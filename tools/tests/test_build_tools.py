"""Build-helper regressions; only Python's standard library is required."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error


TOOLS = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_tool("build_native_deps")
preflight = load_tool("check_build_tools")


class PatchelfTests(unittest.TestCase):
    def test_supported_versions(self):
        for version in ("0.14.5", "0.19.1"):
            with self.subTest(version=version), \
                    patch.object(preflight.shutil, "which", return_value="/build/bin/patchelf"), \
                    patch.object(preflight.subprocess, "check_output", return_value=f"patchelf {version}\n"):
                path, reported = preflight.check_patchelf()
                self.assertEqual(path, Path("/build/bin/patchelf"))
                self.assertEqual(reported, f"patchelf {version}")

    def test_old_or_unrecognized_version_fails(self):
        for reported in ("patchelf 0.14.3", "patchelf 0.14", "unknown"):
            with self.subTest(reported=reported), \
                    patch.object(preflight.shutil, "which", return_value="/usr/bin/patchelf"), \
                    patch.object(preflight.subprocess, "check_output", return_value=reported):
                with self.assertRaisesRegex(RuntimeError, "patchelf >= 0.14.5"):
                    preflight.check_patchelf()

    def test_missing_executable_fails(self):
        with patch.object(preflight.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "build-requirements.txt"):
                preflight.check_patchelf()


class SourceDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requests += 1
                status, content_type, body, extra_length = self.server.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body) + extra_length))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codec4ai-fetch-test-")
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        # The fetcher enforces exact bytes; this deliberately small fixture is not decoded.
        self.payload = b"pinned source archive fixture\n"
        self.item = {
            "name": "fixture", "filename": "fixture.tar.gz",
            "url": f"http://127.0.0.1:{self.server.server_port}/fixture.tar.gz",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        self.server.responses = []
        self.server.requests = 0
        sleeper = patch.object(builder.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        redirect = redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def response(self, body=None, status=200, content_type="application/octet-stream", extra_length=0):
        return status, content_type, self.payload if body is None else body, extra_length

    def test_html_response_is_preserved_and_retry_can_recover(self):
        wrong = b"<html>temporary error</html>"
        self.server.responses = [self.response(wrong, content_type="text/html"), self.response()]
        archive = builder.fetch(self.item, self.cache)
        self.assertEqual(archive.read_bytes(), self.payload)
        self.assertEqual(self.server.requests, 2)
        self.sleep.assert_called_once_with(2)
        report = json.loads((self.cache / "diagnostics/fixture.tar.gz.attempt-1.json").read_text())
        self.assertEqual(report["status"], 200)
        self.assertEqual(report["content_type"], "text/html")
        self.assertEqual(report["expected_sha256"], self.item["sha256"])
        self.assertEqual(report["actual_sha256"], hashlib.sha256(wrong).hexdigest())
        self.assertEqual((self.cache / "diagnostics" / report["response_file"]).read_bytes(), wrong)
        self.assertFalse((self.cache / "fixture.tar.gz.part").exists())

    def test_http_error_response_is_preserved_and_retried(self):
        wrong = b"service unavailable"
        self.server.responses = [self.response(wrong, status=503), self.response()]
        self.assertEqual(builder.fetch(self.item, self.cache).read_bytes(), self.payload)
        report = json.loads((self.cache / "diagnostics/fixture.tar.gz.attempt-1.json").read_text())
        self.assertEqual(report["status"], 503)
        self.assertEqual((self.cache / "diagnostics" / report["response_file"]).read_bytes(), wrong)

    def test_incomplete_transfer_restarts_from_empty_file(self):
        self.server.responses = [self.response(b"partial", extra_length=100), self.response()]
        self.assertEqual(builder.fetch(self.item, self.cache).read_bytes(), self.payload)
        self.assertEqual(self.server.requests, 2)

    def test_connection_error_is_retried(self):
        self.server.responses = [self.response()]
        original = builder.urllib.request.urlopen
        calls = 0

        def flaky(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError("connection reset")
            return original(*args, **kwargs)

        with patch.object(builder.urllib.request, "urlopen", side_effect=flaky):
            self.assertEqual(builder.fetch(self.item, self.cache).read_bytes(), self.payload)
        self.assertEqual(calls, 2)
        report = json.loads((self.cache / "diagnostics/fixture.tar.gz.attempt-1.json").read_text())
        self.assertIn("connection reset", report["error"])

    def test_repeated_mismatch_fails_without_populating_cache(self):
        self.server.responses = [self.response(b"wrong")] * 3
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            builder.fetch(self.item, self.cache)
        self.assertEqual(self.server.requests, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4])
        self.assertFalse((self.cache / "fixture.tar.gz").exists())
        self.assertFalse((self.cache / "fixture.tar.gz.part").exists())
        self.assertEqual(len(list((self.cache / "diagnostics").glob("*.json"))), 3)

    def test_verified_cache_does_not_download(self):
        archive = self.cache / self.item["filename"]
        archive.write_bytes(self.payload)
        self.assertEqual(builder.fetch(self.item, self.cache), archive)
        self.assertEqual(self.server.requests, 0)
        self.sleep.assert_not_called()

    def test_corrupt_cache_is_rejected_without_overwriting_it(self):
        archive = self.cache / self.item["filename"]
        archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "corrupt cached file"):
            builder.fetch(self.item, self.cache)
        self.assertEqual(archive.read_bytes(), b"corrupt")
        self.assertEqual(self.server.requests, 0)


if __name__ == "__main__":
    unittest.main()
