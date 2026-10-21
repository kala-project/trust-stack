"""CDP transport regressions; no Chromium or model is required."""
import base64
import hashlib
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from scripts.browser_smoke import CDP


class BrowserTransportTests(unittest.TestCase):
    def test_idle_connection_survives_handshake_timeout(self):
        client, server = socket.socketpair()
        client.settimeout(.1)  # Production handshake deadline is five seconds.
        server.settimeout(3)
        failures = []

        def receive_exact(size):
            data = b""
            while len(data) < size:
                piece = server.recv(size - len(data))
                if not piece:
                    raise EOFError()
                data += piece
            return data

        def respond():
            try:
                request = b""
                while not request.endswith(b"\r\n\r\n"):
                    request += receive_exact(1)
                key = next(line.split(b": ", 1)[1] for line in request.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                server.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                frame = receive_exact(2)
                length = frame[1] & 127
                if length == 126:
                    length = int.from_bytes(receive_exact(2), "big")
                mask = receive_exact(4)
                payload = receive_exact(length)
                message = json.loads(bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
                response = json.dumps({"id": message["id"], "result": {"value": 1}}).encode()
                server.sendall(bytes([0x81, len(response)]) + response)
                server.recv(100)  # Wait for explicit client close.
            except Exception as exc:
                failures.append(exc)
            finally:
                server.close()

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        cdp = None
        try:
            with patch("scripts.browser_smoke.socket.create_connection", return_value=client):
                cdp = CDP("ws://127.0.0.1/test")
            time.sleep(.25)  # Longer than the socket's original handshake timeout.
            self.assertEqual(cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=1), {"value": 1})
            self.assertNotIn({"method": "Client.readerError"}, cdp.events)
        finally:
            if cdp:
                cdp.close()
                cdp.reader.join(2)
            else:
                client.close()
            worker.join(3)
        self.assertFalse(failures, failures)
