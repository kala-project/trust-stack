"""Composition root shared by HTTP and durable workers, not a transport layer.

Only this assembly module chooses the concrete shared data/model services. It
uses the existing storage identity checks, namespace factories and runtime
provider policy without creating an HTTP server or starting research. Calling
create_runtime_services may initialize service schemas/identity files; callers
must first construct a validated RuntimeConfig and own the returned lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from .chat import ChatService
from .copilot_provider import CopilotProvider
from .documents import DocumentService
from .native_data import NativeDataService
from .runtime_config import RuntimeConfig
from .service import EvidenceService
from .source_acquisition import PublicSourceAcquisition
from .storage_config import StorageConfiguration, verify_local_storage_identity

__all__ = ["RuntimeServices", "create_runtime_services"]


@dataclass(frozen=True)
class RuntimeServices:
    """Internal services, not a serializable client response.

    ChatService owns durable queue resources. The other current services use
    bounded per-operation connections and require no shutdown operation.
    Closing this bundle never stops a different process or deletes data.
    """

    config: RuntimeConfig
    evidence: EvidenceService
    documents: DocumentService | None
    native_data: NativeDataService | None
    acquisition: PublicSourceAcquisition | None
    chat: ChatService

    def close(self) -> None:
        self.chat.close()


def create_runtime_services(config: RuntimeConfig, *, provider=None) -> RuntimeServices:
    """Assemble the same common services for either caller, preserving opt-ins.

    Missing optional registries do not get silently initialized. The caller may
    inject a provider for controlled tests; None selects the existing fixed
    Copilot adapter. No model request or job is submitted by this function.
    """
    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be a validated RuntimeConfig")
    paths = config.paths
    configured_storage = config.storage_config
    if configured_storage is not None and not isinstance(configured_storage, StorageConfiguration):
        configured_storage = StorageConfiguration.load(configured_storage)
    if configured_storage is None:
        verify_local_storage_identity(paths.documents_root, scope="public")
        verify_local_storage_identity(paths.materials_root, scope="private")
        public_store = None
        private_store_factory = None
    else:
        configured_storage.register_public_root(paths.documents_root)
        configured_storage.register_private_root(paths.materials_root, public_root=paths.documents_root)
        public_store = configured_storage.public_store()
        private_store_factory = configured_storage.private_store
    documents = (DocumentService(paths.documents_root, object_store=public_store)
                 if paths.documents_catalog.is_file() else None)
    if paths.native_registry.is_file():
        native_options = {}
        if configured_storage is not None:
            native_options = {
                "public_object_store": configured_storage.native_public_store(),
                "private_object_store_factory": configured_storage.native_private_store,
                "object_store_max_bytes": configured_storage.max_object_bytes,
                "object_store_identity": configured_storage.native_store_identity,
            }
        native = NativeDataService(paths.native_registry.parent, **native_options)
    else:
        native = None
    evidence = EvidenceService(paths.evidence)
    acquisition = None
    if config.enable_public_acquisition:
        if documents is None:
            raise RuntimeError("public acquisition requires an initialized document store")
        acquisition = PublicSourceAcquisition(paths.documents_root, object_store=public_store)
    chat = ChatService(
        paths.evidence, paths.chat,
        provider=provider if provider is not None else CopilotProvider(),
        document_service=documents, native_data_service=native,
        source_acquisition=acquisition,
        verification_config=config.verification_config,
        material_object_store_factory=private_store_factory,
    )
    return RuntimeServices(config, evidence, documents, native, acquisition, chat)
