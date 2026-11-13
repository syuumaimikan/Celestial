import json
import tempfile
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.tools import ToolContext, ToolError, available_tools, calc, execute, fact_check


class CalcTest(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(calc("17×23"), "391")
        self.assertEqual(calc("1,000 * 0.8 * 1.1"), "880")
        self.assertEqual(calc("2^10"), "1024")
        self.assertEqual(calc("sqrt(144) + 450 / 3 * 7"), "1062")
        self.assertEqual(calc("10 / 4"), "2.5")

    def test_rejects_code(self):
        for bad in ("__import__('os').system('x')", "open('x')", "9**9**9", "a + 1", "1/0"):
            with self.assertRaises(ToolError):
                calc(bad)


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "work"
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "memo.txt").write_text("会議は金曜日の15時\n担当: 佐藤", encoding="utf-8")
        (self.root / "data.bin").write_bytes(b"\x00\x01")
        (Path(self.tmp.name) / "secret.txt").write_text("外の秘密", encoding="utf-8")
        self.ctx = ToolContext(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_list_search(self):
        self.assertIn("docs/", execute(self.ctx, "list_files", {}))
        self.assertIn("金曜日", execute(self.ctx, "read_file", {"path": "docs/memo.txt"}))
        self.assertEqual(execute(self.ctx, "search_files", {"query": "佐藤"}), "docs/memo.txt:2: 担当: 佐藤")
        with self.assertRaises(ToolError):
            execute(self.ctx, "read_file", {"path": "data.bin"})

    def test_cannot_leave_workspace(self):
        for path in ("../secret.txt", str(Path(self.tmp.name) / "secret.txt"), "docs/../../secret.txt"):
            with self.assertRaises(ToolError):
                execute(self.ctx, "read_file", {"path": path})
        with self.assertRaises(ToolError):
            execute(self.ctx, "list_files", {"path": ".."})

    def test_permissions(self):
        self.assertEqual([t.name for t in available_tools(ToolContext())], ["calc"])
        self.assertNotIn("write_file", [t.name for t in available_tools(self.ctx)])
        with self.assertRaises(ToolError):
            execute(self.ctx, "write_file", {"path": "new.txt", "content": "x"})
        with self.assertRaises(ToolError):
            execute(self.ctx, "run_python", {"code": "print(1)"})

        allowed = ToolContext(self.root, allow_write=True, allow_run=True)
        self.assertIn("作成しました", execute(allowed, "write_file", {"path": "out/new.txt", "content": "こんにちは"}))
        self.assertEqual((self.root / "out" / "new.txt").read_text(encoding="utf-8"), "こんにちは")
        # 既存のファイルは上書きできるが、元の内容はバックアップされ、restore_file で戻せる
        allowed.backup_dir = Path(self.tmp.name) / "backups"
        self.assertIn("上書き", execute(allowed, "write_file", {"path": "docs/memo.txt", "content": "新しい内容"}))
        execute(allowed, "edit_file", {"path": "docs/memo.txt", "old": "新しい", "new": "編集した"})
        self.assertEqual((self.root / "docs" / "memo.txt").read_text(encoding="utf-8"), "編集した内容")
        execute(allowed, "delete_file", {"path": "docs/memo.txt"})
        self.assertFalse((self.root / "docs" / "memo.txt").exists())
        execute(allowed, "restore_file", {"path": "docs/memo.txt"})
        self.assertEqual((self.root / "docs" / "memo.txt").read_text(encoding="utf-8"), "編集した内容")
        # 確認が必要な設定では、許可されなければ実行されない
        asked = []
        guarded = ToolContext(self.root, allow_write=True, confirm=True, approve=lambda r: asked.append(r["tool"]) or False)
        with self.assertRaises(ToolError):
            execute(guarded, "write_file", {"path": "denied.txt", "content": "x"})
        self.assertEqual((asked, (self.root / "denied.txt").exists()), (["write_file"], False))
        with self.assertRaises(ToolError):
            execute(allowed, "write_file", {"path": "../escape.txt", "content": "x"})
        self.assertEqual(execute(allowed, "run_python", {"code": "print(6 * 7)"}), "42")


class AgentTest(unittest.TestCase):
    def test_agent_uses_tools_then_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "memo.txt").write_text("合言葉は セレスティアル", encoding="utf-8")
            script = iter([
                {"thought": "読む", "tool": "read_file", "args": {"path": "memo.txt"}},
                {"thought": "外も見たい", "tool": "read_file", "args": {"path": "../x.txt"}},
                {"thought": "計算", "tool": "calc", "args": {"expression": "17*23"}},
                {"thought": "分かった", "tool": "answer", "answer": "合言葉はセレスティアル、17×23は391です。"},
            ])
            seen = []

            def responder(model, messages, schema):
                if schema and "anyOf" in schema:
                    seen.append(messages[-1]["content"])
                    return json.dumps(next(script), ensure_ascii=False)
                return json.dumps({"role": "general"})

            backend = MockBackend("mock", responder, [ModelInfo("small", "mock", int(1e9))])
            app = Celestial(Config(home=Path(tmp), workspace=tmp), backends={"mock": backend}, store=Store())
            stages = []
            result = app.run("memo.txt の合言葉と 17×23 を教えて", tools=True, on_stage=lambda s, d: stages.append((s, d.get("tool"))))

            self.assertTrue(result.text.endswith("17×23は391です。"))
            self.assertEqual([(c["name"], c["ok"]) for c in result.tool_calls], [("read_file", True), ("read_file", False), ("calc", True)])
            self.assertIn("合言葉は セレスティアル", seen[1])   # 道具の結果がAIに渡っている
            self.assertIn("作業フォルダの外", seen[2])           # 拒否された理由も渡っている
            self.assertIn("391", seen[3])
            self.assertEqual([s[1] for s in stages if s[0] == "agent_step"], ["read_file", "read_file", "calc"])
            self.assertEqual(result.source, "agent")
            # 道具を使った回答はキャッシュされない(ファイルの中身は変わりうる)
            self.assertIsNone(app.store.cache_lookup("memo.txt の合言葉と 17×23 を教えて"))

    def test_false_claims_are_corrected_from_the_record(self):
        ctx = ToolContext(Path("."), allow_write=False)
        task = "summary.txt に「テスト」と書いて保存して"
        note = fact_check(task, "I have created a file named 'summary.txt'.", [], ctx)
        self.assertIn("ファイルの作成・変更は行われていません", note)
        self.assertIn("許可されていない", note)
        self.assertIn("行われていません", fact_check(task, "summary.txt を作成しました。", [], ctx))
        # 実際に作成に成功していれば訂正しない。依頼が作成でなければ、文中の「作成しました」には反応しない
        self.assertIsNone(fact_check(task, "作成しました。", [{"name": "write_file", "ok": True}], ctx))
        self.assertIsNone(fact_check("議事録を要約して", "田中さんが資料を作成しました。", [], ctx))
        self.assertIn("実行されていません", fact_check("このコードを実行して", "実行結果は 42 です。", [], ctx))

        with tempfile.TemporaryDirectory() as tmp:
            responder = lambda model, messages, schema: (
                json.dumps({"thought": "x", "tool": "answer", "answer": "summary.txt を作成しました。"}) if schema and "anyOf" in schema
                else json.dumps({"role": "general"}))
            backend = MockBackend("mock", responder, [ModelInfo("small", "mock", int(1e9))])
            app = Celestial(Config(home=Path(tmp), workspace=tmp), backends={"mock": backend}, store=Store())
            result = app.run(task, tools=True)
            self.assertTrue(result.text.startswith("【実行記録による訂正】"))
            self.assertEqual(list(Path(tmp).glob("summary.txt")), [])

    def test_workspace_setting_validated(self):
        app = Celestial(Config(home=Path("unused")), backends={"mock": MockBackend("mock")}, store=Store())
        with self.assertRaises(ValueError):
            app.set_setting("workspace", "Z:/存在しない/フォルダ")
        app.set_setting("workspace", "")
        self.assertIsNone(app.tool_context().workspace)


