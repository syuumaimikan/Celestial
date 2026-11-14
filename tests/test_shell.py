"""コマンドの実行を確実にする工夫の試験(Windows の cmd 向けの直し・サーバーを裏で起動・時間切れで子ごと止める・まとめて移動)。"""

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from celestial import tools
from celestial.agent import judge_result
from celestial.tools import ToolContext, ToolError, execute, stop_processes


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(sys.platform == "win32", "Windows の cmd 向けの直し")
class ForCmdTest(unittest.TestCase):
    def test_unix_habits_are_fixed(self):
        fix = tools._for_shell
        self.assertEqual(fix("git commit -m 'Initial commit'"), 'git commit -m "Initial commit"')
        self.assertEqual(fix("git init && git add README.md && git commit -m 'first'"),
                         'git init && git add README.md && git commit -m "first"')
        self.assertEqual(fix("git init; git add ."), "git init& git add .")
        self.assertEqual(fix("mkdir -p photos/2023 photos/2024"),
                         r'(if not exist "photos\2023" mkdir "photos\2023") & (if not exist "photos\2024" mkdir "photos\2024")')
        self.assertEqual(fix("mkdir out && cd out"), '(if not exist "out" mkdir "out") && cd out')
        self.assertEqual(fix("echo 'hello world' > a.txt"), "(echo hello world)> a.txt")
        self.assertEqual(fix("""python -c 'print("hi")'"""), r'python -c "print(\"hi\")"')

    def test_things_that_must_not_change(self):
        fix = tools._for_shell
        for command in ("echo it's John's", 'python -c "print(\'x\')"', "git status",
                        "powershell -Command \"Get-ChildItem | Where-Object { $_.Name -like '*a*' }\"",
                        "dir /b *.txt | findstr a", "set PATH=a;b"):
            self.assertEqual(fix(command), command)

    def test_fixed_commands_really_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            ctx = ToolContext(ws, allow_run=True, confirm=False)
            execute(ctx, "run_command", {"command": "mkdir -p photos/2023 && mkdir -p photos/2023"})  # 2回目もエラーにならない
            self.assertTrue((ws / "photos" / "2023").is_dir())
            self.assertFalse((ws / "-p").exists())                                  # -p という名前のフォルダができない
            execute(ctx, "run_command", {"command": "echo 'hello' > note.txt"})
            self.assertEqual((ws / "note.txt").read_text().strip(), "hello")      # 引用符も末尾の空白も入らない
            out = execute(ctx, "run_command", {"command": f"""\"{sys.executable}\" -c 'print("quoted")'"""})
            self.assertIn("quoted", out)                                            # 一重引用符の中の二重引用符も通る
            out = execute(ctx, "run_command", {"command": "git init && git add note.txt && git commit -m 'first note'"})
            ok, _ = judge_result("run_command", out)
            self.assertTrue(ok, out)                                                # 一重引用符でもコミットできる

    def test_hint_for_unix_commands(self):
        import subprocess
        missing = subprocess.CompletedProcess("cat a.txt", 1, b"", "'cat' は、内部コマンドまたは外部コマンド、操作可能なプログラムまたは"
                                              "バッチ ファイルとして認識されていません。".encode("cp932"))
        hint = tools._command_hint("cat a.txt", missing)
        self.assertIn("type", hint)                                                 # Windows での言い換えを示す
        out = tools._finish(missing, hint)
        ok, told = judge_result("run_command", out)
        self.assertFalse(ok)                                                        # 終了コードは最後の行のまま(失敗と分かる)
        self.assertIn("move_file", told)


class DetectionTest(unittest.TestCase):
    def test_ports_and_long_running_programs(self):
        self.assertEqual(tools._ports('server = HTTPServer(("", 8931), Handler)\nserver.serve_forever()'), [8931])
        self.assertEqual(tools._ports("app.run(port=5001, debug=True)"), [5001])
        self.assertEqual(tools._ports("python -m http.server"), [8000])
        self.assertEqual(tools._ports("app.listen(3000)"), [3000])
        self.assertEqual(tools._ports("server.py 8123"), [8123])
        self.assertEqual(tools._ports("data = conn.recv(4096)\nrun_server(8932)"), [8932])  # 読み書きの大きさは番号と見ない
        self.assertEqual(tools._ports("x = f(1, 2000)\nPORT = 8931"), [8931, 2000])       # はっきり書かれた番号が先
        self.assertEqual(tools._long_running('server = HTTPServer(("", 8931), H)\nserver.serve_forever()'), "server")
        self.assertEqual(tools._long_running("import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()"), "window")
        self.assertEqual(tools._long_running("print(sum(range(10)))"), "")
        test = "import threading\nt = threading.Thread(target=srv.serve_forever)\nt.start()\nsrv.shutdown()"
        self.assertEqual(tools._long_running(test), "")                         # 自分で止める試験は、普通に動かす
        self.assertTrue(tools._SERVER_COMMAND.search("npm run dev"))
        self.assertFalse(tools._SERVER_COMMAND.search("npm run build"))
        self.assertFalse(tools._SERVER_COMMAND.search("vite build"))
        self.assertTrue(tools._RUN_SCRIPT.search('python -X utf8 server.py'))
        self.assertFalse(tools._RUN_SCRIPT.search("git add server.py"))          # 動かすのではなく、名前が出てくるだけ
        self.assertFalse(tools._RUN_SCRIPT.search("python -m pytest test_server.py"))


