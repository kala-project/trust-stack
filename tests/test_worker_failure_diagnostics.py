"""Failure diagnostics contain code locations, not private exception contents."""
import io
import json
import unittest
from unittest import mock

from workbench.worker_process import execute_claim_process


class WorkerFailureDiagnosticsTests(unittest.TestCase):
    def exercise(self, sink):
        control = mock.Mock()
        start = mock.Mock()
        start.wait.return_value = True
        with mock.patch("workbench.worker_process.os.setsid"), \
             mock.patch("workbench.worker_process._install_parent_death_signal"), \
             mock.patch("workbench.worker_process.WorkerProcessConfig", side_effect=RuntimeError("PRIVATE-MARKER")), \
             mock.patch("workbench.worker_process.sys.stderr", sink):
            execute_claim_process({}, {}, mock.Mock(), start, control, 1)
        failure = control.send.call_args_list[-1].args[0]
        self.assertEqual(failure["kind"], "failed")
        self.assertEqual(failure["failure"]["code"], "executor_startup_error")
        self.assertNotIn("PRIVATE-MARKER", json.dumps(failure))
        control.close.assert_called_once()

    def test_log_contains_locations_but_no_exception_text_or_source(self):
        sink = io.StringIO()
        self.exercise(sink)
        raw = sink.getvalue()
        self.assertNotIn("PRIVATE-MARKER", raw)
        result = json.loads(raw)
        self.assertEqual(set(result), {"event", "exception_type", "frames"})
        self.assertEqual(result["exception_type"], "RuntimeError")
        self.assertTrue(result["frames"])
        for frame in result["frames"]:
            self.assertEqual(set(frame), {"module", "function", "line"})
            self.assertEqual(frame["module"], "worker_process.py")
            self.assertEqual(frame["function"], "execute_claim_process")
            self.assertIsInstance(frame["line"], int)

    def test_broken_log_does_not_prevent_terminal_failure_reporting(self):
        sink = mock.Mock()
        sink.write.side_effect = OSError("PRIVATE-SINK-MARKER")
        self.exercise(sink)