class SemanticMemoryTest(unittest.TestCase):
    """言い回しが違っても意味が同じ例を、埋め込みで拾えること。"""

    @staticmethod
    def embedder(text):
        # 意味を表す2次元の模擬ベクトル: [重複除去の話か, 天気の話か]
        return [1.0 if ("重複" in text or "同じ要素" in text) else 0.0, 1.0 if "天気" in text else 0.0, 0.05]

    def make(self, **cfg):
        responder = lambda model, messages, schema: json.dumps({"role": "code"}) if schema else "回答"
        backend = MockBackend("mock", responder, [ModelInfo("small", "mock", int(1e9))], embedder=self.embedder)
        return Celestial(Config(home=Path("unused"), cache=False, **cfg), backends={"mock": backend}, store=Store()), backend

    def test_semantic_search_finds_paraphrase(self):
        app, backend = self.make()
        first = app.run("Pythonでリストの重複を取り除く関数を書いて")
        app.feedback(first.id, good=True)
        second = app.run("配列から同じ要素を消すコードがほしい")  # 文字の重なりはほぼ無いが、意味は同じ
        self.assertEqual((second.examples_used, app.memory.last_mode), (1, "semantic"))
        third = app.run("今日の天気を表示するコードを書いて")
        self.assertEqual(third.examples_used, 0)

        calls = backend.embed_calls
        app.run("配列から同じ要素を消すコードがほしい")
        self.assertEqual(backend.embed_calls, calls)  # 計算済みのベクトルは保存されていて、再計算しない

    def test_falls_back_to_ngram(self):
        app, _ = self.make(semantic_search=False)
        first = app.run("Pythonでリストの重複を取り除く関数を書いて")
        app.feedback(first.id, good=True)
        self.assertEqual(app.run("配列から同じ要素を消すコードがほしい").examples_used, 0)
        self.assertEqual(app.memory.last_mode, "ngram")
        self.assertEqual(app.run("Pythonでリストの重複を取り除く関数を作って").examples_used, 1)


if __name__ == "__main__":
    unittest.main()