class BackgroundAutoTest(unittest.TestCase):
    def test_server_script_is_started_in_the_background(self):
        port = _free_port()
        code = (
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'hello')\n\n"
            f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "server.py").write_text(code, encoding="utf-8")
            ctx = ToolContext(ws, allow_run=True, confirm=False)
            t0 = time.perf_counter()
            out = execute(ctx, "run_file", {"path": "server.py"})
            self.assertLess(time.perf_counter() - t0, 15)                         # 時間切れまで待たない
            self.assertIn("裏で起動しました", out)
            self.assertIn(f"ポート {port} で接続を受け付けています", out)
            self.assertIn("HTTP 200: hello", out)                                 # 開いてみた結果も伝える
            self.assertTrue(judge_result("run_file", out)[0])
            page = execute(ctx, "check_url", {"url": f"http://localhost:{port}/"})
            self.assertIn("HTTP 200", page)
            self.assertIn("hello", page)
            self.assertEqual(stop_processes(ctx), [])                             # 依頼が終われば止める

    def test_quick_exit_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "bad_server.py").write_text("import not_installed_module\nserver.serve_forever()\n", encoding="utf-8")
            ctx = ToolContext(ws, allow_run=True, confirm=False)
            out = execute(ctx, "run_file", {"path": "bad_server.py"})
            self.assertIn("すぐに終了しました", out)
            self.assertFalse(judge_result("run_file", out)[0])

    def test_one_shot_socket_server_is_not_a_web_server(self):
        port = _free_port()
        code = ("import socket\n"
                "with socket.socket() as s:\n"
                f"    s.bind(('127.0.0.1', {port}))\n"
                "    s.listen()\n"
                "    conn, _ = s.accept()\n"
                "    conn.sendall(b'hello')\n"
                "    conn.close()\n")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "server.py").write_text(code, encoding="utf-8")
            ctx = ToolContext(ws, allow_run=True, allow_net=True, confirm=False)  # socket はネットの許可が要る
            out = execute(ctx, "run_file", {"path": "server.py"})
            ok, told = judge_result("run_file", out)
            self.assertFalse(ok, out)                                               # 1回答えて終わるものは、サーバーとして失敗
            self.assertIn("keep running", told)
            stop_processes(ctx)

    def test_check_url_explains_non_http_answers(self):
        import threading
        port = _free_port()
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen()

        def answer():
            conn, _ = listener.accept()
            conn.recv(1000)
            conn.sendall(b"hello")
            conn.close()

        threading.Thread(target=answer, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = ToolContext(Path(tmp), allow_run=True, confirm=False)
                with self.assertRaises(ToolError) as bad:
                    execute(ctx, "check_url", {"url": f"http://localhost:{port}/"})
                self.assertIn("HTTP の応答が返りませんでした", str(bad.exception))
        finally:
            listener.close()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(Path(tmp), allow_run=True, confirm=False)
            with self.assertRaises(ToolError) as refused:
                execute(ctx, "check_url", {"url": f"http://localhost:{_free_port()}/"})
            self.assertIn("待ち受けているサーバーがありません", str(refused.exception))


class TimeoutTest(unittest.TestCase):
    def test_timeout_stops_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ToolContext(Path(tmp), allow_run=True, confirm=False)
            command = f'"{sys.executable}" -c "import time; print(\'start\', flush=True); time.sleep(60)"'
            t0 = time.perf_counter()
            with self.assertRaises(ToolError) as timed:
                tools.run_command(ctx, command, timeout=2)
            self.assertLess(time.perf_counter() - t0, 20)                        # シェルの先のプログラムに待たされない
            self.assertIn("中断しました", str(timed.exception))
            self.assertIn("start", str(timed.exception))                          # それまでの出力も伝える


class MoveManyTest(unittest.TestCase):
    def test_pattern_moves_every_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "photos").mkdir()
            for name in ("IMG_2023_01.jpg", "IMG_2023_07.jpg", "IMG_2024_03.jpg"):
                (ws / "photos" / name).write_text("", encoding="utf-8")
            ctx = ToolContext(ws, allow_write=True, confirm=False, changes={})
            out = execute(ctx, "move_file", {"path": "photos/*2023*", "to": "photos/2023"})
            self.assertIn("2 件を移動しました", out)
            self.assertEqual(sorted(p.name for p in (ws / "photos" / "2023").iterdir()), ["IMG_2023_01.jpg", "IMG_2023_07.jpg"])
            self.assertTrue((ws / "photos" / "IMG_2024_03.jpg").is_file())
            self.assertIn("photos/2023/IMG_2023_01.jpg", ctx.changes)               # 変更の記録にも残る
            execute(ctx, "copy_file", {"path": "photos/*.jpg", "to": "backup"})
            self.assertEqual([p.name for p in (ws / "backup").iterdir()], ["IMG_2024_03.jpg"])
            with self.assertRaises(ToolError):
                execute(ctx, "move_file", {"path": "photos/*.png", "to": "x"})        # 合うものが無ければ、そう伝える
            with self.assertRaises(ToolError):
                execute(ctx, "move_file", {"path": "../*", "to": "x"})                # 作業フォルダの外へは出ない


if __name__ == "__main__":
    unittest.main()
