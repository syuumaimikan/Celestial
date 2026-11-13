"""自動化(n8n のようなワークフロー)の試験。"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.flows import FlowEngine, FlowStore, check, describe_trigger, draft_from_ai, next_run, normalize_flow, render
from celestial.server import make_http_server, make_server


class FakeApp:
    def __init__(self, home: Path, **flags):
        workspace = home / "work"
        workspace.mkdir(exist_ok=True)
        self.cfg = SimpleNamespace(workspace=str(workspace), tools_write=flags.get("write", True), tools_run=False,
                                   tools_net=False, home=home)
        self.asked = []

    def run(self, prompt):
        self.asked.append(prompt)
        return SimpleNamespace(text=f"要約: {prompt[:20]}")

    def decide(self, question, options=None, context=""):
        self.asked.append(f"判断: {question} {options} {context}")
        return SimpleNamespace(answer="至急" if "止まって" in context else "普通", probability=0.93)


class ShapeTest(unittest.TestCase):
    def test_normalize(self):
        flow = normalize_flow({"name": "朝", "trigger": {"type": "schedule", "time": "8:05", "days": [0, 1, 9]},
                               "steps": [{"type": "ai", "prompt": "こんにちは"}, {"type": "notify", "target": "どこか"}]})
        self.assertEqual(flow["trigger"], {"type": "schedule", "time": "08:05", "days": [0, 1]})
        self.assertEqual(flow["steps"][1]["target"], "all")  # 知らない送り先は「すべて」
        self.assertTrue(flow["hook_key"])
        for bad, message in (({"trigger": {"type": "schedule", "time": "25:00"}, "steps": [{"type": "ai", "prompt": "x"}]}, "時刻"),
                             ({"trigger": {"type": "file", "folder": "../外"}, "steps": [{"type": "ai", "prompt": "x"}]}, "作業フォルダ"),
                             ({"steps": []}, "手順を1つ以上"),
                             ({"steps": [{"type": "ai", "prompt": " "}]}, "依頼文"),
                             ({"steps": [{"type": "rm -rf"}]}, "種類")):
            with self.assertRaises(ValueError) as caught:
                normalize_flow(bad)
            self.assertIn(message, str(caught.exception))

    def test_schedule_and_templates(self):
        friday_7am = time.mktime((2026, 11, 13, 7, 0, 0, 0, 0, -1))
        weekday = {"type": "schedule", "time": "08:00", "days": [0, 1, 2, 3, 4]}
        self.assertEqual(time.localtime(next_run(weekday, friday_7am))[:5], (2026, 11, 13, 8, 0))
        after = time.mktime((2026, 11, 13, 9, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(next_run(weekday, after))[:5], (2026, 11, 16, 8, 0))  # 土日を飛ばして月曜
        self.assertEqual(describe_trigger(weekday), "平日 08:00")
        self.assertEqual(describe_trigger({"type": "interval", "minutes": 120}), "2時間ごと")
        self.assertEqual(render("前: {{prev}} / 1: {{step1}} / {{unknown}}", {"prev": "B", "step1": "A"}), "前: B / 1: A / {{unknown}}")
        self.assertTrue(check("エラーが3件", "contains", "エラー"))
        self.assertFalse(check("", "not_empty", ""))
        self.assertTrue(check("2026-11-13", "regex", r"\d{4}-\d\d"))

    def test_ai_draft(self):
        draft = draft_from_ai(json.dumps({"name": "通知", "trigger": {"type": "interval", "minutes": 30},
                                          "steps": [{"type": "wait", "label": "待つ", "seconds": 5}]}))
        self.assertFalse(draft["enabled"])  # 下書きは止めた状態(確かめてから使う)
        self.assertEqual(draft["steps"][0]["seconds"], "5")
        with self.assertRaises(ValueError):
            draft_from_ai("JSON ではない")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.app = FakeApp(self.home)
        self.events = []
        self.engine = FlowEngine(FlowStore(), self.app, publish=lambda event, **data: self.events.append((event, data)))

    def tearDown(self):
        self.engine.stop()
        self.tmp.cleanup()

    def test_steps_pass_results_along(self):
        flow = self.engine.save({"name": "日報", "steps": [
            {"type": "template", "text": "今日のメモ: {{trigger}}"},
            {"type": "ai", "prompt": "要約して: {{prev}}"},
            {"type": "condition", "value": "{{prev}}", "op": "contains", "match": "要約"},
            {"type": "write_file", "path": "日報.md", "content": "# {{date}}\n{{step2}}"},
            {"type": "document", "kind": "xlsx", "path": "表", "content": "項目,値\nA,1"},
            {"type": "notify", "title": "日報", "text": "{{step2}}", "target": "phone"}]})
        run = self.engine.run_now(flow["id"], "会議が2件")
        self.assertEqual(run["status"], "done", run["log"])
        self.assertEqual(self.app.asked, ["要約して: 今日のメモ: 会議が2件"])
        self.assertIn("要約: 要約して", (self.home / "work" / "日報.md").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "work" / "表.xlsx").is_file())
        push = [d for k, d in self.events if k == "push"]
        self.assertEqual(push[0]["text"], "要約: 要約して: 今日のメモ: 会議が2件")
        self.assertTrue(push[0]["only_phones"])
        self.assertEqual([e["status"] for e in run["log"]], ["ok"] * 6)

    def test_decide_step_branches(self):
        flow = self.engine.save({"name": "問い合わせの仕分け", "trigger": {"type": "webhook"}, "steps": [
            {"type": "decide", "question": "緊急度は?", "options": "至急\n普通\n低い", "context": "{{trigger}}"},
            {"type": "condition", "value": "{{prev}}", "op": "equals", "match": "至急"},
            {"type": "template", "text": "至急({{step1.probability}}): {{trigger}}"}]})
        run = self.engine.run_now(flow["id"], "サーバーが止まっています")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["output"], "至急(0.93): サーバーが止まっています")
        self.assertIn("判断: 緊急度は? ['至急', '普通', '低い'] サーバーが止まっています", self.app.asked)
        self.assertEqual(self.engine.run_now(flow["id"], "資料をください")["status"], "stopped")  # 普通なら止まる

    def test_condition_stops_and_errors_are_recorded(self):
        flow = self.engine.save({"name": "止める", "steps": [{"type": "condition", "value": "{{trigger}}", "op": "contains", "match": "至急"},
                                                            {"type": "notify", "text": "x"}]})
        run = self.engine.run_now(flow["id"], "普通の連絡")
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(len(run["log"]), 1)
        # 許可していない種類の手順は、理由を記録して止まる
        self.app.cfg.tools_write = False
        flow = self.engine.save({"name": "書く", "steps": [{"type": "write_file", "path": "a.txt", "content": "x"}]})
        run = self.engine.run_now(flow["id"])
        self.assertEqual(run["status"], "error")
        self.assertIn("ファイルの変更を許可", run["log"][0]["output"])
        flow = self.engine.save({"name": "外へ", "steps": [{"type": "read_file", "path": "../../秘密.txt"}]})
        self.app.cfg.tools_write = True
        self.assertEqual(self.engine.run_now(flow["id"])["status"], "error")  # 作業フォルダの外には届かない

    def test_schedule_and_file_triggers(self):
        flow = self.engine.save({"name": "時刻", "trigger": {"type": "interval", "minutes": 5}, "steps": [{"type": "template", "text": "x"}]})
        now = time.time()
        self.assertEqual(self.engine.tick(now), [])  # 最初は次の時刻を決めるだけ
        self.assertEqual(self.engine.tick(now + 60), [])
        started = self.engine.tick(now + 301)
        self.assertEqual(len(started), 1)
        self.engine.set_enabled(flow["id"], False)
        watch = self.engine.save({"name": "受信箱", "trigger": {"type": "file", "folder": "受信箱", "pattern": "*.txt"},
                                  "steps": [{"type": "template", "text": "{{trigger.name}}: {{trigger.text}}"}]})
        inbox = self.home / "work" / "受信箱"
        inbox.mkdir()
        (inbox / "前から.txt").write_text("古い", encoding="utf-8")
        self.assertEqual(self.engine.tick(), [])  # 前からあるファイルでは動かない
        (inbox / "新しい.txt").write_text("こんにちは", encoding="utf-8")
        (inbox / "画像.png").write_bytes(b"x")
        run_ids = self.engine.tick()
        self.assertEqual(len(run_ids), 1)
        for _ in range(100):
            run = self.engine.store.run(run_ids[0])
            if run["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(run["output"], "新しい.txt: こんにちは")
        self.assertEqual(self.engine.store.runs(watch["id"])[0]["source"], "file")


class ServerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        (home / "work").mkdir()
        backend = MockBackend("mock", lambda model, messages, schema: json.dumps({"role": "general"}) if schema else "了解しました",
                              [ModelInfo("small", "mock", int(1e9))])
        self.app = Celestial(Config(home=home, workspace=str(home / "work"), auto_title=False), backends={"mock": backend}, store=Store())
        self.server = make_server(self.app, 0)
        self.api = self.server.api
        self.remote_server = make_http_server(self.api, "127.0.0.1", 0, remote=True)
        for s in (self.server, self.remote_server):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.remote = f"http://127.0.0.1:{self.remote_server.server_address[1]}"

    def tearDown(self):
        self.api.flows.stop()
        for s in (self.server, self.remote_server):
            s.shutdown()
            s.server_close()
        self.api.flows.store.close()
        self.tmp.cleanup()

    def call(self, url, body=None, headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def status(self, url, body=None, headers=None):
        try:
            self.call(url, body, headers)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def test_webhook_and_permissions(self):
        flow = self.call(self.base + "/api/flows", {"name": "受け取り", "trigger": {"type": "webhook"},
                                                    "steps": [{"type": "ai", "prompt": "{{trigger.text}} に返事して"}]})
        url = flow["hook_urls"][0]
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        result = self.call(url + "&wait=1", {"text": "明日の予定"})
        self.assertEqual((result["status"], result["output"]), ("done", "了解しました"))
        self.assertEqual(self.status(url.split("?")[0] + "?key=wrong-key&wait=1", {"text": "x"}), 404)  # 鍵が違えば動かない
        listing = self.call(self.base + "/api/flows")
        self.assertEqual(listing["flows"][0]["last_run"]["status"], "done")
        # スマホからは、作る・変えるはできず、動かす・履歴を見ることはできる
        code = self.call(self.base + "/api/pairing/start", {})["code"]
        token = self.call(self.remote + "/api/pair", {"code": code, "name": "Pixel", "platform": "android"})["token"]
        auth = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.status(self.remote + "/api/flows", {"name": "x", "steps": [{"type": "command", "command": "dir"}]}, auth), 403)
        phone_view = self.call(self.remote + "/api/flows", headers=auth)
        self.assertFalse(phone_view["can_edit"])
        self.assertNotIn("hook_key", phone_view["flows"][0])  # Webhook の鍵はスマホに出さない
        self.assertNotIn("hook_urls", phone_view["flows"][0])
        run_id = self.call(self.remote + f"/api/flows/{flow['id']}/run", {"input": "{\"text\": \"スマホから\"}"}, auth)["run_id"]
        for _ in range(100):
            run = self.call(self.remote + f"/api/flows/runs/{run_id}", headers=auth)
            if run["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual((run["status"], run["source"]), ("done", "phone"))
        self.assertEqual(self.status(self.remote + f"/api/flows/{flow['id']}/delete", {}, auth), 403)


if __name__ == "__main__":
    unittest.main()
