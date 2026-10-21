"""Real local HTTP and temporary storage; providers are explicitly controlled, not live models."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.native_data import NativeDataService
from workbench.server import EvidenceHTTPServer
from workbench.worker import DurableWorker, create_worker_service
from tests.test_chat_http import ToolUsingTestProvider


class DurableHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence.sqlite3"
        self.chat = self.root / "chat.sqlite3"
        self.documents = self.root / "documents"
        build_database(db_path=self.evidence)
        DocumentService(self.documents)
        NativeDataService(self.root / "native-data")
        self.provider = ToolUsingTestProvider()
        self.server = EvidenceHTTPServer(
            0, self.evidence, chat_db=self.chat, documents_root=self.documents,
            provider=self.provider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.workers = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        for worker in self.workers:
            worker.close()
        self.temp.cleanup()

    def request(self, path, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(
                "POST" if data is not None else "GET", path,
                body=json.dumps(data).encode() if data is not None else None,
                headers={"Origin": self.origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_submission_waits_for_separate_worker_and_replays(self):
        status, conversation = self.request("/api/chat/conversations", {"company_id": "MSFT"})
        self.assertEqual(status, 201)
        payload = {"conversation_id": conversation["conversation_id"],
                   "message": "Show available revenue evidence", "request_id": "durable-http-request"}
        status, start = self.request("/api/chat/messages", payload)
        self.assertEqual(status, 202)
        self.assertEqual(start["status"], "queued")
        self.assertEqual(self.provider.histories, [])
        repeat_status, repeated = self.request("/api/chat/messages", payload)
        self.assertEqual(repeat_status, 202)
        self.assertEqual(repeated["run_id"], start["run_id"])
        provider = ToolUsingTestProvider()
        worker_service = create_worker_service(
            self.evidence, self.chat, documents_root=self.documents, provider=provider)
        self.workers.append(worker_service)
        self.assertEqual(worker_service.document_service.root, self.documents)
        self.assertEqual(worker_service.native_data.root, self.root / "native-data")
        worker = DurableWorker(worker_service, heartbeat_seconds=0.1)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        status, run = self.request("/api/chat/run?id=" + start["run_id"])
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "structured_candidate_required")
        self.assertEqual(len(provider.histories), 1)
        with closing(worker_service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM jobs").fetchone()[0], "failed")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant' AND run_id=?",
                (start["run_id"],)).fetchone()[0], 0)

    def test_subprocess_worker_claims_persisted_http_job(self):
        status, conversation = self.request("/api/chat/conversations", {"company_id": "MSFT"})
        self.assertEqual(status, 201)
        status, submitted = self.request("/api/chat/messages", {
            "conversation_id": conversation["conversation_id"], "message": "Read available revenue",
            "request_id": "durable-separate-process"})
        self.assertEqual(status, 202)
        script = (
            "import json,os,sys; "
            "from workbench.worker import create_worker_service,DurableWorker; "
            "from tests.test_chat_http import ToolUsingTestProvider; "
            "s=create_worker_service(sys.argv[1],sys.argv[2],documents_root=sys.argv[3],"
            "provider=ToolUsingTestProvider()); "
            "worked=DurableWorker(s,heartbeat_seconds=.1).run_once(); "
            "s.close(); print(json.dumps({'pid':os.getpid(),'worked':worked}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.evidence), str(self.chat), str(self.documents)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertNotEqual(output["pid"], os.getpid())
        self.assertTrue(output["worked"])
        code, run = self.request("/api/chat/run?id=" + submitted["run_id"])
        self.assertEqual(code, 200)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "structured_candidate_required")
        self.assertEqual(self.provider.histories, [])

    def test_worker_preflights_alias_before_initialization(self):
        before = self.evidence.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "evidence and chat"):
            create_worker_service(self.evidence, self.evidence, provider=ToolUsingTestProvider())
        self.assertEqual(self.evidence.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
