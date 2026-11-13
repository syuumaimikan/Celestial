"""Codex や Claude Code のような機能(フォルダの中の編集・差分の表示・プロジェクトの作成)の試験。"""

import json
import tempfile
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.project import clean_plan, create_project, looks_like_project_request, report, strip_fences, syntax_error
from celestial.tools import ToolContext, ToolError, execute, summarize_changes


class ChangesTest(unittest.TestCase):
    def test_changes_are_recorded_with_diffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('a')\nprint('b')\n", encoding="utf-8")
            (root / "old.txt").write_text("消す\n", encoding="utf-8")
            ctx = ToolContext(workspace=root, allow_write=True, backup_dir=root / ".bk", changes={})
            execute(ctx, "edit_file", {"path": "app.py", "old": "print('b')", "new": "print('B')\nprint('c')"})
            execute(ctx, "edit_file", {"path": "app.py", "old": "print('c')", "new": "print('C')"})  # 2回目も最初の内容と比べる
            execute(ctx, "write_file", {"path": "src/new.py", "content": "x = 1\n"})
            execute(ctx, "delete_file", {"path": "old.txt"})
            execute(ctx, "make_excel", {"path": "表", "data": "a,b\n1,2"})
            changes = {c["path"]: c for c in summarize_changes(ctx)}
            self.assertEqual(changes["app.py"]["status"], "modified")
            self.assertEqual((changes["app.py"]["added"], changes["app.py"]["removed"]), (2, 1))
            self.assertIn("+print('C')", changes["app.py"]["diff"])
            self.assertEqual((changes["src/new.py"]["status"], changes["src/new.py"]["added"]), ("added", 1))
            self.assertEqual(changes["old.txt"]["status"], "deleted")
            self.assertTrue(changes["表.xlsx"]["binary"])  # 文字でないファイルは差分を出さない

    def test_line_endings_are_kept(self):
        # Windows の CRLF のファイルを編集・追記しても、改行が \r\r\n に壊れず、複数行の old も見つかる
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
            (root / "lf.txt").write_bytes(b"a\nb\n")
            ctx = ToolContext(workspace=root, allow_write=True)
            execute(ctx, "edit_file", {"path": "crlf.txt", "old": "a\nb", "new": "A\nB\nB2"})
            execute(ctx, "append_file", {"path": "crlf.txt", "content": "d\n"})
            execute(ctx, "append_file", {"path": "lf.txt", "content": "c\r\n"})
            execute(ctx, "write_file", {"path": "lf.txt", "content": "x\r\ny\n"})
            self.assertEqual((root / "crlf.txt").read_bytes(), b"A\r\nB\r\nB2\r\nc\r\nd\r\n")
            self.assertEqual((root / "lf.txt").read_bytes(), b"x\ny\n")


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.approvals = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_intent(self):
        for text in ("Pythonで家計簿のCLIアプリを作って", "簡単なタイマーのウェブページを作成して", "Make a todo app in Python"):
            self.assertTrue(looks_like_project_request(text), text)
        for text in ("このアプリのバグを直して", "app.py に関数を追加して", "アプリの作り方を教えて", "今日の天気は?"):
            self.assertFalse(looks_like_project_request(text), text)

    def test_plan_is_checked(self):
        plan = clean_plan({"folder": "Todo App!", "title": "ToDo", "files": [{"path": "../外.py", "purpose": "x"},
                                                                             {"path": "README.md", "purpose": "説明"},
                                                                             {"path": "todo.py", "purpose": "本体"}], "run": "python todo.py", "test": ""})
        self.assertEqual(plan["folder"], "todo-app")
        self.assertEqual([f["path"] for f in plan["files"]], ["todo.py", "README.md"])  # 外を指す名前は除き、README は最後
        with self.assertRaises(ValueError):
            clean_plan({"files": []})
        self.assertEqual(strip_fences("```python\nprint(1)\n```"), "print(1)\n")
        self.assertIn("SyntaxError", syntax_error("a.py", "def f(:\n"))
        self.assertIsNone(syntax_error("a.py", "def f():\n    return 1\n"))

    def test_create_check_and_fix(self):
        plan = {"folder": "calc", "title": "計算", "files": [{"path": "calc.py", "purpose": "add 関数"},
                                                           {"path": "tests/test_calc.py", "purpose": "add のテスト"}],
                "run": "python calc.py", "test": "python -m unittest discover -s tests"}
        versions = {"calc.py": ["def add(a, b):\n    return a - b\n", "def add(a, b):\n    return a + b\n"]}
        prompts = []

        def write(prompt):
            prompts.append(prompt)
            path = prompt.rsplit("Write the complete content of `", 1)[1].split("`")[0]
            if path == "calc.py":
                return versions["calc.py"].pop(0) if len(versions["calc.py"]) > 1 else versions["calc.py"][0]
            if path.startswith("tests/"):
                return ("```python\nimport sys, unittest\nsys.path.insert(0, '.')\nfrom calc import add\n\n"
                        "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n```")
            return "# 計算\n使い方: python calc.py\n"

        ctx = ToolContext(workspace=self.root, allow_write=True, allow_run=True, backup_dir=self.root / ".bk", confirm=True,
                          approve=lambda request: self.approvals.append(request) or True, changes={})
        (self.root / "calc").mkdir()  # 同じ名前のフォルダがあれば、別の名前にする
        result = create_project(request="足し算の関数とテストを作って", ctx=ctx, plan_fn=lambda r: plan, write_fn=write)
        self.assertEqual(result["folder"], "calc-2")
        self.assertEqual(len(self.approvals), 1)  # 許可はまとめて1回
        self.assertIn("python -m unittest", self.approvals[0]["preview"])
        self.assertTrue(result["test"]["ok"], result["test"]["output"])  # 1回目は引き算でテストに落ち、直して合格
        self.assertEqual((self.root / "calc-2" / "calc.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")
        self.assertTrue(any("has a problem" in p for p in prompts))  # 失敗の出力を見せて直させた
        self.assertTrue((self.root / "calc-2" / "README.md").is_file())  # README は計画になくても作る
        self.assertIn("テスト(`python -m unittest discover -s tests`)にも合格", report(result))
        changes = {c["path"] for c in summarize_changes(ctx)}
        self.assertEqual(changes, {"calc-2/calc.py", "calc-2/tests/test_calc.py", "calc-2/README.md"})

    def test_needs_permission(self):
        ctx = ToolContext(workspace=self.root, allow_write=False)
        with self.assertRaises(ToolError):
            create_project(request="x", ctx=ctx, plan_fn=lambda r: {}, write_fn=lambda p: "")


class AgentProjectTest(unittest.TestCase):
    def test_agent_routes_project_requests_and_reports_changes(self):
        def respond(model, messages, schema):
            if schema and "role" in schema.get("properties", {}):
                return json.dumps({"role": "code"})
            if schema and "files" in schema.get("properties", {}):
                return json.dumps({"folder": "hello", "title": "挨拶", "files": [{"path": "hello.py", "purpose": "挨拶を表示"}],
                                   "run": "python hello.py", "test": ""})
            if "Write the complete content of `hello.py`" in messages[-1]["content"]:
                return "print('こんにちは')\n"
            return "# 挨拶\n"

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "work" / "既存").mkdir(parents=True)
            backend = MockBackend("mock", respond, [ModelInfo("coder", "mock", int(4e9))])
            app = Celestial(Config(home=home, workspace=str(home / "work"), tools_write=True, tools_confirm=False),
                            backends={"mock": backend}, store=Store())
            stages = []
            result = app.run("Pythonで挨拶を表示するアプリを作って", agent=True, on_stage=lambda s, d: stages.append((s, d)))
            self.assertEqual(result.route_source, "project")
            self.assertEqual((home / "work" / "hello" / "hello.py").read_text(encoding="utf-8"), "print('こんにちは')\n")
            self.assertEqual({c["path"] for c in result.changes}, {"hello/hello.py", "hello/README.md"})
            self.assertIn(("project", {"phase": "file", "path": "hello.py", "n": 1, "total": 2}), stages)
            # 作業するフォルダは、作業フォルダの中だけ選べる
            self.assertEqual(app.work_folder("既存"), (home / "work" / "既存").resolve())
            with self.assertRaises(ValueError):
                app.work_folder("../")


if __name__ == "__main__":
    unittest.main()
