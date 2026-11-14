import json
import tempfile
import unittest
from unittest import mock
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

    def test_symbolic_and_units(self):
        self.assertEqual(calc("solve(x**2 - 5*x + 6 = 0)"), "x = 2, 3")
        self.assertEqual(calc("x + y = 10, x - y = 2"), "x = 6, y = 4")
        self.assertEqual(calc("diff(x**3 + 2*x, x)"), "3*x**2 + 2")
        self.assertEqual(calc("integrate(x**2, (x, 0, 3))"), "9")
        self.assertEqual(calc("factor(x**2 - 9)"), "(x - 3)*(x + 3)")
        self.assertEqual(calc("25 C to F"), "77 F")
        self.assertIn("62.137", calc("100 km to mile"))
        self.assertEqual(calc("mean(10, 20, 30)"), "20")


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
        # 名前でも探せる(中身の空の画像を IMG_2023_* で探して「見つかりませんでした」になっていた。実際にあった)
        photos = self.root / "photos"
        photos.mkdir()
        for name in ("IMG_2023_01.jpg", "IMG_2023_07.jpg", "IMG_2024_03.jpg"):
            (photos / name).write_bytes(b"")
        found = execute(self.ctx, "search_files", {"query": "IMG_2023_*", "path": "photos"})
        self.assertIn("photos/IMG_2023_01.jpg", found)
        self.assertIn("photos/IMG_2023_07.jpg", found)
        self.assertNotIn("2024", found)
        self.assertIn("memo.txt", execute(self.ctx, "search_files", {"query": "memo"}))
        # 一覧には、中のフォルダのファイルの数と名前も添える(「photos/」だけでは中が分からなかった。実際にあった)
        listed = execute(self.ctx, "list_files", {})
        self.assertIn("photos/  (ファイル 3 件: IMG_2023_01.jpg, IMG_2023_07.jpg, IMG_2024_03.jpg)", listed)
        # フォルダをそのまま移そうとしたら、中の名前と、まとめて移すパターンを返す(「ファイルがありません」だった)
        with self.assertRaises(ToolError) as folder:
            execute(ToolContext(self.root, allow_write=True), "copy_file", {"path": "photos/", "to": "photos/2023"})
        self.assertIn("photos/*2023*", str(folder.exception))
        self.assertIn("IMG_2023_01.jpg", str(folder.exception))
        # 写したときは、元のファイルも残っていると添える(分けるつもりで写していた)
        copied = execute(ToolContext(self.root, allow_write=True), "copy_file", {"path": "photos/IMG_2024_03.jpg", "to": "photos/copy"})
        self.assertIn("move_file", copied)
        (self.root / "photos" / "copy" / "IMG_2024_03.jpg").unlink()
        (self.root / "photos" / "copy").rmdir()
        # path に2つ並べて移す(「ファイルがありません」になっていた。実際にあった): まとめて to のフォルダへ
        writer = ToolContext(self.root, allow_write=True)
        out = execute(writer, "move_file", {"path": "photos/IMG_2023_01.jpg photos/IMG_2023_07.jpg", "to": "photos/2023"})
        self.assertIn("2 件を移動しました", out)
        self.assertEqual(sorted(p.name for p in (photos / "2023").iterdir()), ["IMG_2023_01.jpg", "IMG_2023_07.jpg"])
        self.assertTrue((photos / "IMG_2024_03.jpg").is_file())
        with self.assertRaises(ToolError):  # どれかが無ければ、今までどおり「ありません」
            execute(writer, "move_file", {"path": "photos/IMG_2024_03.jpg photos/nothing.jpg", "to": "photos/2024"})
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
        with tempfile.TemporaryDirectory() as tmp:  # 空にすると、既定の作業フォルダ(無ければ作る)を使う
            default = Path(tmp) / "Celestial"
            with mock.patch.object(Celestial, "default_workspace", staticmethod(lambda: default)):
                app.set_setting("workspace", "")
                self.assertEqual(app.tool_context().workspace, default)
                self.assertTrue(default.is_dir())


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


class ExtendedToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_calc_csv_sqlite_regex_archive_chart_and_net_guards(self):
        from celestial.knowledge import KnowledgeBase
        from celestial.tools import ToolContext, ToolError, available_tools, execute

        # デフォルトの最小権限では calc のみ
        self.assertEqual([t.name for t in available_tools(ToolContext())], ["calc"])

        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=self.dir / ".backup")
        self.assertEqual(execute(ctx, "calc", {"expression": "comb(6, 2)"}), "15")
        self.assertEqual(execute(ctx, "calc", {"expression": "lcm(12, 18)"}), "36")

        # CSV SQL 検索 + 書き込み系 SQL 拒否
        (self.dir / "sales.csv").write_text("item,price\nApple,120\nOrange,80\nApple,150\n", encoding="utf-8")
        info = execute(ctx, "csv_query", {"path": "sales.csv", "sql": ""})
        self.assertIn("item, price", info)
        agg = execute(ctx, "csv_query", {"path": "sales.csv", "sql": "SELECT item, SUM(price) AS tot FROM t GROUP BY item ORDER BY tot DESC"})
        self.assertIn("270", agg)
        with self.assertRaises(ToolError):
            execute(ctx, "csv_query", {"path": "sales.csv", "sql": "DROP TABLE t"})

        # SQLite read-only 検索 + 変更系 SQL 拒否
        import sqlite3
        db_p = self.dir / "data.db"
        con = sqlite3.connect(db_p)
        con.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        con.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob')")
        con.commit()
        con.close()
        self.assertIn("users (2 行)", execute(ctx, "sqlite_query", {"path": "data.db", "sql": ""}))
        self.assertIn("Alice", execute(ctx, "sqlite_query", {"path": "data.db", "sql": "SELECT name FROM users WHERE id = 1"}))
        with self.assertRaises(ToolError):
            execute(ctx, "sqlite_query", {"path": "data.db", "sql": "DELETE FROM users"})

        # regex_replace (preview -> apply -> restore)
        (self.dir / "notes.txt").write_text("foo=1\nfoo=2\n", encoding="utf-8")
        preview = execute(ctx, "regex_replace", {"path": "notes.txt", "pattern": r"foo=(\d+)", "replacement": r"bar=\1", "dry_run": "true"})
        self.assertIn("プレビュー", preview)
        self.assertIn("foo=1", (self.dir / "notes.txt").read_text(encoding="utf-8"))
        applied = execute(ctx, "regex_replace", {"path": "notes.txt", "pattern": r"foo=(\d+)", "replacement": r"bar=\1"})
        self.assertIn("2 箇所", applied)
        self.assertEqual((self.dir / "notes.txt").read_text(encoding="utf-8").strip(), "bar=1\nbar=2")

        # archive pack/list/unpack + zip-slip 拒否
        sub = self.dir / "pkg"
        sub.mkdir()
        (sub / "a.txt").write_text("hello", encoding="utf-8")
        execute(ctx, "archive", {"action": "pack", "path": "pkg", "to": "pkg.zip"})
        self.assertIn("a.txt", execute(ctx, "archive", {"action": "list", "path": "pkg.zip"}))
        execute(ctx, "archive", {"action": "unpack", "path": "pkg.zip", "to": "out_pkg"})
        self.assertEqual((self.dir / "out_pkg" / "a.txt").read_text(encoding="utf-8"), "hello")

        import zipfile
        evil = self.dir / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../escape.txt", "bad")
        with self.assertRaises(ToolError):
            execute(ctx, "archive", {"action": "unpack", "path": "evil.zip", "to": "safe_out"})

        # make_chart HTML line / SVG pie
        execute(ctx, "make_chart", {"path": "trend.html", "kind": "line", "title": "推移", "data": "月,値\n1月,10\n2月,30"})
        html = (self.dir / "trend.html").read_text(encoding="utf-8")
        self.assertIn("<svg", html)
        self.assertIn("<path ", html)
        self.assertIn("<circle ", html)

        # net SSRF guard
        net_ctx = ToolContext(workspace=self.dir, allow_net=True)
        with self.assertRaises(ToolError):
            execute(net_ctx, "http_request", {"url": "http://127.0.0.1:8765/api/state"})

        # kb_add / kb_search / save_memory ツール連携
        saved: list[str] = []
        kb = KnowledgeBase(Store())
        full_ctx = ToolContext(
            workspace=self.dir, kb=kb,
            memorize=lambda a, f="": (saved.append(f) or f"覚えました: {f}") if a == "save" else ", ".join(saved),
        )
        execute(full_ctx, "kb_add", {"title": "仕様書", "content": "ポート番号は8765です"})
        self.assertIn("8765", execute(full_ctx, "kb_search", {"query": "ポート"}))
        self.assertIn("覚えました", execute(full_ctx, "save_memory", {"action": "save", "fact": "好きな色は青"}))
        self.assertEqual(saved, ["好きな色は青"])


class ExtendedCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tool_gating(self):
        self.assertEqual([t.name for t in available_tools(ToolContext())], ["calc"])
        net_only = {t.name for t in available_tools(ToolContext(workspace=self.dir, allow_net=True, allow_write=False))}
        self.assertIn("web_search", net_only)
        self.assertIn("web_extract", net_only)
        self.assertIn("code_check", net_only)
        self.assertIn("scan_barcode", net_only)
        self.assertNotIn("download_file", net_only)
        net_write = {t.name for t in available_tools(ToolContext(workspace=self.dir, allow_net=True, allow_write=True))}
        self.assertIn("download_file", net_write)

    def test_code_check_and_apply_patch(self):
        backup = self.dir / "_bk"
        (self.dir / "ok.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.dir / "bad.py").write_text("def broken(\n", encoding="utf-8")
        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=backup, changes={})
        self.assertIn("問題は見つかりませんでした", execute(ctx, "code_check", {"path": "ok.py"}))
        self.assertIn("構文エラー", execute(ctx, "code_check", {"path": "bad.py"}))

        diff = (
            "--- a/ok.py\n"
            "+++ b/ok.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return (a + b) * 2\n"
            "--- /dev/null\n"
            "+++ b/created.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+hello\n"
        )
        res = execute(ctx, "apply_patch", {"patch": diff})
        self.assertIn("更新: ok.py", res)
        self.assertIn("作成: created.txt", res)
        self.assertIn("(a + b) * 2", (self.dir / "ok.py").read_text(encoding="utf-8"))
        self.assertEqual((self.dir / "created.txt").read_text(encoding="utf-8"), "hello\n")
        self.assertIn("ok.py", ctx.changes)
        # 無効なハンクで失敗してもファイルは壊さない
        with self.assertRaises(ToolError):
            execute(ctx, "apply_patch", {"patch": "--- a/ok.py\n+++ b/ok.py\n@@ -1,1 +1,1 @@\n-nonexistent\n+x\n"})

    def test_convert_image_and_barcode_tools(self):
        ctx = ToolContext(workspace=self.dir, allow_write=True, changes={})
        (self.dir / "data.csv").write_text("name,score\nAlice,95\nBob,88\n", encoding="utf-8")
        execute(ctx, "convert_file", {"path": "data.csv", "to": "data.json"})
        loaded = json.loads((self.dir / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded[0]["name"], "Alice")
        execute(ctx, "convert_file", {"path": "data.json", "to": "data.yaml"})
        execute(ctx, "convert_file", {"path": "data.yaml", "to": "report.md"})
        execute(ctx, "convert_file", {"path": "report.md", "to": "report.html"})
        execute(ctx, "convert_file", {"path": "report.md", "to": "report.docx"})
        self.assertIn("<table>", (self.dir / "report.html").read_text(encoding="utf-8"))
        self.assertTrue((self.dir / "report.docx").is_file())

        # QR / Barcode 生成 + 読み取り
        execute(ctx, "qr_barcode", {"text": "https://celestial.test/ok", "path": "qr.svg", "format": "qr"})
        self.assertIn("<svg", (self.dir / "qr.svg").read_text(encoding="utf-8"))
        execute(ctx, "qr_barcode", {"text": "CELESTIAL-2026", "path": "qr.png", "format": "qr"})
        scanned = execute(ctx, "scan_barcode", {"path": "qr.png"})
        self.assertIn("CELESTIAL-2026", scanned)

        # 画像編集 (resize, watermark, collage, PDF変換, info)
        from PIL import Image
        Image.new("RGB", (200, 120), (30, 120, 200)).save(self.dir / "img1.png")
        Image.new("RGB", (200, 120), (200, 80, 40)).save(self.dir / "img2.png")
        execute(ctx, "image_edit", {"action": "resize", "path": "img1.png", "to": "small.jpg", "width": "100"})
        info = json.loads(execute(ctx, "image_edit", {"action": "info", "path": "small.jpg"}))
        self.assertEqual((info["width"], info["height"]), (100, 60))
        execute(ctx, "image_edit", {"action": "watermark", "path": "small.jpg", "to": "wm.pdf", "text": "CONFIDENTIAL"})
        self.assertTrue((self.dir / "wm.pdf").read_bytes().startswith(b"%PDF"))
        execute(ctx, "image_edit", {"action": "collage", "path": "img1.png", "extras": "img2.png", "to": "grid.png"})
        self.assertTrue((self.dir / "grid.png").is_file())

    def test_web_search_extract_download_and_pc_mocks(self):
        ctx = ToolContext(workspace=self.dir, allow_net=True, allow_write=True)
        html_page = (
            b"<html><head><title>Sample</title><meta name='description' content='Desc'></head>"
            b"<body><h1>Top</h1><a href='/doc'>Guide</a><table><tr><th>K</th><th>V</th></tr><tr><td>A</td><td>1</td></tr></table></body></html>"
        )
        with mock.patch("celestial.web._get", return_value=(html_page, "text/html; charset=utf-8")):
            links = execute(ctx, "web_extract", {"url": "https://example.com/start", "mode": "links"})
            self.assertIn("https://example.com/doc", links)
            tables = execute(ctx, "web_extract", {"url": "https://example.com/start", "mode": "tables"})
            self.assertIn("| K | V |", tables)
            meta = execute(ctx, "web_extract", {"url": "https://example.com/start", "mode": "meta"})
            self.assertIn("タイトル: Sample", meta)

        def fake_get(url, timeout=15):
            if "api.duckduckgo.com" in url:
                return json.dumps({"Heading": "Python", "AbstractText": "Language", "AbstractURL": "https://python.org"}).encode(), "application/json"
            return json.dumps({"query": {"search": [{"title": "PythonWiki", "snippet": "<b>Prog</b>"}]}}).encode(), "application/json"

        with mock.patch("celestial.web._get", side_effect=fake_get):
            res = execute(ctx, "web_search", {"query": "Python", "lang": "en", "limit": "5"})
            self.assertIn("https://python.org", res)
            self.assertIn("PythonWiki", res)

        with self.assertRaises(ToolError):
            execute(ctx, "download_file", {"url": "http://127.0.0.1/secret.txt", "path": "s.txt"})

        pc_ctx = ToolContext(workspace=self.dir, allow_pc=True, allow_write=True)
        with mock.patch("celestial.pc.speak", return_value="spoken") as m_speak, \
             mock.patch("celestial.pc.notify_desktop", return_value="notified") as m_notify, \
             mock.patch("celestial.pc.network_info", return_value="IPv4: 192.168.1.10") as m_net:
            if available_tools(pc_ctx) and any(t.name == "speak" for t in available_tools(pc_ctx)):
                self.assertEqual(execute(pc_ctx, "speak", {"text": "こんにちは", "path": "out.wav"}), "spoken")
                m_speak.assert_called_once()
                self.assertEqual(execute(pc_ctx, "notify_pc", {"title": "T", "text": "M"}), "notified")
                m_notify.assert_called_once_with("T", "M")
                self.assertIn("192.168.1.10", execute(pc_ctx, "network_info", {}))
                m_net.assert_called_once()


if __name__ == "__main__":
    unittest.main()
