import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.chats import ChatStore
from celestial.evolve import Store
from celestial.runs import needs_context
from celestial.server import make_http_server, make_server


def responder(model, messages, schema):
    if schema and "role" in schema.get("properties", {}):
        return json.dumps({"role": "general"})
    if schema and "anyOf" in schema:  # エージェントの1手: 1回目はファイル作成、2回目は報告
        if not any("[write_file result]" in m["content"] for m in messages):
            return json.dumps({"thought": "書く", "tool": "write_file", "args": {"path": "note.txt", "content": "メモ"}})
        return json.dumps({"thought": "完了", "tool": "answer", "answer": "note.txt を作成しました。"})
    if schema:
        return "{}"
    return "こんにちは"


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.workspace = home / "work"
        self.workspace.mkdir()
        backend = MockBackend("mock", responder, [ModelInfo("small", "mock", int(1e9))])
        # 題名の自動付与は裏で走って題名を変えるので、ここでは切る(TitleTest で確かめる)
        cfg = Config(home=home, workspace=str(self.workspace), tools_write=True, tools_confirm=True, cache=True, auto_title=False)
        self.app = Celestial(cfg, backends={"mock": backend}, store=Store())
        self.server = make_server(self.app, 0)
        self.api = self.server.api
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # Tailscale 側の受け口を、手元のアドレスで再現する
        self.remote_server = make_http_server(self.api, "127.0.0.1", 0, remote=True)
        self.remote = f"http://127.0.0.1:{self.remote_server.server_address[1]}"
        for s in (self.server, self.remote_server):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def tearDown(self):
        for s in (self.server, self.remote_server):
            s.shutdown()
            s.server_close()
        self.api.flows.stop()
        self.api.flows.store.close()  # 開いたままだと一時フォルダを消せない(Windows)
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None, base=None, method=None, raw=False):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = urllib.request.Request((base or self.base) + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read()
            kind = r.headers.get("Content-Type", "")
            return (content, r) if raw else json.loads(content) if kind.startswith("application/json") else content.decode()

    def status_of(self, path, body=None, headers=None, base=None, method=None):
        try:
            self.call(path, body, headers, base, method, raw=True)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def pair(self):
        code = self.call("/api/pairing/start", {})["code"]
        result = self.call("/api/pair", {"code": code, "name": "Pixel", "platform": "android"}, base=self.remote)
        return {"Authorization": f"Bearer {result['token']}"}, result


class LocalTest(ServerBase):
    def test_pages_and_state(self):
        self.assertIn("Celestial", self.call("/"))
        self.assertIn("sendMessage", self.call("/app.js"))
        state = self.call("/api/state")
        self.assertEqual((state["manager"], state["who"]), ("small", "pc"))
        self.assertEqual(self.status_of("/../pyproject.toml"), 404)

    def test_rejects_other_origins(self):
        self.assertEqual(self.status_of("/api/run", {"task": "x"}, {"Origin": "https://evil.example"}), 403)
        self.assertEqual(self.status_of("/api/state", headers={"Host": "evil.example"}), 403)

    def test_rejects_requests_through_a_proxy(self):
        # Tailscale Funnel などで誤って公開しても、鍵なしの受け口は外から使えない
        for header in ({"X-Forwarded-For": "203.0.113.5"}, {"Tailscale-Funnel-Request": "?1"}, {"Via": "1.1 proxy"}):
            self.assertEqual(self.status_of("/api/state", headers=header), 403)
        self.assertEqual(self.status_of("/api/state"), 200)

    def test_run_is_saved_to_conversation(self):
        lines = [json.loads(line) for line in self.call("/api/run", {"task": "やあ"}).splitlines()]
        kinds = [e["type"] for e in lines]
        self.assertEqual((kinds[0], kinds[-1]), ("run", "done"))
        self.assertIn("token", kinds)
        first, done = lines[0], lines[-1]
        self.assertEqual(done["result"]["text"], "こんにちは")
        conv = self.call(f"/api/conversations/{first['conversation_id']}")
        self.assertEqual([(m["role"], m["content"]) for m in conv["messages"]], [("user", "やあ"), ("assistant", "こんにちは")])
        self.assertEqual(conv["conversation"]["title"], "やあ")
        self.assertEqual(conv["messages"][1]["meta"]["status"], "done")

        # メッセージへの評価が、学習(interaction)にも伝わる
        self.call("/api/feedback", {"message_id": first["assistant_message_id"], "good": True})
        self.assertEqual(self.call("/api/history")[0]["feedback"], 1)
        self.assertEqual(self.call(f"/api/conversations/{first['conversation_id']}")["messages"][1]["meta"]["feedback"], 1)

        # 同じ会話の続きでも、単独で意味が通る依頼ならキャッシュが効く。短い返事は会話の流れを使う
        cid = first["conversation_id"]
        task = "日本で一番高い山について教えてください"
        self.call("/api/run", {"task": task, "conversation_id": cid})
        again = [json.loads(line) for line in self.call("/api/run", {"task": task, "conversation_id": cid}).splitlines()]
        self.assertEqual((again[0]["context"], again[-1]["result"]["source"]), (False, "cache"))
        follow = [json.loads(line) for line in self.call("/api/run", {"task": "もっと短く", "conversation_id": cid}).splitlines()]
        self.assertEqual((follow[0]["context"], follow[-1]["result"]["source"]), (True, "chat"))
        self.assertEqual(len(self.call(f"/api/conversations/{cid}")["messages"]), 8)
        listing = self.call("/api/conversations")["conversations"]
        self.assertEqual((len(listing), listing[0]["count"]), (1, 8))

    def test_regenerate_replaces_answer(self):
        first = [json.loads(line) for line in self.call("/api/run", {"task": "東京タワーの高さは?"}).splitlines()][0]
        self.call("/api/feedback", {"message_id": first["assistant_message_id"], "good": False})
        self.app.backends["mock"].responder = lambda model, messages, schema: (
            json.dumps({"role": "general"}) if schema and "role" in schema.get("properties", {})
            else json.dumps({"ok": True, "issue": ""}) if schema else "333メートルです。")
        events = [json.loads(line) for line in self.call(f"/api/messages/{first['assistant_message_id']}/regenerate", {}).splitlines()]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["result"]["source"], "llm")  # キャッシュではなく作り直す
        conv = self.call(f"/api/conversations/{first['conversation_id']}")
        self.assertEqual([(m["role"], m["content"]) for m in conv["messages"]],
                         [("user", "東京タワーの高さは?"), ("assistant", "333メートルです。")])
        self.assertEqual(conv["messages"][1]["meta"]["regenerated"], 1)
        self.assertIsNone(conv["messages"][1]["meta"]["feedback"])  # 前の回答への評価は引き継がない
        # 依頼(ユーザーの発言)は作り直せない
        self.assertEqual(self.status_of(f"/api/messages/{first['user_message_id']}/regenerate", {}), 404)

    def test_answer_export_to_office_files(self):
        import io
        import zipfile
        cid = self.api.chats.create_conversation("四半期の売上")["id"]
        table = self.api.chats.add_message(cid, "assistant", "まとめです。\n\n| 月 | 売上 |\n|---|---|\n| 4月 | 1,200 |\n| 5月 | 1,350 |")["id"]
        plain = self.api.chats.add_message(cid, "assistant", "表のない回答です。\n- 要点1\n- 要点2")["id"]
        for fmt, part in (("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml"), ("pptx", "ppt/presentation.xml")):
            body, response = self.call(f"/api/messages/{table}/export?format={fmt}", raw=True)
            self.assertIn("openxmlformats", response.headers["Content-Type"])
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn(part, zipfile.ZipFile(io.BytesIO(body)).namelist())
        self.assertEqual(self.status_of(f"/api/messages/{plain}/export?format=xlsx"), 400)  # 表がなければ Excel にはできない
        self.assertEqual(self.status_of(f"/api/messages/{plain}/export?format=pptx"), 200)
        self.assertEqual(self.status_of(f"/api/messages/{plain}/export?format=exe"), 400)

    def test_settings_persist_and_validate(self):
        self.call("/api/settings", {"cache": False})
        self.assertFalse(self.app.cfg.cache)
        self.assertEqual(self.status_of("/api/settings", {"home": "x"}), 400)

    def test_background_run_cancel(self):
        slow = MockBackend("mock", lambda model, messages, schema: (time.sleep(0.6), json.dumps({"role": "general"}) if schema else "長い回答")[1],
                           [ModelInfo("small", "mock", int(1e9))])
        self.app.backends["mock"] = slow
        summary = self.call("/api/run", {"task": "ゆっくり答えて", "stream": False})
        self.assertTrue(self.call(f"/api/runs/{summary['run_id']}/cancel", {})["ok"])
        events = [json.loads(line) for line in self.call(f"/api/runs/{summary['run_id']}/events").splitlines()]
        self.assertEqual(events[-1]["type"], "cancelled")
        message = self.api.chats.message(summary["assistant_message_id"])
        self.assertEqual(message["meta"]["status"], "cancelled")
        self.assertIn("停止しました", message["content"])

    def test_agent_approval_from_another_device(self):
        run = self.api.runs.start(task="note.txt にメモを書いて", who="pc", options={"agent": True})
        deadline = time.time() + 10
        approval = None
        while time.time() < deadline and approval is None:
            approval = next((e for e in run.events if e["type"] == "approval"), None)
            time.sleep(0.05)
        self.assertIsNotNone(approval)
        self.assertEqual((approval["tool"], approval["preview"]), ("write_file", "メモ"))
        headers, _ = self.pair()  # 別の端末(スマホ)から許可する
        self.call("/api/approve", {"id": approval["id"], "allow": True}, headers=headers, base=self.remote)
        run.wait(10**9, 10)
        while not run.done:
            time.sleep(0.05)
        self.assertEqual(run.status, "done")
        self.assertEqual((self.workspace / "note.txt").read_text(encoding="utf-8"), "メモ")


