"""画像の認識(説明・文字・物の数・質問)と、その受け口の試験。"""

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.server import make_server
from celestial.vision import MODES, decode_image, parse, prompt

# 1×1 の PNG
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def responder(model, messages, schema):
    if schema and "objects" in schema.get("properties", {}):
        return json.dumps({"objects": [{"name": "りんご", "count": 3}, {"name": "皿", "count": 1}], "summary": "皿にりんごがある"},
                          ensure_ascii=False)
    if "Transcribe" in messages[0]["content"]:
        return "営業時間 9:00-18:00"
    return "机の上にノートPCがあります。"


class VisionTest(unittest.TestCase):
    def test_prompts_and_parsing(self):
        for mode in MODES:
            if MODES[mode].needs_question:
                with self.assertRaises(ValueError):
                    prompt(mode)
                messages, _ = prompt(mode, "これは何?")
            else:
                messages, _ = prompt(mode)
            self.assertIn("Japanese", messages[0]["content"])
        with self.assertRaises(ValueError):
            prompt("x-ray")
        out = parse("objects", '{"objects": [{"name": "猫", "count": 2}, {"name": "", "count": 1}], "summary": "猫がいる"}')
        self.assertEqual(out["objects"], [{"name": "猫", "count": 2}])
        self.assertIn("- 猫: 2", out["text"])
        self.assertEqual(parse("objects", "壊れた出力")["objects"], [])
        self.assertEqual(decode_image("data:image/png;base64," + base64.b64encode(PIXEL).decode()), PIXEL)
        with self.assertRaises(ValueError):
            decode_image(base64.b64encode(b"not an image").decode())

    def test_see_uses_the_vision_model(self):
        backend = MockBackend("mock", responder, [ModelInfo("text-only", "mock", int(2e9)), ModelInfo("eyes", "mock", int(3e9), vision=True)])
        with tempfile.TemporaryDirectory() as tmp:
            app = Celestial(Config(home=Path(tmp)), backends={"mock": backend}, store=Store())
            out = app.see(PIXEL, "objects")
            self.assertEqual(out["model"], "eyes")
            self.assertEqual(out["objects"][0], {"name": "りんご", "count": 3})
            self.assertEqual(backend.image_calls[-1], [PIXEL])
            self.assertEqual(app.see(PIXEL, "ocr")["text"], "営業時間 9:00-18:00")

    def test_endpoint(self):
        backend = MockBackend("mock", responder, [ModelInfo("eyes", "mock", int(3e9), vision=True)])
        with tempfile.TemporaryDirectory() as tmp:
            app = Celestial(Config(home=Path(tmp), auto_title=False), backends={"mock": backend}, store=Store())
            server = make_server(app, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path, body):
                req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())

            try:
                with urllib.request.urlopen(base + "/api/vision/modes", timeout=10) as r:
                    modes = json.loads(r.read())
                self.assertTrue(modes["available"])
                self.assertIn("ocr", [m["id"] for m in modes["modes"]])
                out = post("/api/vision", {"image": base64.b64encode(PIXEL).decode(), "mode": "describe"})
                self.assertEqual(out["text"], "机の上にノートPCがあります。")
                with self.assertRaises(urllib.error.HTTPError) as bad:
                    post("/api/vision", {"image": "", "mode": "describe"})
                self.assertEqual(bad.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                server.api.flows.stop()
                server.api.flows.store.close()


if __name__ == "__main__":
    unittest.main()
