"""Codex や Claude Code のような機能(フォルダの中の編集・差分の表示・プロジェクトの作成)の試験。"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.codecheck import undefined_names
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


class CodeCheckTest(unittest.TestCase):
    """動かす前に分かる誤り(定義していない名前)。見逃しても害の少ない側に倒す(誤って知らせない)。"""

    FLASK = "from flask import Flask\n\n@app.route('/')\ndef index():\n    return 'hi'\n"

    def test_undefined_names(self):
        self.assertEqual(undefined_names(self.FLASK), [("app", 3)])       # app = Flask(__name__) の書き忘れ(実際にあった)
        self.assertIn("NameError", syntax_error("app.py", self.FLASK))     # 書いた直後の書き直し(1回)の材料になる
        fine = ("import os, json as j\nfrom pathlib import Path\nX = [y for y in range(3)]\n"
                "def f(a, *args, b=1, **kw):\n    global G\n    G = a\n    try:\n        return j.dumps(kw) + os.sep + str(Path('.'))\n"
                "    except ValueError as e:\n        return str(e) + __name__\n"
                "class C:\n    def m(self):\n        return C, G, X, print, len\n"
                "match X:\n    case [first, *rest]:\n        print(first, rest)\n    case {'k': v, **others}:\n        print(v, others)\n")
        self.assertEqual(undefined_names(fine), [])
        if sys.version_info >= (3, 12):  # 型引数と、クラスの中で使える名前
            typed = ("def first[T](items: list[T]) -> T:\n    return items[0]\ntype Pair[K] = tuple[K, K]\n"
                     "class A:\n    label = __qualname__ + __module__\n")
            self.assertEqual(undefined_names(typed), [])
        self.assertEqual(undefined_names("from tkinter import *\nTk()\n"), [])   # * の import は分からないので知らせない
        self.assertEqual(undefined_names("globals().update(RED=1)\nprint(RED)\n"), [])  # 見えない形で作った名前も同じ
        self.assertEqual(undefined_names("def broken(:\n"), [])                  # 構文の誤りは compile のほうで知らせる

    def test_written_file_gets_a_note_and_edit_tells_how_to_add_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            written = execute(ctx, "write_file", {"path": "app.py", "content": self.FLASK})
            self.assertIn("app(3 行目)", written)
            self.assertIn("app = Flask(__name__)", written)
            with self.assertRaises(ToolError) as e:  # 無い行を「置き換え」ようとした(実際にあった): 行の足し方を伝える
                execute(ctx, "edit_file", {"path": "app.py", "old": "app = Flask(__name__)", "new": "app = Flask(__name__)\n"})
            self.assertIn("足したい場所のすぐ前にある行", str(e.exception))
            with self.assertRaises(ToolError) as same:  # 何も変えない編集は「編集しました」にしない(実際にあった)
                execute(ctx, "edit_file", {"path": "app.py", "old": "def index():", "new": "def index():"})
            self.assertIn("何も変わりません", str(same.exception))
            fixed = execute(ctx, "edit_file", {"path": "app.py", "old": "from flask import Flask",
                                               "new": "from flask import Flask\n\napp = Flask(__name__)"})
            self.assertNotIn("注意", fixed)
            self.assertNotIn("注意", execute(ctx, "write_file", {"path": "notes.txt", "content": "app"}))

    def test_fixes_that_can_be_copied_as_they_are(self):
        # 小さいAIは、直し方の説明より、そのまま写せる old と new のほうが確実に直せる(説明だけでは直せず、同じ誤った編集を
        # 4回繰り返した)。挙げた直し方を順に使うと、定義していない名前が残らない
        from celestial.codecheck import suggest_fixes
        cases = [self.FLASK.replace("from flask import Flask\n", "from flask import Flask\nimport os\n"),
                 "from flask import Flask\napp = Flask(__name__)\n@app.route('/')\ndef i():\n"
                 "    return render_template('index.html', now=datetime.now(), t=time.time())\n",
                 "import os\nfrom flask import Flask, jsonify\nimport json\n\n@app.route('/')\ndef i():\n    return render_template('a.html')\n",
                 "# flask の画面\n@app.route('/')\ndef i():\n    return jsonify(os.getcwd())\n"]
        for code in cases:
            with self.subTest(code=code[:30]):
                fixes = suggest_fixes(code)
                self.assertTrue(fixes)
                for fix in fixes:
                    self.assertEqual(code.count(fix["old"]), 1)               # old は1か所に決まる
                    code = code.replace(fix["old"], fix["new"])
                self.assertEqual(undefined_names(code), [])
                compile(code, "app.py", "exec")
        self.assertIn("from datetime import datetime", suggest_fixes(cases[1])[0]["new"])  # datetime.now() の使い方に合わせる
        self.assertEqual(suggest_fixes("print(mystery_value)\n"), [])                     # 直し方が決まらないものは挙げない
        with tempfile.TemporaryDirectory() as tmp:  # 書いた直後の注意に、フォルダの中の道のりつきで添える(CRLF のファイルでも)
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            (Path(tmp) / "web").mkdir()
            (Path(tmp) / "web" / "app.py").write_bytes(cases[0].replace("\n", "\r\n").encode("utf-8"))
            note = execute(ctx, "edit_file", {"path": "web/app.py", "old": "def index():", "new": "def home():"})
            line = next(line for line in note.splitlines() if line.startswith("edit_file "))
            fix = json.loads(line[len("edit_file "):])
            self.assertEqual(fix["path"], "web/app.py")
            self.assertNotIn("注意", execute(ctx, "edit_file", fix))

    def test_template_path_with_templates_folder_is_caught_when_written(self):
        # render_template('templates/index.html') は templates/templates/index.html を探して見つからない。except で受けると
        # 「Error fetching weather data」だけのページになり、原因が見えなかった(実際にあった)。書いた時点で直し方を見せる
        from celestial.tools import CODE_PROBLEM
        code = ("from flask import Flask, render_template\n\napp = Flask(__name__)\n"
                "template_file = 'templates/index.html'\n\n@app.route('/')\ndef index():\n"
                "    try:\n        return render_template(template_file, now=1)\n"
                "    except Exception as e:\n        print(f'Error: {e}')\n        return 'Error fetching weather data'\n")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            note = execute(ctx, "write_file", {"path": "app.py", "content": code})
            self.assertIn(CODE_PROBLEM, note)
            self.assertIn("TemplateNotFound", note)
            line = next(line for line in note.splitlines() if line.startswith("edit_file "))
            self.assertNotIn(CODE_PROBLEM, execute(ctx, "edit_file", json.loads(line[len("edit_file "):])))
            self.assertIn("template_file = 'index.html'", (Path(tmp) / "app.py").read_text(encoding="utf-8"))
            # templates の場所を変えている・本当に templates/templates/ の中にある、なら誤りではない
            moved = code.replace("Flask(__name__)", "Flask(__name__, template_folder='.')")
            self.assertNotIn(CODE_PROBLEM, execute(ctx, "write_file", {"path": "b.py", "content": moved}))
            (Path(tmp) / "templates" / "templates").mkdir(parents=True)
            (Path(tmp) / "templates" / "templates" / "index.html").write_text("<p>x</p>", encoding="utf-8")
            self.assertNotIn(CODE_PROBLEM, execute(ctx, "write_file", {"path": "c.py", "content": code}))

    def test_datetime_used_against_its_import_is_caught_when_written(self):
        # from datetime import datetime のあとの datetime.datetime.now() は、動かすと AttributeError(実際にあった。
        # 以前は、この形のコードに from datetime import datetime を勧めてしまっていた)
        from celestial.codecheck import suggest_fixes
        from celestial.tools import CODE_PROBLEM
        self.assertIn("import datetime", suggest_fixes("print(datetime.datetime.now())\n")[0]["new"])
        self.assertNotIn("from datetime import datetime", suggest_fixes("print(datetime.datetime.now())\n")[0]["new"])
        cases = {"from datetime import datetime\n\ndef now():\n    return str(datetime.datetime.now())\n": "datetime.now()",
                 "import datetime\n\ndef now():\n    return datetime.now().strftime('%H:%M')\n": "datetime.datetime.now()"}
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            for code, fixed in cases.items():
                with self.subTest(code=code[:30]):
                    note = execute(ctx, "write_file", {"path": "clock.py", "content": code})
                    self.assertIn(CODE_PROBLEM, note)
                    self.assertIn("AttributeError", note)
                    line = next(line for line in note.splitlines() if line.startswith("edit_file "))
                    self.assertNotIn(CODE_PROBLEM, execute(ctx, "edit_file", json.loads(line[len("edit_file "):])))
                    after = (Path(tmp) / "clock.py").read_text(encoding="utf-8")
                    self.assertIn(fixed, after)
                    scope: dict = {}
                    exec(compile(after, "clock.py", "exec"), scope)
                    self.assertTrue(scope["now"]())                         # 直したものは、実際に動く
            both = "import datetime\nfrom datetime import datetime\nprint(datetime.now())\n"  # 両方あれば、どちらも正しい
            self.assertNotIn(CODE_PROBLEM, execute(ctx, "write_file", {"path": "b.py", "content": both}))

    def test_the_same_addition_twice_is_refused(self):
        # 直らないと、同じ「行を足す」編集を3回繰り返し、同じ行が3つ重なっていた(実際にあった)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            execute(ctx, "write_file", {"path": "app.py", "content": "def f():\n    x = g()\n    return x\n\ndef g():\n    return None\n"})
            edit = {"path": "app.py", "old": "x = g()", "new": "x = g()\nif x is None:\n    x = 0"}
            self.assertIn("編集しました", execute(ctx, "edit_file", edit))
            with self.assertRaises(ToolError) as e:
                execute(ctx, "edit_file", edit)
            self.assertIn("もう入っています", str(e.exception))
            text = (Path(tmp) / "app.py").read_text(encoding="utf-8")
            self.assertEqual(text.count("if x is None"), 1)
            self.assertIn("    if x is None:\n        x = 0", text)               # 字下げは置き換える場所に合わせてある
            execute(ctx, "edit_file", {"path": "app.py", "old": "return None", "new": "# 何もない\nreturn None"})  # 前に足すのも同じ
            with self.assertRaises(ToolError):
                execute(ctx, "edit_file", {"path": "app.py", "old": "return None", "new": "# 何もない\nreturn None"})

    def test_editing_by_the_def_line_works_on_the_whole_function(self):
        # 小さいAIは「関数」を def の行で指す。そのまま置き換えると、足した関数が @app.route を奪ったり、元の中身が
        # return のあとに残ったりして、同じ関数が重なり Flask が起動できなくなった(qwen2.5-coder:7b で実際にあった)
        import ast
        code = ("from flask import Flask, jsonify\nfrom datetime import datetime\n\napp = Flask(__name__)\n\n"
                "@app.route('/')\ndef index():\n    return 'top'\n\n"
                "@app.route('/api/weather')\ndef weather():\n    try:\n        data = {'t': 11.6}\n"
                "        return jsonify({'temperature': data['t']})\n    except Exception:\n"
                "        return jsonify({'error': '取得できませんでした'}), 500\n\n"
                "if __name__ == '__main__':\n    app.run(port=5000)\n")

        def functions(source):
            return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            app = Path(tmp) / "app.py"
            app.write_text(code, encoding="utf-8")
            # 足す関数 + 中身まで書き直した weather: weather のかたまりごと置き換える(元の中身は残さない)
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "def weather():", "new": (
                "def get_weather_data():\n    try:\n        return {'t': 11.6}\n    except Exception as e:\n"
                "        return {'error': str(e)}\n\n@app.route('/api/weather')\ndef weather():\n"
                "    data = get_weather_data()\n    if 'error' in data:\n        return jsonify(data), 500\n"
                "    return jsonify({'temperature': data['t']})")})
            self.assertIn("関数の全体", out)
            source = app.read_text(encoding="utf-8")
            self.assertEqual(source.count("@app.route('/api/weather')"), 1)
            self.assertNotIn("取得できませんでした", source)                          # 元の中身は残っていない
            found = functions(source)
            self.assertEqual(found["get_weather_data"].decorator_list, [])          # 足した関数はページにならない
            self.assertEqual(len(found["weather"].decorator_list), 1)
            # 別の関数だけ: weather は残して、その上(デコレーターより上)に足す
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "def weather():",
                                             "new": "def get_current_time():\n    return datetime.now().strftime('%H:%M:%S')"})
            self.assertIn("消さずに残し", out)
            found = functions(app.read_text(encoding="utf-8"))
            self.assertEqual(found["get_current_time"].decorator_list, [])
            self.assertEqual(len(found["weather"].body), 3)                          # weather の中身はそのまま
            self.assertLess(found["get_current_time"].lineno, found["weather"].decorator_list[0].lineno)
            # もうある関数と同じ名前の関数を足す編集は断る(ファイルは変えない)
            before = app.read_text(encoding="utf-8")
            with self.assertRaises(ToolError) as e:
                execute(ctx, "edit_file", {"path": "app.py", "old": "def get_current_time():",
                                           "new": "def get_weather_data():\n    return {}"})
            self.assertIn("もうファイルにあります", str(e.exception))
            self.assertEqual(app.read_text(encoding="utf-8"), before)
            # 足す関数 + old の def の行: デコレーターより上に足す(index の @app.route('/') を奪わない)
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "def index():", "new": "def helper():\n    return 1\n\ndef index():"})
            self.assertIn("デコレーター", out)
            found = functions(app.read_text(encoding="utf-8"))
            self.assertEqual(found["helper"].decorator_list, [])
            self.assertEqual(len(found["index"].decorator_list), 1)
            # 名前を変えるだけ・中身の頭に行を足すだけは、今までどおりそのまま置き換える
            execute(ctx, "edit_file", {"path": "app.py", "old": "def helper():", "new": "def helper_two():"})
            execute(ctx, "edit_file", {"path": "app.py", "old": "def get_current_time():",
                                       "new": "def get_current_time():\n    print('time')"})
            # try / except の中で return する書き直しも、関数ごと置き換える(元の return 'top' を残さない)
            execute(ctx, "edit_file", {"path": "app.py", "old": "def index():", "new": (
                "def index():\n    try:\n        return 'new top'\n    except Exception:\n        return 'error'")})
            source = app.read_text(encoding="utf-8")
            self.assertNotIn("return 'top'", source)
            self.assertIn("@app.route('/')\ndef index():\n    try:", source)
            found = functions(source)
            self.assertIn("helper_two", found)
            self.assertEqual(len(found["get_current_time"].body), 2)
            for node in found.values():  # どの関数にも、return のあとに動かない行が無い
                self.assertFalse(any(isinstance(s, ast.Return) for s in node.body[:-1]), node.name)
            compile(source, "app.py", "exec")

    def test_route_functions_with_the_same_name_are_caught_when_written(self):
        # 同じ関数が2つ重なると、Flask は起動したときに AssertionError で止まる(実際にあった)。書いた時点で知らせ、
        # 同じ写しなら、消す直し方をそのまま渡す
        from celestial.tools import CODE_PROBLEM
        block = ("@app.route('/api/weather')\ndef get_weather_data():\n    try:\n        return {'t': 1}\n"
                 "    except Exception as e:\n        return {'error': str(e)}\n")
        code = ("from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\ndef index():\n    return 'top'\n\n"
                + block + "\n" + block + "\nif __name__ == '__main__':\n    app.run()\n")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            out = execute(ctx, "write_file", {"path": "app.py", "content": code})
            self.assertIn(CODE_PROBLEM, out)
            self.assertIn("get_weather_data(10・17 行目)", out)
            self.assertIn("AssertionError", out)
            fix = json.loads(out.split("\nedit_file ", 1)[1].splitlines()[0])
            out = execute(ctx, "edit_file", fix)
            self.assertNotIn(CODE_PROBLEM, out)
            source = (Path(tmp) / "app.py").read_text(encoding="utf-8")
            self.assertEqual(source.count("def get_weather_data"), 1)
            self.assertIn("def index():\n    return 'top'", source)
            self.assertIn("app.run()", source)
            compile(source, "app.py", "exec")
            # 中身が違うなら、どちらを残すかは決められないので、知らせるだけ
            other = block.replace("{'t': 1}", "{'t': 2}")
            out = execute(ctx, "write_file", {"path": "two.py", "content": code.replace(block + "\n" + block, block + "\n" + other)})
            self.assertIn(CODE_PROBLEM, out)
            self.assertNotIn("edit_file {", out)
            # Flask でなければ、同じ名前の関数を作り直しても止まらないので、知らせない
            out = execute(ctx, "write_file", {"path": "plain.py", "content": "def f():\n    return 1\n\ndef f():\n    return 2\n"})
            self.assertNotIn(CODE_PROBLEM, out)

    def test_a_route_above_the_flask_app_is_caught_when_written(self):
        # /api/time の関数を import のすぐ下(app = Flask(__name__) より上)に足して NameError になり、直そうとして上にもう1行
        # app = Flask(__name__) を足したため、下の行で app が作り直されて /api/time が 404 のままだった(実際にあった)
        from celestial.codecheck import used_before_defined
        from celestial.tools import CODE_PROBLEM
        code = ("from flask import Flask, jsonify\nfrom datetime import datetime\n\napp = Flask(__name__)\n\n"
                "def get_current_time():\n    return datetime.now().strftime('%H:%M:%S')\n\n"
                "@app.route('/')\ndef index():\n    return 'top'\n\n"
                "if __name__ == '__main__':\n    app.run(port=5000)\n")
        route = "@app.route('/api/time')\ndef api_time():\n    return jsonify({'time': get_current_time()})"

        def fixes(out):
            return [json.loads(line.split("edit_file ", 1)[1]) for line in out.splitlines() if line.startswith("edit_file {")]

        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            app = Path(tmp) / "app.py"
            app.write_text(code, encoding="utf-8")
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "from datetime import datetime",
                                             "new": "from datetime import datetime\n\n" + route})
            self.assertIn(CODE_PROBLEM, out)
            self.assertIn("作る前に使っています", out)
            for fix in fixes(out):  # 示した直し方を、そのまま順に使う
                out = execute(ctx, "edit_file", {"path": "app.py", **fix})
            self.assertNotIn(CODE_PROBLEM, out)
            source = app.read_text(encoding="utf-8")
            self.assertEqual(source.count("app = Flask(__name__)"), 1)
            self.assertLess(source.index("app = Flask(__name__)"), source.index("@app.route('/api/time')"))
            self.assertEqual(used_before_defined(source), [])
            compile(source, "app.py", "exec")
            # 上にもう1行足すと、2回目で app が作り直される: 2回目の行を消す直し方を示す
            app.write_text(code.replace("from datetime import datetime\n", "from datetime import datetime\n\n" + route + "\n"),
                           encoding="utf-8")
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "@app.route('/api/time')",
                                             "new": "app = Flask(__name__)\n\n@app.route('/api/time')"})
            self.assertIn("2回あります", out)
            for fix in fixes(out):
                out = execute(ctx, "edit_file", {"path": "app.py", **fix})
            source = app.read_text(encoding="utf-8")
            self.assertEqual(source.count("app = Flask(__name__)"), 1)
            self.assertNotIn(CODE_PROBLEM, out)
            compile(source, "app.py", "exec")
        # Flask でなくても、作る前に呼んでいる関数は知らせる(名前は定義されているので、ほかの確かめでは見つからない)
        self.assertEqual(used_before_defined("main()\n\ndef main():\n    print(1)\n"), [("main", 1, 3)])
        # 関数の中身・lambda・if __name__ の中で使うのは、あとで動くので問題ない
        self.assertEqual(used_before_defined("def f():\n    return g()\n\nh = lambda: g()\n\ndef g():\n    return 1\n"
                                             "if __name__ == '__main__':\n    f()\n"), [])

    def test_an_exact_copy_of_a_function_in_new_is_not_added_again(self):
        # 前の編集の new を使い回して、もうある関数(中身も同じ)をまた足そうとして断られ、消そうとして迷い、打ち切られて
        # いた(qwen2.5-coder:7b で実際にあった): 中身まで同じ写しは除いて、残りを反映する
        weather = ("def get_weather_data():\n    try:\n        return {'t': 1}\n    except Exception as e:\n"
                   "        return {'error': str(e)}\n")
        code = ("from flask import Flask, jsonify\nfrom datetime import datetime\n\napp = Flask(__name__)\n\n" + weather +
                "\n@app.route('/api/weather')\ndef api_weather():\n    return jsonify(get_weather_data())\n\n"
                "if __name__ == '__main__':\n    app.run(port=5000)\n")
        new = (weather + "\ndef get_current_time():\n    return datetime.now().strftime('%H:%M:%S')\n\n"
               "@app.route('/api/weather')\ndef api_weather():\n    return jsonify({**get_weather_data(), 'time': get_current_time()})")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            app = Path(tmp) / "app.py"
            app.write_text(code, encoding="utf-8")
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "def api_weather():", "new": new})
            self.assertIn("get_weather_data は、ファイルにあるものと中身まで同じ", out)
            source = app.read_text(encoding="utf-8")
            self.assertEqual(source.count("def get_weather_data"), 1)
            self.assertEqual(source.count("def api_weather"), 1)
            self.assertIn("def get_current_time", source)
            self.assertIn("'time': get_current_time()", source)
            compile(source, "app.py", "exec")
            # 中身が違うなら、今までどおり断る(どちらを残すかは分からない)
            changed = new.replace("{'t': 1}", "{'t': 2}")
            with self.assertRaises(ToolError) as caught:
                execute(ctx, "edit_file", {"path": "app.py", "old": "def get_current_time():", "new": changed})
            self.assertIn("もうファイルにあります", str(caught.exception))
            # old が見つからないときは、old の def の関数の今の全体を見せる(old にそのまま写せる)
            with self.assertRaises(ToolError) as caught:
                execute(ctx, "edit_file", {"path": "app.py", "old": weather.replace("{'t': 1}", "{'t': 9}"), "new": "x = 1\n"})
            self.assertIn("今のファイルの get_weather_data", str(caught.exception))
            self.assertIn("return {'t': 1}", str(caught.exception))

    def test_an_edit_that_is_already_in_the_file_says_so(self):
        # 変更は済んでいるのに、同じ編集(old は今は無い)を5回繰り返していた(実際にあった)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            (Path(tmp) / "index.html").write_text("<script>if (data.error) { show() }</script>\n", encoding="utf-8")
            with self.assertRaises(ToolError) as caught:
                execute(ctx, "edit_file", {"path": "index.html", "old": "if ('error' in data) {", "new": "if (data.error) {"})
            self.assertIn("この変更は、もう済んでいます", str(caught.exception))

    def test_a_file_from_before_is_read_before_it_is_rewritten(self):
        # gemma3:4b が calc.py も test_calc.py も読まずに calc.py を書き直し、テストが使う mul を消した(実際にあった)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
                                               encoding="utf-8")
            (Path(tmp) / "empty.py").write_text("", encoding="utf-8")
            hour_ago = time.time() - 3600
            os.utime(Path(tmp) / "calc.py", (hour_ago, hour_ago))
            ctx = ToolContext(workspace=Path(tmp), allow_write=True, changes={}, by_agent=True, started=time.time() - 5)
            with self.assertRaises(ToolError) as cut:
                execute(ctx, "write_file", {"path": "calc.py", "content": "def add(x, y):\n    return x + y\n"})
            self.assertIn("read_file", str(cut.exception))
            self.assertIn("依頼の前からある", str(cut.exception))
            # この依頼で動かしたプログラムが作ったファイルは、そうと伝える(「前からある」と、事実と違うことを言っていた)
            (Path(tmp) / "primes.txt").write_text("2\n3\n", encoding="utf-8")
            with self.assertRaises(ToolError) as made:
                execute(ctx, "write_file", {"path": "primes.txt", "content": "2\n"})
            self.assertIn("この依頼で動かしたプログラムが作った", str(made.exception))
            # list_files にファイルを渡しても、失敗にせず、あるファイルだと返す(「無い」と思い込んでいた)
            self.assertIn("あるファイルです", execute(ctx, "list_files", {"path": "primes.txt"}))
            self.assertIn("mul", (Path(tmp) / "calc.py").read_text(encoding="utf-8"))   # 消えていない
            execute(ctx, "write_file", {"path": "new.py", "content": "x = 1\n"})       # 新しいファイル
            execute(ctx, "write_file", {"path": "new.py", "content": "x = 2\n"})       # この依頼で作ったもの
            execute(ctx, "write_file", {"path": "empty.py", "content": "y = 1\n"})     # 空のファイル
            execute(ctx, "read_file", {"path": "calc.py"})
            execute(ctx, "write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n"
                                                                    "    return a * b\n"})  # 読んだあとなら書き直せる
            # 今とまったく同じ中身を書くのは、何も変わらない失敗(直したつもりで同じものを書いていた)
            with self.assertRaises(ToolError) as same:
                execute(ctx, "write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n"
                                                                        "    return a * b\n"})
            self.assertIn("何も変わりません", str(same.exception))
            # エージェントの外(プロジェクトの作成・ワークフロー)では、今までどおり読まずに書け、同じ中身でも失敗にしない
            (Path(tmp) / "other.py").write_text("z = 1\n", encoding="utf-8")
            free = ToolContext(workspace=Path(tmp), allow_write=True, changes={})
            execute(free, "write_file", {"path": "other.py", "content": "z = 2\n"})
            execute(free, "write_file", {"path": "other.py", "content": "z = 2\n"})

    @unittest.skipUnless(shutil.which("node"), "node がない")
    def test_a_stray_backslash_n_in_page_javascript_becomes_a_newline(self):
        # ページの <script> に、改行のつもりの \n の2文字が入っていて、ブラウザーでは構文の誤りで動かなかった(実際にあった)
        from celestial.tools import CODE_PROBLEM, PAGE_NEWLINES
        broken = ("<html><body><div id='t'></div>\n<script>\n    async function update() {\n"
                  "        const data = await (await fetch('/api/weather')).json();\n"
                  "        document.getElementById('t').textContent = `${data.temp}°C`;\\n        console.log('a\\nb');\\n    }\n"
                  "    setInterval(update, 600000);\\n    update();\\n</script>\n</body></html>\n")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            done = execute(ctx, "write_file", {"path": "templates/index.html", "content": broken})
            self.assertIn(PAGE_NEWLINES, done)
            self.assertNotIn(CODE_PROBLEM, done)
            saved = (Path(tmp) / "templates" / "index.html").read_text(encoding="utf-8")
            self.assertIn("°C`;\n        console.log('a\\nb');\n    }", saved)   # 文字列の中の \n はそのまま
            self.assertIn("setInterval(update, 600000);\n    update();\n</script>", saved)
            # 直せない構文の誤りは、行と一緒に知らせる。テンプレートの {{ }} は誤りにしない
            done = execute(ctx, "write_file", {"path": "templates/other.html", "content":
                           "<script>\nconst t = {{ weather|tojson }};\nfunction f() {\n  return 1;\n</script>\n"})
            self.assertIn(CODE_PROBLEM, done)
            self.assertIn("SyntaxError", done)
            done = execute(ctx, "write_file", {"path": "templates/fine.html", "content":
                           "<script>\nconst t = {{ weather|tojson }};\n{% if x %}console.log('x');{% endif %}\n</script>\n"
                           "<script type='application/ld+json'>{\"a\": 1,}</script>\n"})
            self.assertNotIn(CODE_PROBLEM, done)
            # 編集で入った \n も直す
            done = execute(ctx, "edit_file", {"path": "templates/fine.html", "old": "console.log('x');",
                                              "new": "console.log('x');\\n    console.log('y');"})
            self.assertIn(PAGE_NEWLINES, done)

    def test_an_old_not_in_a_small_file_shows_the_whole_file(self):
        # gemma3:4b が、読んだあとでも、ファイルに無い行を old にした edit_file を4回続けて失敗した(実際にあった)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
                                               encoding="utf-8")
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            with self.assertRaises(ToolError) as cut:
                execute(ctx, "edit_file", {"path": "calc.py", "old": "print(add(1, 2) == 4)", "new": "print(add(1, 2) == 3)"})
            self.assertIn("今の calc.py の全体(6 行)", str(cut.exception))
            self.assertIn("def add(a, b):\n    return a - b", str(cut.exception))

    def test_a_newline_that_cut_a_string_is_put_back(self):
        # print('Starting httpd...\n') の \n が、JSON で本当の改行になって文字列が切れ、直す edit_file の old も合わずに
        # 打ち切られていた(qwen2.5-coder:7b で実際にあった): \n の2文字に戻して保存する
        from celestial.codecheck import repair_string_newlines
        from celestial.tools import CODE_PROBLEM
        self.assertEqual(repair_string_newlines("print('Starting httpd...\n')\nx = 1\n"), "print('Starting httpd...\\n')\nx = 1\n")
        self.assertEqual(repair_string_newlines("if True:\n    print(f'a {1}\n  b')\n"), "if True:\n    print(f'a {1}\\n  b')\n")
        self.assertEqual(repair_string_newlines("print('a\n\nb')\n"), "print('a\\n\\nb')\n")
        self.assertIsNone(repair_string_newlines("print('ok')\n"))                   # 直す所が無い
        self.assertIsNone(repair_string_newlines("print('hello)\nx = 1\n"))           # 次の行で閉じない(本当の書き間違い)
        self.assertIsNone(repair_string_newlines("def f(:\n    pass\n"))              # ほかの誤りは扱わない
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            out = execute(ctx, "write_file", {"path": "server.py", "content": "print('Starting httpd...\n')\n"})
            self.assertIn("\\n の2文字に戻して保存しました", out)
            self.assertNotIn(CODE_PROBLEM, out)
            source = (Path(tmp) / "server.py").read_text(encoding="utf-8")
            compile(source, "server.py", "exec")
            out = execute(ctx, "edit_file", {"path": "server.py", "old": "print('Starting httpd...\\n')",
                                             "new": "print('Ready\n')"})
            self.assertIn("\\n の2文字に戻して保存しました", out)
            self.assertIn("print('Ready\\n')", (Path(tmp) / "server.py").read_text(encoding="utf-8"))

    def test_an_old_found_in_many_places_shows_where(self):
        # 「old の文字列が 3 か所にあります」だけでは、小さいAIは同じ編集を繰り返していた(実際にあった)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            execute(ctx, "write_file", {"path": "a.py", "content": "x = 1\nprint(x)\ny = 2\nprint(x)\n"})
            with self.assertRaises(ToolError) as e:
                execute(ctx, "edit_file", {"path": "a.py", "old": "print(x)", "new": "print(x + 1)"})
            message = str(e.exception)
            self.assertIn("2 か所にあります(2・4 行目)", message)
            self.assertIn('4 行目なら old = "y = 2\\nprint(x)"', message)
            execute(ctx, "edit_file", {"path": "a.py", "old": "y = 2\nprint(x)", "new": "y = 2\nprint(y)"})
            self.assertEqual((Path(tmp) / "a.py").read_text(encoding="utf-8"), "x = 1\nprint(x)\ny = 2\nprint(y)\n")

    def test_edit_that_breaks_python_is_not_saved(self):
        # 関数の途中にプログラム全体を貼り付けるような編集(実際にあった)は、保存せずに、壊れる場所を見せる
        from celestial.agent import judge_result
        from celestial.tools import CODE_PROBLEM
        good = "def index():\n    if True:\n        return 'a'\n    return 'b'\n"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            execute(ctx, "write_file", {"path": "app.py", "content": good})
            with self.assertRaises(ToolError) as e:
                execute(ctx, "edit_file", {"path": "app.py", "old": "        return 'a'",
                                           "new": "from datetime import datetime\napp = Flask(__name__)"})
            self.assertIn("構文が壊れる", str(e.exception))
            self.assertIn("ファイルは変えていません", str(e.exception))
            self.assertIn(" 3> from datetime import datetime", str(e.exception))      # 壊れる行に印
            with self.assertRaises(ToolError):
                execute(ctx, "append_file", {"path": "app.py", "content": "    else:\n"})  # 足すほうも同じ
            self.assertEqual((Path(tmp) / "app.py").read_text(encoding="utf-8"), good)  # ファイルはそのまま
            broken = execute(ctx, "write_file", {"path": "b.py", "content": "def f(:\n    pass\n"})  # 丸ごと書くときは保存して知らせる
            self.assertIn(CODE_PROBLEM, broken)
            self.assertIn("SyntaxError", broken)
            ok, judged = judge_result("write_file", broken)
            self.assertFalse(ok)                                                            # 失敗として直させる
            self.assertTrue(judged.startswith("FAILED: the file was saved"))
            # もともと壊れているファイルは、直す途中かもしれないので止めない
            self.assertIn("編集しました", execute(ctx, "edit_file", {"path": "b.py", "old": "pass", "new": "return 1"}))

    def test_edit_finds_the_place_when_old_is_slightly_off(self):
        # 小さいAIは、直す前の形のままの行や、字下げを落とした行を old に渡しがち(実際にあった)。
        # 置き換える場所が1か所に決まるときだけ置き換え、どこを置き換えたかを返す
        code = ("from flask import Flask, render_template\napp = Flask(__name__)\n\n@app.route('/')\ndef index():\n"
                "    data = {}\n    if data:\n        return render_template('index.html', weather=data, now=now)\n"
                "    return render_template('error.html')\n")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            execute(ctx, "write_file", {"path": "app.py", "content": code})
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "return render_template('index.html', weather=data)",
                                             "new": "return render_template('index.html', weather=data, now=datetime.now())"})
            self.assertIn("いちばん近い 8 行目を置き換えました", out)
            self.assertIn("datetime(8 行目)", out)                       # 置き換えたあとの誤り(import 漏れ)も知らせる
            text = (Path(tmp) / "app.py").read_text(encoding="utf-8")
            self.assertIn("        return render_template('index.html', weather=data, now=datetime.now())\n", text)
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "if data:\nreturn render_template('index.html', weather=data, now=datetime.now())",
                                             "new": "if data:\n    return render_template('index.html', weather=data)"})
            self.assertIn("字下げや空白の違いを除くと一致した 7〜8 行目", out)
            self.assertIn("    if data:\n        return render_template('index.html', weather=data)\n",
                          (Path(tmp) / "app.py").read_text(encoding="utf-8"))
            # 字下げがずれた new(old はそのまま見つかる)は、置き換える場所の字下げに合わせる
            out = execute(ctx, "edit_file", {"path": "app.py", "old": "    data = {}", "new": "now = datetime.now()\ndata = {}"})
            self.assertIn("字下げを、置き換える場所に合わせました", out)
            self.assertIn("    now = datetime.now()\n    data = {}\n", (Path(tmp) / "app.py").read_text(encoding="utf-8"))
            # 別の語が違う行(other.html と error.html)は、別の行かもしれないので置き換えない
            with self.assertRaises(ToolError):
                execute(ctx, "edit_file", {"path": "app.py", "old": "return render_template('other.html')", "new": "return 1"})
            # 引用符の違いだけなら同じ行
            out = execute(ctx, "edit_file", {"path": "app.py", "old": 'return render_template("error.html")',
                                             "new": 'return render_template("error.html"), 500'})
            self.assertIn("いちばん近い", out)

    def test_a_near_edit_of_part_of_a_line_keeps_the_rest_of_the_line(self):
        # old が行の一部({{ ... }})で、そのままでは見つからないとき、行の全体を new にして <div class="weather"> まで
        # 消していた。ページの JavaScript が querySelector('.weather') の null で止まった(実際にあった)
        rest = "'晴れ' if w == 1 else '曇り' if w == 3 else '雨' if w == 61 else '雪' if w == 71 else '雷雨' if w == 95 else '不明' }}"
        first, fixed = "{{ " + rest, "{{ '快晴' if w == 0 else " + rest
        page = f"<body>\n    <div class=\"weather\">{first}</div>\n    <div class=\"temp\">{{{{ t }}}}</div>\n</body>\n"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            execute(ctx, "write_file", {"path": "index.html", "content": page})
            out = execute(ctx, "edit_file", {"path": "index.html", "old": "{{ '不明' if w == 2 else " + rest, "new": fixed})
            self.assertIn("行の前と後ろは、そのまま残しました", out)
            text = (Path(tmp) / "index.html").read_text(encoding="utf-8")
            self.assertIn(f"    <div class=\"weather\">{fixed}</div>\n", text)
            # 置き換えても中身が同じになる編集は、ファイルを変えずに「もう済んでいます」(時刻も変えない。変わると、
            # 起動したあとでコードを変えた、の確かめが働き、起動し直しと同じ編集を4回繰り返した)
            before = (Path(tmp) / "index.html").stat().st_mtime_ns
            time.sleep(0.02)
            with self.assertRaises(ToolError) as caught:
                execute(ctx, "edit_file", {"path": "index.html", "old": "{{ '不明' if w == 2 else '快晴' if w == 0 else " + rest,
                                           "new": fixed})
            self.assertIn("もう済んでいます", str(caught.exception))
            self.assertEqual((Path(tmp) / "index.html").stat().st_mtime_ns, before)
            # new が行の前と後ろも書いているなら、二重にしない
            out = execute(ctx, "edit_file", {"path": "index.html", "old": "<div class=\"temp\">{{ t }} °C の気温</div>",
                                             "new": "<div class=\"temp\">{{ t }}°C</div>"})
            self.assertNotIn("そのまま残しました", out)
            self.assertIn("    <div class=\"temp\">{{ t }}°C</div>\n", (Path(tmp) / "index.html").read_text(encoding="utf-8"))


class MarkupInPythonTest(unittest.TestCase):
    def test_an_html_page_written_into_app_py_is_rewritten_not_edited(self):
        # write_code の app.py に HTML のページがそのまま書かれ、エージェントは CSS の行に注釈を足す編集を繰り返していた
        # (実際にあった)
        from celestial.agent import judge_result
        from celestial.codecheck import markup_in_python
        page = ("<!DOCTYPE html>\n<html lang=\"ja\">\n<head>\n    <style>\n        body {\n            height: 100vh;\n"
                "        }\n    </style>\n</head>\n<body>\n    <div class=\"weather\">{{ weather.weather }}</div>\n</body>\n</html>\n")
        self.assertTrue(markup_in_python(page))
        self.assertTrue(markup_in_python(page.split("\n", 1)[1].replace("<html lang=\"ja\">\n", "")))  # 先頭が <html でなくても
        self.assertFalse(markup_in_python("from flask import Flask\napp = Flask(__name__)\nHTML = \"\"\"<html>\n<body></body>\n</html>\"\"\"\n"))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            out = execute(ctx, "write_file", {"path": "app.py", "content": page})
            self.assertIn("write_code で app.py を、Python のコードとして書き直して", out)
            ok, judged = judge_result("write_file", out)
            self.assertFalse(ok)
            self.assertIn("edit_file cannot fix this", judged)


class UnrequestedVariableTest(unittest.TestCase):
    def test_reading_a_variable_the_url_does_not_ask_for_is_a_mistake(self):
        # URL が current=temperature_2m,relative_humidity_2m なのに current['weather_code'] を読み、KeyError を「不明」に
        # していた(天気がいつも「不明」。実際にあった)
        from celestial.knowhow import open_meteo_unrequested
        code = ("import requests\nURL = 'https://api.open-meteo.com/v1/forecast?latitude=34.69&longitude=135.50"
                "&current=temperature_2m,relative_humidity_2m&daily=temperature_2m_max&timezone=Asia%2FTokyo'\n"
                "def get():\n    data = requests.get(URL, timeout=5).json()\n    current = data['current']\n"
                "    return {'weather': current['weather_code'] if 'weather_code' in current else '不明',\n"
                "            'temperature': current['temperature_2m'], 'max': data['daily']['temperature_2m_max'][0],\n"
                "            'min': data['daily'].get('temperature_2m_min')}\n")
        found = open_meteo_unrequested(code)
        self.assertEqual(found, [("weather_code", "current", "current=temperature_2m,relative_humidity_2m",
                                  "current=temperature_2m,relative_humidity_2m,weather_code"),
                                 ("temperature_2m_min", "daily", "daily=temperature_2m_max",
                                  "daily=temperature_2m_max,temperature_2m_min")])
        fixed = code.replace("relative_humidity_2m&daily=temperature_2m_max", "relative_humidity_2m,weather_code&daily="
                             "temperature_2m_max,temperature_2m_min")
        self.assertEqual(open_meteo_unrequested(fixed), [])
        params = ("import requests\nPARAMS = {'latitude': 35.68, 'longitude': 139.76, 'current': ['temperature_2m']}\n"
                  "r = requests.get('https://api.open-meteo.com/v1/forecast', params=PARAMS).json()\n"
                  "print(r['current']['weather_code'], r['current']['temperature_2m'])\n")
        self.assertEqual(open_meteo_unrequested(params), [("weather_code", "current", "['temperature_2m']",
                                                           "['temperature_2m', 'weather_code']")])
        built = "URL = f'https://api.open-meteo.com/v1/forecast?current={CURRENT}'\nprint(data['current']['weather_code'])\n"
        self.assertEqual(open_meteo_unrequested(built), [])                 # 組み立てている URL は見ない
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(workspace=Path(tmp), allow_write=True)
            out = execute(ctx, "write_file", {"path": "app.py", "content": code})
            self.assertIn("頼んでいない項目を、コードが読んでいます", out)
            self.assertIn('"new": "current=temperature_2m,relative_humidity_2m,weather_code"', out)
            from celestial.agent import judge_result
            self.assertFalse(judge_result("write_file", out)[0])

    @unittest.skipUnless(shutil.which("node"), "node が無い")
    def test_a_weather_shown_as_unknown_is_a_problem(self):
        from celestial import pagerun
        page = "<div id='weather'></div><div id='temp'></div>"
        script = "document.getElementById('weather').textContent = '不明'; document.getElementById('temp').textContent = '不明';"
        run = pagerun.run_page("http://127.0.0.1:9/", page, [(len(page), script)], {})
        _, problems = pagerun.describe(run, [script])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("天気の欄(#weather)に「不明」", problems[0])
        script = "document.getElementById('weather').textContent = '--';"   # 読み込み中の印は咎めない
        run = pagerun.run_page("http://127.0.0.1:9/", page, [(len(page), script)], {})
        self.assertEqual(pagerun.describe(run, [script])[1], [])


@unittest.skipUnless(shutil.which("node"), "node が無い")
class PageRunTest(unittest.TestCase):
    """ページの JavaScript を node で動かす確かめ(pagerun)。"""

    page = ("<!DOCTYPE html><html><head><title>t</title></head><body>\n<div id=\"clock\"></div>\n<div class=\"weather\"></div>\n"
            "<div class=\"temp\"></div>\n<script>\n"
            "function tick() { document.getElementById('clock').textContent = new Date().toLocaleTimeString('ja-JP'); }\n"
            "fetch('/api/weather').then(r => r.json()).then(data => {\n"
            "  document.querySelector('.weather').textContent = data.weather;\n"
            "  document.querySelector('.temp').textContent = data.temperature + '°C';\n"
            "});\ntick(); setInterval(tick, 1000);\n</script></body></html>")
    reply = {"/api/weather": (200, '{"weather": 2, "temperature": 18.2}')}

    def run_page(self, html, replies=None, outside=False):
        from celestial.pagerun import describe, run_page
        at = html.index("<script>\n")
        scripts = [(at, html[at + len("<script>"):].split("</script>", 1)[0])]
        run = run_page("http://127.0.0.1:5000/", html, scripts, self.reply if replies is None else replies, outside)
        self.assertIsNotNone(run)
        return describe(run, [code for _, code in scripts])

    def test_a_weather_number_and_a_missing_element_are_found(self):
        notes, problems = self.run_page(self.page)
        self.assertIn(".temp「18.2°C」", notes[0])                     # 動かしたときの表示を知らせる
        self.assertTrue(any("番号「2」がそのまま出ます" in p for p in problems))
        fixed = self.page.replace("= data.weather;", "= ({0: '快晴', 2: '晴れ時々曇り'})[data.weather];")
        notes, problems = self.run_page(fixed)
        self.assertEqual(problems, [])
        self.assertIn(".weather「晴れ時々曇り」", notes[0])
        self.assertRegex(notes[0], r"#clock「\d{1,2}:\d{2}:\d{2}」")   # 時計も動いた
        gone = fixed.replace("<div class=\"weather\"></div>\n", "")  # 編集で要素が消えた(実際にあった)
        _, problems = self.run_page(gone)
        self.assertTrue(problems and "ページに .weather の要素がありません" in problems[0])
        # head の中の <script> は、body の要素より先に動く(その時点では、まだ要素が無い)
        from celestial.pagerun import describe, run_page
        early = fixed.replace("<head><title>t</title></head>", "<head><title>t</title></head>")
        code = "document.getElementById('clock').textContent = '';"
        run = run_page("http://127.0.0.1:5000/", early, [(early.index("<title>"), code)], self.reply)
        _, problems = describe(run, [code])
        self.assertTrue(problems and "まだその要素がありません" in problems[0])

    def test_a_working_page_is_not_blamed(self):
        broken_key = self.page.replace("data.temperature", "data.temp").replace("= data.weather;", "= '晴れ';")
        _, problems = self.run_page(broken_key)
        self.assertTrue(problems and "undefined" in problems[0])        # 答えに無い項目を読むと undefined が出る
        quiet = self.page.replace("= data.weather;", "= '晴れ';")
        self.assertEqual(self.run_page(quiet, replies={})[1], [])       # 答えが返らない(外のサービスが止まっている)だけなら言わない
        library = quiet.replace("tick(); setInterval", "new Chart(document.getElementById('clock'), {}); tick(); setInterval")
        self.assertEqual(self.run_page(library, outside=True)[1], [])  # CDN のライブラリの名前は、誤りとしない
        typo = quiet.replace("tick(); setInterval", "tik(); setInterval")
        problems = self.run_page(typo)[1]
        self.assertTrue(problems and "ReferenceError: tik is not defined" in problems[0])
        made = quiet.replace("<div class=\"temp\"></div>", "<div id=\"box\"></div>").replace(
            "tick(); setInterval", "document.getElementById('box').innerHTML = '<span class=\"temp\"></span>'; tick(); setInterval")
        self.assertEqual(self.run_page(made)[1], [])                    # JavaScript で作った要素も見つかる

    def test_the_page_check_runs_the_page_and_fails_the_check(self):
        import http.server
        import threading
        from celestial.agent import judge_result
        from celestial.tools import PAGE_PROBLEM, page_wiring
        page = self.page.replace("<div class=\"weather\"></div>\n", "").encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = page if self.path == "/" else b'{"weather": 2, "temperature": 18.2}'
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/" else "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        out = page_wiring(url, page.decode("utf-8"))
        self.assertIn(PAGE_PROBLEM, out)
        self.assertIn("ページに .weather の要素がありません", out)
        ok, judged = judge_result("check_url", "HTTP 200  text/html\n" + out)
        self.assertFalse(ok)
        self.assertIn("when its JavaScript runs", judged)                   # 動かして分かった誤りの直し方を伝える


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
        for text in ("このアプリのバグを直して", "app.py に関数を追加して", "アプリの作り方を教えて", "今日の天気は?",
                     # 調べものが要る依頼・前の作業の続きは、ファイルやネットを扱えるエージェントに任せる
                     "openmeteoの使い方などについてwebサイトで調べて、それらを参考に続きから作成して", "続きからアプリを作成して"):
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
        # ファイル名の行や説明に挟まれたコード(小さいモデルがよくやる)
        self.assertEqual(strip_fences("primes/primes.py\n```python\nprint(2)\n```", "primes/primes.py"), "print(2)\n")
        self.assertEqual(strip_fences("Here is the code:\n```js\nconsole.log(1)\n```\nRun it with node.", "app.js"), "console.log(1)\n")
        self.assertEqual(strip_fences("# File: app.py\nprint(3)\n", "app.py"), "print(3)\n")
        self.assertEqual(strip_fences("```python\nprint(4)\n", "a.py"), "print(4)\n")       # 閉じていない囲み
        readme = "# 使い方\n\n```bash\npython app.py\n```\n\n以上です。\n"
        self.assertEqual(strip_fences(readme, "README.md"), readme)                         # 文書の中のコード例は残す
        self.assertEqual(strip_fences("```markdown\n" + readme + "```", "README.md"), readme)
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