class RemoteTest(ServerBase):
    def test_pairing_and_auth(self):
        hello = self.call("/api/hello", base=self.remote)
        self.assertEqual((hello["app"], hello["authenticated"]), ("Celestial", False))
        self.assertEqual(self.status_of("/api/state", base=self.remote), 401)
        self.assertEqual(self.status_of("/api/pair", {"code": "000000", "name": "x"}, base=self.remote), 403)
        self.assertEqual(self.status_of("/api/pairing/start", {}, base=self.remote), 403)  # コードは PC でしか出せない

        headers, result = self.pair()
        self.assertEqual(self.call("/api/state", headers=headers, base=self.remote)["who"], result["device_id"])
        self.assertTrue(self.call("/api/hello", headers=headers, base=self.remote)["authenticated"])
        self.assertEqual(self.status_of("/api/devices", headers=headers, base=self.remote), 403)  # PC 専用
        self.assertEqual(self.status_of("/api/settings", {"tools_run": True}, headers, base=self.remote), 403)  # 安全に関わる設定
        self.assertEqual(self.status_of("/api/settings", {"fewshot_k": 3}, headers, base=self.remote), 200)
        self.assertEqual(self.status_of("/", base=self.remote), 404)  # PC の画面そのものは出さない

        devices = self.call("/api/devices")["devices"]
        self.assertEqual([d["name"] for d in devices], ["Pixel"])
        self.call(f"/api/devices/{devices[0]['id']}/revoke", {})
        self.assertEqual(self.status_of("/api/state", headers=headers, base=self.remote), 401)

    def test_pairing_code_is_single_use(self):
        code = self.call("/api/pairing/start", {})["code"]
        self.call("/api/pair", {"code": code, "name": "a"}, base=self.remote)
        self.assertEqual(self.status_of("/api/pair", {"code": code, "name": "b"}, base=self.remote), 403)

    def test_sync_between_devices(self):
        headers, _ = self.pair()
        now = time.time()
        push = {"conversations": [{"id": "phoneconv0001", "title": "スマホで話した", "created": now, "updated": now}],
                "messages": [{"id": "phonemsg00001", "conversation_id": "phoneconv0001", "role": "user", "content": "質問",
                              "created": now, "updated": now},
                             {"id": "phonemsg00002", "conversation_id": "phoneconv0001", "role": "assistant", "content": "回答",
                              "created": now + 1, "updated": now + 1, "meta": {"model": "local:qwen"}}]}
        self.assertEqual(self.call("/api/sync", push, headers, base=self.remote)["applied"], 3)
        # PC 側でも見える
        conv = self.call("/api/conversations/phoneconv0001")
        self.assertEqual([m["content"] for m in conv["messages"]], ["質問", "回答"])
        # 古い変更は採用しない(後勝ち)
        stale = {"messages": [{**push["messages"][1], "content": "古い", "updated": now - 100}]}
        self.assertEqual(self.call("/api/sync", stale, headers, base=self.remote)["applied"], 0)
        # PC で書いた続きが、スマホに届く
        rev = self.call("/api/sync?since=0", headers=headers, base=self.remote)["rev"]
        self.call("/api/run", {"task": "やあ", "conversation_id": "phoneconv0001"})
        changes = self.call(f"/api/sync?since={rev}", headers=headers, base=self.remote)
        self.assertIn("こんにちは", [m["content"] for m in changes["messages"]])
        self.assertGreater(changes["rev"], rev)

    def test_events_stream(self):
        headers, _ = self.pair()
        port = self.remote_server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events", headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        first = json.loads(response.readline().decode().removeprefix("data: "))
        self.assertEqual(first["type"], "hello")
        self.call("/api/send", {"kind": "text", "text": "PC からのお知らせ"})
        seen = []
        deadline = time.time() + 10
        while time.time() < deadline:
            line = response.readline().decode().strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
                if any(e["type"] == "push" for e in seen):
                    break
        conn.close()
        push = next(e for e in seen if e["type"] == "push")
        self.assertEqual((push["kind"], push["text"]), ("text", "PC からのお知らせ"))

    def test_files_upload_download_range(self):
        headers, _ = self.pair()
        payload = "こんにちは、ファイル".encode("utf-8") * 100
        result = self.call("/api/files/raw?path=" + urllib.request.quote("受信/メモ.txt"), payload,
                           {**headers, "Content-Type": "application/octet-stream"}, base=self.remote, method="PUT")
        self.assertEqual(result["size"], len(payload))
        self.assertEqual((self.workspace / "受信" / "メモ.txt").read_bytes(), payload)
        listing = self.call("/api/files?path=" + urllib.request.quote("受信"), headers=headers, base=self.remote)
        self.assertEqual([e["name"] for e in listing["entries"]], ["メモ.txt"])
        content, r = self.call("/api/files/raw?path=" + urllib.request.quote("受信/メモ.txt"),
                               headers={**headers, "Range": "bytes=3-8"}, base=self.remote, raw=True)
        self.assertEqual((r.status, content), (206, payload[3:9]))
        self.assertEqual(self.status_of("/api/files/raw?path=../secret.txt", headers=headers, base=self.remote), 403)
        # 上書きすると元の内容がバックアップに残る
        self.call("/api/files/raw?path=" + urllib.request.quote("受信/メモ.txt"), b"new",
                  {**headers, "Content-Type": "application/octet-stream"}, base=self.remote, method="PUT")
        backups = list((Path(self.tmp.name) / "backups").rglob("メモ.txt"))
        self.assertEqual([b.read_bytes() for b in backups], [payload])

    def test_chunked_request_body(self):
        # Dart など、本文を分割送信(chunked)で送る相手にも対応する
        code = self.call("/api/pairing/start", {})["code"]
        conn = http.client.HTTPConnection("127.0.0.1", self.remote_server.server_address[1], timeout=10)
        body = json.dumps({"code": code, "name": "分割送信の端末"}).encode()
        conn.putrequest("POST", "/api/pair")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        for i in range(0, len(body), 7):
            piece = body[i:i + 7]
            conn.send(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        conn.send(b"0\r\n\r\n")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("token", json.loads(response.read()))
        conn.close()

    def test_keep_alive_connection_reuse(self):
        # 1本の接続で、本文を読まない要求(取り消し)と読む要求を続けて送っても混ざらない
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", "/api/state")
        first = conn.getresponse()
        self.assertEqual(first.status, 200)
        first.read()
        conn.request("POST", "/api/runs/0123abcd/cancel", body=b'{"x": 1}', headers={"Content-Type": "application/json"})
        cancel = conn.getresponse()
        self.assertEqual((cancel.status, json.loads(cancel.read())), (200, {"ok": False}))
        try:
            conn.request("GET", "/api/conversations")
            again = conn.getresponse()
            self.assertEqual(again.status, 200)
            self.assertIn("conversations", json.loads(again.read()))
        except (ConnectionError, http.client.HTTPException):
            pass  # サーバーが安全のため接続を閉じた場合も正しい(混ざらなければよい)
        conn.close()

    def test_pair_page(self):
        code = self.call("/api/pairing/start", {})["code"]
        page = self.call(f"/pair?c={code}", base=self.remote)
        self.assertIn(code, page)
        self.assertIn("intent://pair?", page)


class ChatStoreTest(unittest.TestCase):
    def test_changes_are_paged_without_gaps(self):
        chats = ChatStore(Store())
        conv = chats.create_conversation("t")
        for i in range(25):
            chats.add_message(conv["id"], "user", f"m{i}")
        chats.update_conversation(conv["id"], deleted=True)  # 1つの rev で全メッセージが変わる
        seen, since = {}, 0
        for _ in range(100):
            page = chats.changes(since, limit=7)
            for msg in page["messages"]:
                seen[msg["id"]] = msg
            since = page["rev"]
            if not page["more"]:
                break
        self.assertEqual(len(seen), 25)
        self.assertTrue(all(m["deleted"] for m in seen.values()))
        self.assertEqual(chats.list_conversations(), [])

    def test_messages_keep_order_even_at_same_time(self):
        chats = ChatStore(Store())
        conv = chats.create_conversation("t")
        from unittest import mock
        with mock.patch("celestial.chats.time.time", return_value=1000.0):  # 時計が進まない(粗い)場合
            ids = [chats.add_message(conv["id"], role, text)["id"] for role, text in
                   [("user", "質問"), ("assistant", "回答"), ("user", "続き"), ("assistant", "続きの回答")]]
        created = [m["created"] for m in chats.messages(conv["id"])]
        self.assertEqual([m["id"] for m in chats.messages(conv["id"])], ids)
        self.assertEqual(created, sorted(created))
        self.assertEqual(len(set(created)), 4)  # 端末側が時刻だけで並べても順番が崩れない

    def test_resync_from_another_database(self):
        chats = ChatStore(Store())
        conv = chats.create_conversation("t")
        chats.add_message(conv["id"], "user", "こんにちは")
        page = chats.changes(0)
        self.assertTrue(page["db"])
        # 端末が別のデータベース(番号が先に進んでいた)と同期していたら、最初から送り直す
        stale = chats.changes(page["rev"] + 500)
        self.assertEqual(len(stale["messages"]), 1)
        self.assertEqual(stale["rev"], page["rev"])
        self.assertNotEqual(ChatStore(Store()).db_id, chats.db_id)

    def test_needs_context(self):
        self.assertFalse(needs_context("Pythonで回文判定の関数を書いて", has_history=False))
        self.assertTrue(needs_context("それを英語にして", has_history=True))
        self.assertTrue(needs_context("もっと短く", has_history=True))
        self.assertFalse(needs_context("次の文章を要約して:\n" + "長い本文。" * 30, has_history=True))
        self.assertFalse(needs_context("東京から大阪までの新幹線の所要時間と、移動の選択肢を比べて詳しく教えてください", has_history=True))


class TitleTest(unittest.TestCase):
    """最初のやり取りのあと、依頼の文の頭(途中で切れた題名)ではなく、内容を表す短い題名を付ける。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.asked = []

        def respond(model, messages, schema):
            if schema and "role" in schema.get("properties", {}):
                return json.dumps({"role": "general"})
            if messages[0]["content"].startswith("会話の一覧に出す短い題名"):
                self.asked.append(messages[-1]["content"])
                return "題名: 「4〜6月の売上表」。"
            return "作成しました。"

        backend = MockBackend("mock", respond, [ModelInfo("small", "mock", int(1e9))])
        self.app = Celestial(Config(home=Path(self.tmp.name)), backends={"mock": backend}, store=Store())
        self.server = make_server(self.app, 0)
        self.api = self.server.api

    def tearDown(self):
        self.server.server_close()
        self.api.flows.stop()
        self.api.flows.store.close()
        self.tmp.cleanup()

    def wait_title(self, cid, expected):
        for _ in range(100):
            if self.api.chats.conversation(cid)["title"] == expected:
                return True
            time.sleep(0.02)
        return False

    def run_task(self, task, cid=None):
        run = self.api.runs.start(task=task, who="pc", conversation_id=cid)
        run.wait(0, 10)
        while not run.done:
            run.wait(len(run.events), 10)
        return run.conversation_id

    def test_names_the_conversation_after_the_first_answer(self):
        task = "作業フォルダに、4月 1,200,000円、5月 1,350,000円、6月 1,280,000円 の売上表を Excel ファイルで作って"
        cid = self.run_task(task)
        self.assertTrue(self.wait_title(cid, "4〜6月の売上表"))  # 前置き・かぎかっこ・句点は除く
        self.assertTrue(self.asked[0].startswith("依頼: 作業フォルダに"))
        self.assertNotIn("作成しました", self.asked[0])  # 回答は渡さない(小さいAIは回答を写してしまう)
        # 2回目のやり取りでは付け直さない
        self.run_task("合計の行も足して", cid)
        time.sleep(0.2)
        self.assertEqual(len(self.asked), 1)

    def test_keeps_a_title_the_user_chose(self):
        conversation = self.api.chats.create_conversation("自分で付けた題名", origin="pc")
        cid = self.run_task("売上表を作って", conversation["id"])
        time.sleep(0.2)
        self.assertEqual(self.api.chats.conversation(cid)["title"], "自分で付けた題名")
        self.assertEqual(self.asked, [])

    def test_clean_title(self):
        from celestial.manager import clean_title
        self.assertEqual(clean_title("タイトル:『会議の議事録』", "会議の議事録をまとめて"), "会議の議事録")
        self.assertEqual(clean_title("<think>考える</think>\n売上の集計", "売上を集計して"), "売上の集計")
        self.assertIsNone(clean_title("Sales report", "売上の報告を書いて"))  # 日本語の依頼に英語の題名
        self.assertIsNone(clean_title("销售报告设计", "売上の報告を書いて"))  # 中国語の題名
        self.assertIsNone(clean_title("あ" * 31, "依頼"))
        self.assertEqual(clean_title("Sales report", "Write a sales report"), "Sales report")
        self.assertIsNone(clean_title("秋の詩", "Write a poem about autumn"))  # 英語の依頼に日本語の題名
        self.assertIsNone(clean_title("作業フォルダにファイルを作成してください", "作業フォルダにファイルを作成して"))  # 文になっている
        self.assertIsNone(clean_title("メールアドレス: tanaka@example.com", "アドレスを抜き出して"))  # データを写している
        self.assertIsNone(clean_title("391", "17 * 23 はいくつ?"))  # 答えを書いている
        from celestial.manager import title_messages
        messages = title_messages("Write a poem")
        self.assertTrue(messages[-1]["content"].startswith("Request: "))  # 依頼の言語に合わせる
        self.assertEqual([m["role"] for m in messages[1:3]], ["user", "assistant"])  # 例は会話の形で渡す


class QrTest(unittest.TestCase):
    def test_decodes(self):
        try:
            import zxingcpp
            from PIL import Image
        except ImportError:
            self.skipTest("QRの読み取りライブラリ(zxing-cpp, Pillow)がありません")
        from celestial.qr import QrCode
        for text in ("http://100.103.100.0:8765/pair?c=123456", "日本語" * 40):
            q = QrCode(text.encode("utf-8"))
            scale, border = 5, 4
            n = q.size + border * 2
            img = Image.new("L", (n * scale, n * scale), 255)
            for y in range(q.size):
                for x in range(q.size):
                    if q.modules[y][x]:
                        img.paste(0, ((x + border) * scale, (y + border) * scale, (x + border + 1) * scale, (y + border + 1) * scale))
            self.assertEqual(zxingcpp.read_barcodes(img)[0].text, text)


if __name__ == "__main__":
    unittest.main()
