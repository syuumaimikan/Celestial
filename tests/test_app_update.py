"""スマホのアプリの更新: PC が持っている版を知らせる・作ったアプリを配れる場所に置く。"""

import argparse
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.cli import cmd_publish_app
from celestial.evolve import Store
from celestial.server import make_server


class AppUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name) / "home"
        backend = MockBackend("mock", lambda model, messages, schema: "ok", [ModelInfo("m", "mock", int(1e9))])
        self.app = Celestial(Config(home=home, auto_title=False), backends={"mock": backend}, store=Store())

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_and_report_the_version(self):
        fake = Path(self.tmp.name) / "built.apk"
        fake.write_bytes(b"PK\x03\x04" + b"0" * 1000)
        self.assertEqual(cmd_publish_app(self.app, argparse.Namespace(apk=str(fake))), 0)
        placed = self.app.cfg.home / "app"
        self.assertEqual((placed / "celestial.apk").read_bytes(), fake.read_bytes())
        meta = json.loads((placed / "celestial.json").read_text("utf-8"))
        self.assertGreater(meta["build"], 0)                       # pubspec.yaml の版が書き添えられる

        server = make_server(self.app, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/api/app", timeout=10) as r:
                info = json.loads(r.read())
            self.assertTrue(info["available"])
            self.assertEqual((info["version"], info["build"]), (meta["version"], meta["build"]))
            self.assertEqual(info["size"], fake.stat().st_size)
            with urllib.request.urlopen(base + info["url"], timeout=10) as r:   # そのまま受け取れる
                self.assertEqual(r.read(), fake.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            server.api.flows.stop()
            server.api.flows.store.close()

    def test_missing_apk_is_explained(self):
        with self.assertRaises(ValueError):
            cmd_publish_app(self.app, argparse.Namespace(apk=str(Path(self.tmp.name) / "nothing.apk")))


if __name__ == "__main__":
    unittest.main()
