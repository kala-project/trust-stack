"""Shared assembly contracts; temporary data and controlled providers, not live research."""
import ast
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.native_data import NativeDataService
from workbench.runtime import create_runtime_services
from workbench.runtime_config import RuntimeConfig, RuntimePaths
from workbench.server import EvidenceHTTPServer
from workbench.worker import create_worker_service
from tests.test_chat import FakeProvider

ROOT = Path(__file__).resolve().parent.parent


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_defaults_are_absolute_and_configuration_does_not_create_files(self):
        parent = self.root / "not-created"
        paths = RuntimePaths.resolve(parent / "evidence.sqlite3")
        config = RuntimeConfig(paths)
        self.assertEqual(paths.chat, parent / "chat.sqlite3")
        self.assertEqual(paths.wiki, parent / "wiki.sqlite3")
        self.assertEqual(paths.sample, parent / "sample_research.sqlite3")
        self.assertEqual(paths.documents_catalog, parent / "documents/catalog.sqlite3")
        self.assertEqual(paths.native_registry, parent / "native-data/registry.sqlite")
        self.assertEqual(paths.materials_root, parent / "chat.sqlite3.materials")
        self.assertFalse(config.enable_public_acquisition)
        self.assertFalse(parent.exists())
        with self.assertRaises(FrozenInstanceError):
            paths.chat = paths.evidence

    def test_explicit_paths_and_storage_path_are_not_loaded_during_validation(self):
        paths = RuntimePaths.resolve(
            self.root / "evidence.sqlite3", chat_db=self.root / "private/session.sqlite",
            wiki_db=self.root / "notes.sqlite", sample_db=self.root / "sample.sqlite",
            documents_root=self.root / "raw-catalog", auth_path=self.root / "owner.sqlite")
        config = RuntimeConfig(paths, storage_config="not-loaded.json")
        self.assertEqual(config.paths.materials_root, self.root / "private/session.sqlite.materials")
        self.assertEqual(config.paths.auth, self.root / "owner.sqlite")
        self.assertNotIn("not-loaded.json", repr(config))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_runtime_config_rejects_types_unknown_fields_and_modes(self):
        paths = RuntimePaths.resolve(self.root / "evidence.sqlite3")
        cases = [{"enable_public_acquisition": value} for value in (1, "true", None)]
        cases += [{"storage_config": value} for value in ({}, [], 42, " ")]
        for options in cases:
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                RuntimeConfig(paths, **options)
        with self.assertRaises(TypeError):
            RuntimeConfig(paths, unknown=True)
        with self.assertRaises(TypeError):
            RuntimeConfig({"evidence": "x"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_or_non_path_inputs_are_rejected(self):
        for field in ("db_path", "chat_db", "wiki_db", "sample_db", "documents_root", "auth_path"):
            for value in ("", " ", 12, False):
                with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                    RuntimePaths.resolve(**{"db_path": self.root / "evidence.sqlite3", field: value})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_all_registered_database_aliases_are_rejected(self):
        evidence = self.root / "evidence.sqlite3"
        for field in ("chat_db", "wiki_db", "sample_db", "auth_path"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "must be distinct"):
                RuntimePaths.resolve(evidence, **{field: evidence})
        with self.assertRaisesRegex(RuntimeError, "documents_catalog"):
            RuntimePaths.resolve(self.root / "documents/catalog.sqlite3", documents_root=self.root / "documents")
        with self.assertRaisesRegex(RuntimeError, "native_registry"):
            RuntimePaths.resolve(evidence, chat_db=self.root / "native-data/registry.sqlite")
        with self.assertRaisesRegex(RuntimeError, "must be distinct"):
            RuntimePaths.resolve(evidence, chat_db=self.root / "same", wiki_db=self.root / "same")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_and_hardlink_aliases_are_rejected_without_modification(self):
        evidence = self.root / "evidence.sqlite3"
        evidence.write_bytes(b"sentinel: never initialize this as a writable database")
        for kind in ("symlink", "hardlink"):
            alias = self.root / (kind + ".sqlite")
            if kind == "symlink":
                alias.symlink_to(evidence)
            else:
                os.link(evidence, alias)
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "must be distinct"):
                RuntimePaths.resolve(evidence, chat_db=alias)
        self.assertEqual(evidence.read_bytes(), b"sentinel: never initialize this as a writable database")


class RuntimeAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence.sqlite3"
        self.paths = RuntimePaths.resolve(self.evidence)
        build_database(db_path=self.evidence)

    def initialized_sources(self):
        DocumentService(self.paths.documents_root)
        NativeDataService(self.paths.native_registry.parent)

    def test_api_and_worker_share_common_services_and_explicit_options(self):
        self.initialized_sources()
        provider = FakeProvider()
        server = EvidenceHTTPServer(
            0, self.evidence, provider=provider, enable_public_acquisition=True)
        self.addCleanup(server.server_close)
        worker = create_worker_service(
            self.evidence, self.paths.chat, provider=provider, enable_public_acquisition=True)
        self.addCleanup(worker.close)
        self.assertEqual(server.runtime.config.paths, self.paths)
        self.assertIs(server.service, server.runtime.evidence)
        self.assertIs(server.documents, server.chat.document_service)
        self.assertIs(server.native_data, server.chat.native_data)
        self.assertIs(server.runtime.acquisition, server.chat.source_acquisition)
        for service in (server.chat, worker):
            self.assertEqual(service.evidence_db, self.paths.evidence)
            self.assertEqual(service.chat_db, self.paths.chat)
            self.assertEqual(service.document_service.root, self.paths.documents_root)
            self.assertEqual(service.native_data.root, self.paths.native_registry.parent)
            self.assertIs(service.provider, provider)
            self.assertIsNotNone(service.source_acquisition)

    def test_optional_registries_are_not_created_and_provider_is_injected(self):
        provider = FakeProvider()
        with mock.patch("workbench.runtime.CopilotProvider", side_effect=AssertionError("not requested")):
            services = create_runtime_services(RuntimeConfig(self.paths), provider=provider)
        self.addCleanup(services.close)
        self.assertIsNone(services.documents)
        self.assertIsNone(services.native_data)
        self.assertIsNone(services.acquisition)
        self.assertIs(services.chat.provider, provider)
        self.assertFalse(self.paths.documents_catalog.exists())
        self.assertFalse(self.paths.native_registry.exists())
        services.close()
        services.close()
        self.assertTrue(services.chat._closed)

    def test_default_provider_uses_existing_adapter_without_starting_a_request(self):
        provider = FakeProvider()
        with mock.patch("workbench.runtime.CopilotProvider", return_value=provider) as factory:
            services = create_runtime_services(RuntimeConfig(self.paths))
        self.addCleanup(services.close)
        factory.assert_called_once_with()
        self.assertIs(services.chat.provider, provider)

    def test_acquisition_without_catalog_fails_before_chat_or_provider_creation(self):
        with mock.patch("workbench.runtime.ChatService") as chat, mock.patch(
                "workbench.runtime.CopilotProvider") as provider:
            with self.assertRaisesRegex(RuntimeError, "initialized document store"):
                create_runtime_services(RuntimeConfig(self.paths, enable_public_acquisition=True))
        chat.assert_not_called()
        provider.assert_not_called()
        self.assertFalse(self.paths.chat.exists())

    def test_both_entries_reject_alias_before_factory_is_called(self):
        for entry in ("api", "worker"):
            with self.subTest(entry=entry), mock.patch(
                    "workbench.runtime.create_runtime_services") as worker_factory, mock.patch(
                    "workbench.server.create_runtime_services") as api_factory:
                with self.assertRaisesRegex(RuntimeError, "must be distinct"):
                    if entry == "api":
                        EvidenceHTTPServer(0, self.evidence, chat_db=self.evidence, provider=FakeProvider())
                    else:
                        create_worker_service(self.evidence, self.evidence, provider=FakeProvider())
                worker_factory.assert_not_called()
                api_factory.assert_not_called()

    def test_http_binding_failure_closes_assembled_service(self):
        assembled = []

        def factory(*args, **kwargs):
            services = create_runtime_services(*args, **kwargs)
            assembled.append(services)
            return services

        with mock.patch("workbench.server.create_runtime_services", side_effect=factory), mock.patch(
                "workbench.server.ThreadingHTTPServer.__init__", side_effect=OSError("port in use")):
            with self.assertRaisesRegex(OSError, "port in use"):
                EvidenceHTTPServer(0, self.evidence, provider=FakeProvider())
        self.assertEqual(len(assembled), 1)
        self.assertTrue(assembled[0].chat._closed)

    def test_worker_assembly_in_fresh_process_never_imports_http_entrypoint(self):
        script = """
import sys
from workbench.worker import create_worker_service
class Provider:
    pass
service = create_worker_service(sys.argv[1], sys.argv[2], provider=Provider())
try:
    assert 'workbench.server' not in sys.modules
    assert service.verification_store is not None
finally:
    service.close()
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.evidence), str(self.paths.chat)],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class LayerDependencyTests(unittest.TestCase):
    def test_cleaned_boundaries_do_not_reintroduce_reverse_imports(self):
        # This is deliberately a migrated-boundary check, not a claim that the
        # entire legacy module graph is clean. Walk nested imports as well.
        forbidden = {
            "runtime": {"server", "worker"},
            "runtime_config": {"server", "worker", "runtime", "chat", "research_tools"},
            "worker": {"server"},
            "execution_scope": {"research_tools", "server", "runtime"},
            "research_inputs": {"research_tools", "server", "runtime"},
            "tool_errors": {"research_tools", "server", "runtime", "chat"},
        }
        for module, targets in forbidden.items():
            tree = ast.parse((ROOT / "workbench" / (module + ".py")).read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    if node.level == 1 and node.module is None:
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif node.level == 1:
                        names = [(node.module or "").split(".")[0]]
                    elif node.module == "workbench":
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif (node.module or "").startswith("workbench."):
                        names = [node.module.split(".")[1]]
                elif isinstance(node, ast.Import):
                    names = [alias.name.split(".")[1] for alias in node.names
                             if alias.name.startswith("workbench.")]
                with self.subTest(module=module, line=getattr(node, "lineno", None)):
                    self.assertFalse(set(names) & targets, f"{module} imports {names}")


if __name__ == "__main__":
    unittest.main()
