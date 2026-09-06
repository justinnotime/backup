"""Copy-only CLI tests with local HTTP fixtures; no installed/private runtime."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from genteam.client import open_request

BUNDLE = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def server(handler):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        instance.server_close()
        worker.join()


class StandaloneTests(unittest.TestCase):
    def test_archive_and_reply_preview_run_from_copied_bundle(self):
        calls = []

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                calls.append((self.command, self.path))
                if self.path.endswith("/servers"):
                    value = {"servers": [{"id": "server-example", "slug": "example"}]}
                elif "/servers/resolve" in self.path:
                    value = {
                        "members": [],
                        "channels": [
                            {"id": "ch_example", "name": "example", "channel_type": "group"}
                        ],
                    }
                elif "/messages" in self.path:
                    value = {
                        "items": [
                            {
                                "ts": "2099-01-01T12:00:00Z",
                                "data": {
                                    "comet_message_id": "10",
                                    "content": "Synthetic archived message",
                                },
                            }
                        ],
                        "has_more": False,
                    }
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                calls.append((self.command, self.path))
                self.send_error(500)

        with tempfile.TemporaryDirectory() as temporary, server(Fixture) as endpoint:
            root = Path(temporary)
            package = root / "copied skill"
            shutil.copytree(BUNDLE / "src", package / "src")
            shutil.copytree(BUNDLE / "scripts", package / "scripts")
            (root / "cookie").write_text("synthetic-cookie")
            configuration = root / "private config.json"
            configuration.write_text(
                json.dumps(
                    {
                        "schema": "genteam/v1",
                        "base_url": endpoint,
                        "cookie_file": "cookie",
                        "archive": {
                            "output_directory": "archive",
                            "state_file": "state/progress.json",
                            "rate_delay": 0,
                            "selection": {"enabled": True, "mode": "blacklist"},
                        },
                        "send": {"state_directory": "sender"},
                    }
                )
            )
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "xdg"),
            }
            for command in (
                ("auth", "--check"),
                ("sync",),
                (
                    "send",
                    "send",
                    "--to",
                    "ch_example",
                    "--reply-to",
                    "10",
                    "--text",
                    "Unsent preview",
                ),
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(package / "scripts" / command[0]),
                        "--config",
                        str(configuration),
                        *command[1:],
                    ],
                    env=environment,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("synthetic-cookie", result.stdout + result.stderr)
            self.assertTrue(any((root / "archive").rglob("*.md")))
            self.assertEqual(
                json.loads((root / "state/progress.json").read_text())["channels"]["ch_example"][
                    "newest_id"
                ],
                "10",
            )
            self.assertFalse(any(method == "POST" for method, _path in calls))

    def test_redirects_never_forward_cookie_or_transport_token(self):
        received = []

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

        with server(Sink) as sink:

            class Redirect(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    self.send_response(302)
                    self.send_header("Location", sink + "/destination")
                    self.end_headers()

            with server(Redirect) as origin:
                for header in ("Cookie", "authToken"):
                    with self.assertRaises(urllib.error.HTTPError):
                        open_request(
                            urllib.request.Request(origin, headers={header: "synthetic-secret"})
                        )
        self.assertEqual(received, [])
