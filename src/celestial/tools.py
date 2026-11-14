"""ワーカーが使える道具(ツール)。

安全のための決まり:
- ファイルを扱う道具は、設定した作業フォルダの中だけに届く(外へ出るパスは拒否する)。
- ファイルを書き換える・消す前に、必ず元のファイルをバックアップへ写す(restore_file で戻せる)。
- 書き込み・実行・PC操作・ネットは、それぞれ設定で明示的に許可されたときだけ使える。
- 影響のある操作(risky)は、実行の直前に利用者へ内容を見せて確認を取る(設定で省略できる = すべて自動)。
- すべて自動でも、コマンドとコードは guard.py で確かめる: 取り返しのつかない操作は実行せず、作業フォルダの外を変える・
  ネットから取り込む・公開するなどは、利用者に確かめてから行う。
"""

from __future__ import annotations

import ast
import csv
import difflib
import fnmatch
import io
import itertools
import html
import http.client
import json
import math
import operator
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import guard, knowhow, media, office, pagerun, pc, symcalc, web
from .codecheck import (block_lines, datetime_fixes, duplicate_routes, flask_order_fixes, js_syntax_error, markup_in_python,
                        repair_page_newlines,
                        repair_string_newlines, script_kind, script_problem, suggest_fixes, template_path_fixes,
                        top_level_defs, undefined_names, used_before_defined)

MAX_OUTPUT = 4000
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".celestial"}


class ToolError(Exception):
    """道具の使い方が誤っている、または許可されていない。メッセージはそのままAIに返す。"""


@dataclass
class ToolContext:
    workspace: Path | None = None
    allow_write: bool = False
    allow_run: bool = False
    allow_pc: bool = False
    allow_net: bool = False
    backup_dir: Path | None = None
    confirm: bool = False  # True なら、影響のある操作の前に approve で確認を取る
    confirm_commands: bool = False  # True なら、コマンド・コードの実行と PC の操作の前に確認を取る(ファイルの変更は確認しない)
    guard: bool = True     # コマンドとコードを guard.py で確かめる(権限が「すべて許可」なら False)
    anywhere: bool = False  # 作業フォルダの外も、絶対パスで扱える(権限が「すべて許可」のとき)
    approve: Callable[[dict], bool] | None = None
    describe_image: Callable[[Path, str], str] | None = None  # 画像を見て説明するAI(スクリーンショット用)
    delegate: Callable[[str, str], str] | None = None  # 専門のワーカーに仕事を頼む (役割, 依頼) → 回答
    scratch_dir: Path | None = None  # スクリーンショットなどの置き場
    changes: dict | None = None  # 変えたファイルの記録 {作業フォルダからの位置: 最初の内容(新しく作ったなら None)}。None なら記録しない
    write_code: Callable[[str, str, str | None], str] | None = None  # (位置, 指示, 今の中身) → ファイルの中身を書くAI
    processes: list[str] = field(default_factory=list)  # この依頼で起動した常駐プログラムの名前(終わったら止める)
    task: str = ""  # 今の依頼の文(頼まれていないのに、作ったファイルを利用者の画面のアプリで開かないため)
    read: set[str] = field(default_factory=set)  # この依頼で読んだファイル(読まずに丸ごと書き直させないため)
    by_agent: bool = False  # 道具を使うのがエージェントのAI(読まずに書き直させない など、AIの誤りを防ぐ決まりを使う)
    started: float = 0.0  # 依頼を始めた時刻(依頼の前からあったファイルと、この依頼の中でできたファイルを分ける)
    operated: bool = False  # この依頼で PC を操作した(アプリを開く・入力・クリックなど。そのときだけ、画面を撮って確かめる)
    kb: object | None = None  # ナレッジベース(KnowledgeBase)
    memorize: Callable[[str, str], str] | None = None  # (action, fact) -> 記憶の更新結果


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # AI向けの説明
    label: str        # 画面に出す名前
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    group: str = "calc"  # calc / team / files / write / run / pc / net
    risky: bool = False  # 実行前に確認を取るか


TOOLS = {t.name: t for t in [
    Tool("calc", "Evaluate arithmetic, algebra (equations like \"2*x+3=11\", \"x+y=10, x-y=2\", solve), calculus (diff, integrate, limit), "
                 "factor/simplify/factorint, statistics (mean/median/stdev), and unit conversions (\"100 km to mile\", \"25 C to F\"). args: expression.",
         "計算・数式", ("expression",)),
    Tool("delegate", "Ask a specialist AI worker to do one self-contained piece of work and get its answer. "
                     "args: role (general / code / reason / summarize / translate / write / extract), task (the full request, including any text to process).",
         "ワーカーに依頼", ("role", "task"), (), "team"),
    # ---- ファイル(読み取り) ----
    Tool("list_files", "List files and folders in the workspace. args: path (folder, default \".\").", "ファイル一覧", (), ("path",), "files"),
    Tool("tree", "Show the folders and files of the workspace (or of a folder in it) a few levels deep. args: path (default \".\").",
         "フォルダの構成", (), ("path",), "files"),
    Tool("find_files", "Find files by name pattern anywhere in the workspace. args: pattern (e.g. \"*.py\", \"*report*\").", "ファイルを探す", ("pattern",), (), "files"),
    Tool("read_file", "Read a workspace text, code, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), or PDF (.pdf) file. "
                      "args: path, start (first line number, default \"1\").", "ファイルを読む", ("path",), ("start",), "files"),
    Tool("search_files", "Find files whose name contains a text or matches a pattern (such as \"IMG_2023_*\"), and lines "
                         "containing the text in workspace files. args: query, path (folder, default \".\").", "ファイル内を検索",
         ("query",), ("path",), "files"),
    Tool("csv_query", "Inspect schema or run a read-only SQL SELECT query on a CSV, TSV, or JSON-array file (table name is t). "
                      "args: path, sql (optional; omit for schema & sample rows).", "CSV/JSONをSQL検索",
         ("path",), ("sql",), "files"),
    Tool("sqlite_query", "Inspect tables or run a read-only SQL SELECT query on a SQLite database (.db/.sqlite). "
                         "args: path, sql (optional; omit to list tables & counts).", "SQLiteを検索",
         ("path",), ("sql",), "files"),
    Tool("git_info", "Inspect git repository status, diff, log, or branches in the workspace (read-only). "
                     "args: action (status / diff / log / branch, default status), target (optional path/branch), limit (default \"20\").",
         "Git情報", (), ("action", "target", "limit"), "files"),
    Tool("code_check", "Check syntax and common bugs in workspace files (.py, .js, .html, .json, .yaml, .toml) without executing them. "
                       "args: path (file or folder, default \".\").",
         "コード検証", (), ("path",), "files"),
    Tool("scan_barcode", "Decode QR codes or 1D/2D barcodes from an image file in the workspace. args: path.",
         "QR/バーコード読取", ("path",), (), "files"),
    # ---- ファイル(変更) ----
    Tool("write_file", "Create a text file, or replace the whole content of an existing one. args: path, content.", "ファイルを書く", ("path", "content"), (), "write", True),
    Tool("edit_file", "Replace one exact piece of text in a file. args: path, old (exact existing text, must appear once), new.", "ファイルを編集", ("path", "old", "new"), (), "write", True),
    Tool("apply_patch", "Apply a unified diff patch cleanly to one or more files in the workspace (backs up modified files). "
                        "args: patch (unified diff starting with ---/+++ and @@ hunks).",
         "パッチを適用", ("patch",), (), "write", True),
    Tool("append_file", "Add text to the end of a file (creates it if missing). args: path, content.", "ファイルに追記", ("path", "content"), (), "write", True),
    Tool("write_code", "Create or rewrite a file (code, HTML, CSS, config, long text) from a description: a writer AI writes the "
                       "complete content. Use this instead of write_file for anything longer than about 15 lines. "
                       "args: path, instructions (what the file must contain: features, function and class names, how it uses "
                       "the other files, the language of messages).", "ファイルを書く(AIが執筆)", ("path", "instructions"), (), "write", True),
    Tool("make_folder", "Create a folder in the workspace. args: path.", "フォルダを作成", ("path",), (), "write"),
    Tool("move_file", "Move or rename a file inside the workspace. args: path, to. path can also be a pattern such as "
                      "\"photos/*2023*\": then every matching file is moved into the folder to (made if missing).",
         "ファイルを移動", ("path", "to"), (), "write", True),
    Tool("copy_file", "Copy a file inside the workspace. args: path, to. path can also be a pattern such as \"*.txt\": then "
                      "every matching file is copied into the folder to.", "ファイルをコピー", ("path", "to"), (), "write", True),
    Tool("delete_file", "Delete a file (a backup is kept). args: path.", "ファイルを削除", ("path",), (), "write", True),
    Tool("restore_file", "Undo: bring back the most recent backup of a file. args: path.", "ファイルを元に戻す", ("path",), (), "write", True),
    Tool("regex_replace", "Find and replace with a regular expression across a file or glob pattern (backs up touched files). "
                          "args: path (file or glob like \"*.py\"), pattern, replacement, dry_run (\"yes\" to preview only).",
         "正規表現で一括置換", ("path", "pattern", "replacement"), ("dry_run",), "write", True),
    Tool("archive", "Pack, unpack, or list a .zip or .tar.gz archive inside the workspace safely. "
                    "args: action (pack / unpack / list), path (source folder/file for pack, or archive file for unpack/list), to (destination path).",
         "圧縮・展開", ("action", "path"), ("to",), "write", True),
    # ---- 文書(Word・Excel・PowerPoint)・グラフ ----
    Tool("make_word", "Create or append to a Word document (.docx). args: path, content (Markdown: # headings, - bullets, 1. numbered lists, "
                      "| tables |, **bold**, ``` code blocks, --- for a page break), title (optional), mode (\"overwrite\" or \"append\").",
         "Word 文書を作成", ("path", "content"), ("title", "mode"), "write", True),
    Tool("make_excel", "Create an Excel workbook (.xlsx). args: path, data (CSV text, a Markdown table, or JSON: a list of rows, "
                       "a list of objects, or {\"sheet name\": rows}). The first row is the header (row 1), data starts at row 2. "
                       "Write totals and calculations as Excel formulas starting with = instead of computing them yourself. "
                       "=SUM(above) (also AVERAGE/MAX/MIN/COUNT) means all data rows above in the same column, "
                       "e.g. CSV data \"月,売上\\n4月,1200\\n5月,1350\\n6月,1280\\n合計,=SUM(above)\". Numbers like 1,200 and 15% are stored as numbers.",
         "Excel ブックを作成", ("path", "data"), (), "write", True),
    Tool("make_slides", "Create or append to a PowerPoint presentation (.pptx). args: path, content (Markdown: each '# ' heading starts a new slide, "
                        "'- ' lines are bullet points, indent two spaces for sub-points), mode (\"overwrite\" or \"append\").",
         "PowerPoint を作成", ("path", "content"), ("mode",), "write", True),
    Tool("make_chart", "Generate a clean chart (.svg or interactive .html report with table) from CSV, Markdown table, or JSON data. "
                       "args: path (ending in .svg or .html), data, kind (bar / line / pie, default bar), title (optional).",
         "グラフを作成", ("path", "data"), ("kind", "title"), "write", True),
    Tool("convert_file", "Convert a file between formats (.json/.yaml/.toml/.csv/.tsv/.md/.txt/.html/.docx/.xlsx/.pptx/.pdf). "
                         "args: path (source file), to (destination file whose extension selects output format).",
         "ファイル形式を変換", ("path", "to"), (), "write", True),
    Tool("image_edit", "Inspect or process images (resize, crop, rotate, flip_h, flip_v, grayscale, blur, sharpen, contrast, watermark, convert incl. .pdf, collage, info). "
                       "args: action, path (source image), to (output path; optional for info or in-place), width, height, box (\"l,t,r,b\"), angle, text, extras (comma/JSON paths for collage).",
         "画像を加工・変換", ("action", "path"), ("to", "width", "height", "box", "angle", "text", "extras"), "write", True),
    Tool("qr_barcode", "Generate a QR code (.svg/.png) or barcode (.png: code128, code39, ean13, ean8, upca, datamatrix, aztec, pdf417). "
                       "args: text, path, format (default qr), scale (default \"8\").",
         "QR/バーコード生成", ("text", "path"), ("format", "scale"), "write", True),
    # ---- 実行 ----
    Tool("run_file", "Run a program file (.py, .js, .ps1, .bat, .cmd, .sh) and get its output. The file's own folder is the current "
                     "folder, so files it writes land next to it. Use this to run a script you wrote. A server or a window app is "
                     "started in the background instead (test a server with check_url). args: path, args (command-line arguments, optional).",
         "ファイルを実行", ("path",), ("args",), "run", True),
    Tool("run_python", "Run a short piece of Python code written in args.code (a quick calculation or check) and get its printed "
                       "output. It can not run a file by name: for a .py file use run_file. args: code.", "Python を実行", ("code",), (), "run", True),
    Tool("run_command", "Run a shell command in the workspace folder (e.g. tests, git, pip install) and get its output. "
                        "It must finish within 90 seconds; a command that starts a server is started in the background instead. "
                        "args: command.", "コマンドを実行", ("command",), (), "run", True),
    Tool("start_process", "Start a long-running command in the background (a web server, a watcher) and get its first output. "
                          "args: command, name (short id, optional), keep (\"yes\" to leave it running after the task; otherwise it is "
                          "stopped when the task ends).", "常駐プログラムを起動", ("command",), ("name", "keep"), "run", True),
    Tool("process_output", "Read the latest output of a background process and whether it is still running. args: name.",
         "常駐プログラムの出力", ("name",), (), "run"),
    Tool("stop_process", "Stop a background process started with start_process. args: name.", "常駐プログラムを停止", ("name",), (), "run"),
    Tool("check_url", "Open a URL on this PC (http://localhost:PORT/...) and get the status code and the start of the page. "
                      "Use it to test a server you started. args: url.", "ローカルのURLを確かめる", ("url",), (), "run"),
    # ---- PC操作 ----
    Tool("system_info", "Show this PC's status: OS, CPU, memory, disks, battery, GPU, current time. no args.", "PCの状態", (), (), "pc"),
    Tool("list_windows", "List the titles of open windows. no args.", "ウィンドウ一覧", (), (), "pc"),
    Tool("list_processes", "List running programs that use the most memory. no args.", "実行中のプログラム", (), (), "pc"),
    Tool("screenshot", "Look at the screen: takes a screenshot and returns a description. args: question (what to look for, optional).", "画面を見る", (), ("question",), "pc"),
    Tool("clipboard_read", "Read the text in the clipboard. no args.", "クリップボードを読む", (), (), "pc"),
    Tool("clipboard_write", "Put text into the clipboard. args: text.", "クリップボードに書く", ("text",), (), "pc"),
    Tool("open", "Open an app, file, folder or web page. args: target (app name like \"notepad\", a path, or a URL).", "開く", ("target",), (), "pc", True),
    Tool("focus_window", "Bring a window to the front. args: title (part of the window title).", "ウィンドウを前面に", ("title",), (), "pc"),
    Tool("close_window", "Ask a window to close. args: title (part of the window title).", "ウィンドウを閉じる", ("title",), (), "pc", True),
    Tool("type_text", "Type text with the keyboard. args: text, window (part of the target window title; it is brought to the front "
                       "and checked first. Always give it).", "文字を入力", ("text",), ("window",), "pc", True),
    Tool("press_keys", "Press keys or shortcuts. args: keys (e.g. \"enter\", \"ctrl+s\", \"ctrl+a ctrl+c\"), window (part of the "
                        "target window title, checked first).", "キーを押す", ("keys",), ("window",), "pc", True),
    Tool("click", "Click the mouse at a screen position in pixels. args: x, y, button (left/right/double, default left).", "クリック", ("x", "y"), ("button",), "pc", True),
    Tool("scroll", "Scroll the mouse wheel. args: amount (positive = up, negative = down, e.g. \"-3\").", "スクロール", ("amount",), (), "pc"),
    Tool("volume", "Change the sound volume. args: action (up / down / mute).", "音量", ("action",), (), "pc"),
    Tool("media", "Control music/video playback. args: action (play_pause / next / previous / stop).", "再生の操作", ("action",), (), "pc"),
    Tool("speak", "Speak text aloud via Windows speech synthesis, or save spoken speech to a .wav file in the workspace. "
                  "args: text, path (optional .wav path), rate (\"-10\"..\"10\", default \"0\").",
         "音声読み上げ", ("text",), ("path", "rate"), "pc"),
    Tool("notify_pc", "Show a Windows desktop balloon notification. args: title, text.", "デスクトップ通知", ("title", "text"), (), "pc"),
    Tool("network_info", "Inspect local IPv4 addresses and listening TCP ports, and optionally test DNS/TCP reachability. "
                         "args: host (optional hostname/IP), port (optional port number).",
         "ネットワーク診断", (), ("host", "port"), "pc"),
    Tool("wait", "Wait a moment (e.g. for an app to open). args: seconds (max 10).", "待つ", ("seconds",), (), "pc"),
    # ---- ネット ----
    Tool("web_search", "Search public knowledge via DuckDuckGo Instant Answer, Wikipedia Search API, and News RSS. "
                       "args: query, lang (\"ja\" or \"en\", default ja), limit (default \"8\").",
         "Web検索", ("query",), ("lang", "limit"), "net"),
    Tool("web_extract", "Extract links list, HTML tables (as Markdown), or metadata/headings from a public webpage. "
                        "args: url, mode (links / tables / meta, default links), limit (default \"40\").",
         "Web構造抽出", ("url",), ("mode", "limit"), "net"),
    Tool("download_file", "Download a public URL directly into a workspace file (requires write permission). args: url, path.",
         "ファイルをダウンロード", ("url", "path"), (), "net", True),
    Tool("wikipedia", "Look up a topic in Wikipedia. args: query, lang (\"ja\" or \"en\", default ja).", "Wikipedia を調べる", ("query",), ("lang",), "net"),
    Tool("news", "Fetch latest news headlines via public RSS feeds. args: topic (optional keyword/section), lang (\"ja\" or \"en\", default ja), limit (default \"8\").",
         "最新ニュース", (), ("topic", "lang", "limit"), "net"),
    Tool("fetch_url", "Read the text of a web page. args: url.", "ウェブページを読む", ("url",), (), "net"),
    Tool("http_request", "Send an HTTP request (GET, POST, PUT, PATCH, DELETE) to a public URL / JSON API. "
                         "args: url, method (default GET), headers (JSON object string, optional), body (optional).",
         "HTTP API 通信", ("url",), ("method", "headers", "body"), "net", True),
    # ---- 知識ベース・記憶 ----
    Tool("kb_search", "Search the local knowledge base (indexed files, PDFs, notes, URLs). args: query, top_k (default \"5\").",
         "知識ベース検索", ("query",), ("top_k",), "kb"),
    Tool("kb_add", "Save a note, workspace file, or public URL to the persistent knowledge base. "
                   "args: title, content (or pass path / url), path (optional), url (optional), tags (optional).",
         "知識ベースに登録", ("title",), ("content", "path", "url", "tags"), "kb"),
    Tool("save_memory", "Remember, list, or forget a personal preference/fact about the user. "
                        "args: action (remember / forget / list), fact (text to remember or forget).",
         "メモリ操作", ("action",), ("fact",), "memory"),
]}


def available_tools(ctx: ToolContext) -> list[Tool]:
    groups = {"calc"}
    if ctx.delegate is not None:
        groups.add("team")
    if ctx.kb is not None:
        groups.add("kb")
    if ctx.memorize is not None:
        groups.add("memory")
    if ctx.workspace is not None:
        groups.add("files")
        if ctx.allow_write:
            groups.add("write")
    if ctx.allow_run:
        groups.add("run")
    if ctx.allow_pc and pc.SUPPORTED:
        groups.add("pc")
    if ctx.allow_net:
        groups.add("net")
    out = []
    for t in TOOLS.values():
        if t.group not in groups:
            continue
        if t.name == "download_file" and (ctx.workspace is None or not ctx.allow_write):
            continue
        out.append(t)
    return out


def step_schema(tools: list[Tool], plan_steps: int = 0) -> dict:
    """AIの1手を表す JSON スキーマ。道具ごとに必須の引数を強制し、引数なしの空振りを防ぐ。

    plan_steps > 0 なら、今どの手順に取り組んでいるか(step)も必ず書かせる(進み具合の表示と、段取りの維持のため)。
    """
    text = {"type": "string"}
    head = ["thought", "step"] if plan_steps else ["thought"]
    step = {"step": {"type": "integer", "minimum": 1, "maximum": plan_steps}} if plan_steps else {}
    variants = [{
        "type": "object", "required": head + ["tool", "args"],
        "properties": {"thought": text, **step, "tool": {"type": "string", "enum": [t.name]},
                       "args": {"type": "object", "required": list(t.required),
                                "properties": {a: text for a in t.required + t.optional}}},
    } for t in tools]
    variants.append({"type": "object", "required": ["thought", "tool", "answer"],
                     "properties": {"thought": text, "tool": {"type": "string", "enum": ["answer"]}, "answer": text}})
    return {"anyOf": variants}


def unavailable_notes(ctx: ToolContext) -> list[str]:
    """今の設定ではできないこと。AIに伝えて、できたふりをさせない。"""
    notes = []
    if ctx.workspace is None:
        notes.append("reading, listing or searching files (no workspace folder is set)")
    if ctx.workspace is None or not ctx.allow_write:
        notes.append("creating, changing or deleting files")
    if not ctx.allow_run:
        notes.append("running code or commands")
    if not (ctx.allow_pc and pc.SUPPORTED):
        notes.append("controlling the PC (opening apps, typing, clicking, seeing the screen)")
    if not ctx.allow_net:
        notes.append("accessing the internet")
    return notes


# ---- 計算 -------------------------------------------------------------------

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil,
          "log": math.log, "log10": math.log10, "log2": math.log2, "exp": math.exp,
          "sin": math.sin, "cos": math.cos, "tan": math.tan, "radians": math.radians, "degrees": math.degrees,
          "factorial": math.factorial, "gcd": math.gcd, "lcm": math.lcm, "comb": math.comb, "perm": math.perm}
_CONSTS = {"pi": math.pi, "e": math.e}


def _calc_fallback(expression: str, original_err: Exception) -> str:
    try:
        sym = symcalc.sym_evaluate(expression)
    except (ValueError, TypeError, SyntaxError, ArithmeticError) as e:
        raise ToolError(f"計算できません: {e}") from e
    if sym is not None:
        return sym
    raise ToolError(str(original_err)) from original_err


def calc(expression: str) -> str:
    text = unicodedata.normalize("NFKC", expression).replace("×", "*").replace("÷", "/").replace("^", "**")
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)  # 1,000 → 1000
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        return _calc_fallback(expression, ToolError(f"式として読めません: {expression}"))

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e6):
                raise ToolError("べき乗が大きすぎます")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ToolError("使えるのは数値、+ - * / // % **、括弧、sqrt などの基本的な関数だけです")

    try:
        value = ev(tree)
    except ToolError as e:
        if "べき乗" in str(e):
            raise
        return _calc_fallback(expression, e)
    except (ArithmeticError, ValueError, TypeError) as e:
        raise ToolError(f"計算できません: {e}") from e
    if isinstance(value, float):
        value = round(value, 10)  # 0.1+0.2 のような浮動小数点の端数を落とす
        if value.is_integer() and abs(value) < 1e15:
            value = int(value)
    return str(value)


# ---- ファイル ---------------------------------------------------------------

def _resolve(ctx: ToolContext, path: str) -> Path:
    if ctx.workspace is None:
        raise ToolError("作業フォルダが設定されていません")
    root = ctx.workspace.resolve()
    raw = (path or ".").strip()
    if ctx.anywhere and (Path(raw).expanduser().is_absolute() or raw.startswith("~")):
        return Path(os.path.expandvars(raw)).expanduser().resolve()  # 「すべて許可」: PC のどこでも扱える
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ToolError("作業フォルダの外にはアクセスできません" + ("" if ctx.anywhere else "(権限を「すべて許可」にすると扱えます)"))
    return target


def _relative(ctx: ToolContext, target: Path) -> str:
    """作業フォルダからの位置(外のファイルなら絶対パス)。"""
    try:
        return target.relative_to(ctx.workspace.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def _newline_of(target: Path) -> str:
    """ファイルの改行の形(CRLF か LF)。新しいファイルは、この PC の普通の形。"""
    try:
        head = target.read_bytes()[:65536] if target.is_file() else b""
    except OSError:
        head = b""
    if b"\r\n" in head:
        return "\r\n"
    if b"\n" in head:
        return "\n"
    return os.linesep


def _encode(text: str, newline: str) -> bytes:
    """改行をそろえて UTF-8 にする(AIの書いた \\r\\n と \\n が混ざっても、ファイルの形に合わせる)。"""
    return text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")


def _read_text(path: Path, limit: int) -> str:
    data = path.read_bytes()[: limit * 4]
    if b"\x00" in data:
        raise ToolError("テキストファイルではありません")
    return data.decode("utf-8", "replace")[:limit]


def _backup(ctx: ToolContext, target: Path) -> None:
    """書き換え・削除の前に、今の内容をバックアップへ写す。"""
    if ctx.backup_dir is None or not target.is_file():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    rel = _relative(ctx, target)
    if Path(rel).is_absolute():  # 作業フォルダの外のファイル: ドライブ名を含めた形で残す
        rel = "_outside/" + rel.replace(":", "").lstrip("/")
    copy = ctx.backup_dir / stamp / rel
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, copy)


def list_files(ctx: ToolContext, path: str = ".") -> str:
    target = _resolve(ctx, path)
    if target.is_file():  # ファイルを渡した(「フォルダがありません」を見て、ファイルが無いと思い込んでいた。実際にあった)
        return (f"{_relative(ctx, target)}  ({target.stat().st_size} bytes)(これはフォルダではなく、あるファイルです。中身は "
                "read_file で読めます)")
    if not target.is_dir():
        raise ToolError(f"フォルダがありません: {path}")
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = [f"{_relative(ctx, p)}/{_folder_peek(p)}" if p.is_dir() else f"{_relative(ctx, p)}  ({p.stat().st_size} bytes)"
             for p in entries[:200]]
    if len(entries) > 200:
        lines.append(f"... 他 {len(entries) - 200} 件")
    return "\n".join(lines) or "(空のフォルダ)"


def _folder_peek(folder: Path, names: int = 6) -> str:
    """一覧に添える、フォルダの中身の短い説明(「photos/」だけでは中の名前が分からず、フォルダをそのまま移そうとしていた)。"""
    if folder.name in _SKIP_DIRS:
        return "  (中は省略)"
    try:
        entries = list(itertools.islice(folder.iterdir(), 1000))
    except OSError:
        return ""
    if not entries:
        return "  (空)"
    files = sorted(p.name for p in entries if p.is_file())
    folders = sum(1 for p in entries if p.is_dir())
    more = "以上" if len(entries) >= 1000 else ""
    parts = [f"フォルダ {folders} 件{more}"] if folders else []
    if files:
        parts.append(f"ファイル {len(files)} 件{more}: " + ", ".join(files[:names]) + (" など" if len(files) > names else ""))
    return "  (" + "、".join(parts) + ")"


def tree(ctx: ToolContext, path: str = ".", depth: int = 3, limit: int = 300) -> str:
    """フォルダの構成(字下げで階層を表す)。大きな生成物のフォルダは中を見ない。"""
    top = _resolve(ctx, path)
    if not top.is_dir():
        raise ToolError(f"フォルダがありません: {path}")
    lines: list[str] = []

    def walk(folder: Path, level: int) -> None:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if len(lines) >= limit:
                return
            if entry.is_dir():
                skipped = entry.name in _SKIP_DIRS
                lines.append("  " * level + entry.name + "/" + ("  (中は省略)" if skipped else ""))
                if not skipped and level + 1 < depth:
                    walk(entry, level + 1)
            else:
                lines.append("  " * level + f"{entry.name}  ({entry.stat().st_size} bytes)")

    walk(top, 0)
    if len(lines) >= limit:
        lines.append(f"... ({limit}件で打ち切り)")
    head = "./" if top == ctx.workspace.resolve() else _relative(ctx, top) + "/"
    return head + "\n" + ("\n".join(lines) if lines else "(空のフォルダ)")


def find_files(ctx: ToolContext, pattern: str) -> str:
    root = _resolve(ctx, ".")
    hits = []
    for file in root.rglob("*"):
        if _SKIP_DIRS & set(file.relative_to(root).parts):
            continue
        if fnmatch.fnmatch(file.name.lower(), pattern.lower()):
            hits.append(_relative(ctx, file) + ("/" if file.is_dir() else ""))
            if len(hits) >= 100:
                hits.append("... (100件で打ち切り)")
                break
    return "\n".join(hits) or "見つかりませんでした"


def _refuse_office(target: Path) -> None:
    """Word・PowerPoint のファイルは中身が ZIP なので、文字として部分的に書き換えると壊れる。作り直しへ誘導する。"""
    if target.suffix.lower() in (".docx", ".pptx"):
        tool = {".docx": "make_word", ".pptx": "make_slides"}[target.suffix.lower()]
        raise ToolError(f"{target.name} は文字として部分的に書き換えられません。{tool} で、直した内容全体を"
                        "同じ名前で作り直してください(元のファイルはバックアップされます)")


def _edit_sheet(ctx: ToolContext, target: Path, change: Callable[[str], str]) -> None:
    """Excel のブックを、CSV として読んだ中身(read_file で見える形)に対する変更で書き換える。数式もそのまま残る。"""
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {_relative(ctx, target)}")
    try:
        text = office.read_text(target, limit=10_000_000)
    except (ValueError, KeyError, OSError) as e:
        raise ToolError(f"読めませんでした: {e}") from e
    new_text = change(text)
    _backup(ctx, target)
    office.write_xlsx(target, office.sheets_from_text(new_text))


def read_file(ctx: ToolContext, path: str, start: str = "1") -> str:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {path}")
    ctx.read.add(_relative(ctx, target))
    if target.suffix.lower() in office.READABLE_DOC_SUFFIXES:  # Word・Excel・PowerPoint・PDF は中身の文字を取り出して読む
        try:
            text = office.read_text(target)
            if target.suffix.lower() == ".xlsx":
                text = office.with_results(text)  # 合計などの数式の結果も見せる(表が正しいかを確かめられるように)
            return f"[{target.name} の中身(文字だけを取り出したもの)]\n" + (text or "(文字がありません)")
        except (ValueError, KeyError, OSError) as e:
            raise ToolError(f"読めませんでした: {e}") from e
    lines = _read_text(target, 400_000).splitlines()
    try:
        first = max(1, int(start))
    except ValueError as e:
        raise ToolError("start は行番号(数字)で指定してください") from e
    if not lines:
        return "(空のファイル)"
    chunk, size = [], 0
    for line in lines[first - 1 : first - 1 + 400]:
        size += len(line) + 1
        if size > 12000 and chunk:
            break
        chunk.append(line)
    last = first - 1 + len(chunk)
    note = "" if first == 1 and last >= len(lines) else f"\n[{first}〜{last} 行目を表示。全 {len(lines)} 行。続きは start=\"{last + 1}\"]"
    return "\n".join(chunk) + note


def search_files(ctx: ToolContext, query: str, path: str = ".") -> str:
    if not query.strip():
        raise ToolError("検索する語が空です")
    target = _resolve(ctx, path)
    root = ctx.workspace.resolve()
    needle, hits, scanned = query.strip().lower(), [], 0
    pattern = bool(_PATTERN.search(needle))  # IMG_2023_* のようなパターンは、ファイルの名前で探す
    named: list[str] = []
    files = [target] if target.is_file() else (p for p in target.rglob("*") if p.is_file())
    for file in files:
        rel = file.relative_to(root).as_posix()
        if _SKIP_DIRS & set(file.relative_to(root).parts):
            continue
        name = file.name.lower()
        if (fnmatch.fnmatch(name, needle) or fnmatch.fnmatch(rel.lower(), needle)) if pattern else needle in name:
            named.append(rel)
        if pattern or file.stat().st_size > 1_000_000:
            continue
        scanned += 1
        if scanned > 3000:
            break
        try:
            text = _read_text(file, 1_000_000)
        except (ToolError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                if len(hits) >= 50:
                    break
        if len(hits) >= 50:
            break
    found = "\n".join(hits) + ("\n... (50件で打ち切り)" if len(hits) >= 50 else "")
    if named:
        names = "名前が合うファイル:\n" + "\n".join(named[:100]) + (f"\n... ほか {len(named) - 100} 件" if len(named) > 100 else "")
        return names + (f"\n中身に含むもの:\n{found}" if hits else "")
    return found or "見つかりませんでした"


COPY_KEEPS = "(写したので、元のファイルも元の場所に残っています。移す・分けるときは move_file を使います)"
CODE_PROBLEM = "注意(コードの誤り)"  # 書いたコードが、動かす前から誤っている(エージェントは失敗として直させる)
STRING_NEWLINES = ("(文字列の中に改行が入って、文字列が閉じていませんでした。JSON の \\n が改行になったものとみなして、"
                   "\\n の2文字に戻して保存しました)")
MARKUP_IN_PYTHON = "この Python のファイルの中身は、HTML のページや CSS です"
NO_CHANGE = ("この編集をしても、ファイルの中身は変わりません(new の内容は、もうその場所に入っています)。この変更は、もう済んで"
             "います: ファイルは変えていません。同じ編集を繰り返さずに、動かして確かめるか、次の手順へ進んでください。まだうまく"
             "動かないなら、原因は別のところにあります: 動かした結果やページの確認を読み直してください")
PAGE_NEWLINES = ("(JavaScript の中に、改行のつもりの \\n の2文字が入っていて、ブラウザーでは構文の誤りで動かない形でした。"
                 "本当の改行に直して保存しました)")
_PAGE_SUFFIXES = (".html", ".htm", ".js", ".mjs")


def _around(code: str, lineno: int | None, span: int = 3) -> str:
    """その行のまわり(行番号つき。その行には > の印)。"""
    lines = code.splitlines()
    if not lineno or not lines:
        return ""
    lineno = min(max(lineno, 1), len(lines))
    start, end = max(1, lineno - span), min(len(lines), lineno + span - 1)
    return "\n".join(f"{n:>4}{'>' if n == lineno else ' '} {lines[n - 1]}" for n in range(start, end + 1))


def _syntax_problem(name: str, code: str) -> str:
    """Python の構文の誤りの説明とまわりの行(誤りがなければ空)。"""
    try:
        compile(code, name, "exec")
    except SyntaxError as e:
        return f"SyntaxError: {e.msg}({e.lineno} 行目)\n{_around(code, e.lineno)}"
    except ValueError as e:  # ヌル文字など
        return f"{type(e).__name__}: {e}"
    return ""


def _breaks_python(target: Path, before: str, after: str) -> str:
    """構文の正しかった Python のファイルを壊す変更なら、その説明(壊さないなら空)。もともと壊れているものは止めない。"""
    if target.suffix.lower() not in (".py", ".pyw") or _syntax_problem(target.name, before):
        return ""
    problem = _syntax_problem(target.name, after)
    if not problem:
        return ""
    return (f"この変更をすると Python の構文が壊れるため、ファイルは変えていません。変えたあとの形: {problem}\n"
            "まわりの行と字下げのそろった Python にしてください(エラーの文ではなく、ファイルにある行を old にします)。"
            "プログラム全体を書き直すなら write_file を使ってください")


def _script_note(target: Path, shown: str) -> str:
    """書いたページ(HTML)か .js の、JavaScript の構文の誤りの注意(node で確かめる。無い・確かめられなければ空)。"""
    try:
        text = _read_text(target, 400_000).replace("\r\n", "\n")
    except ToolError:
        return ""
    found = script_problem(target.name, text)
    if not found:
        return ""
    line, message = found
    lines = text.split("\n")
    stray = 0 < line <= len(lines) and "\\n" in lines[line - 1]
    return (f"\n{CODE_PROBLEM}: {shown or target.name} の JavaScript に構文の誤りがあり、ブラウザーではこの <script> が動きません"
            f"(時計や表示の更新も止まります)。{message}({line} 行目)\n{_around(text, line)}"
            + ("\n(この行には \\n の2文字があります。改行のつもりなら、本当の改行にしてください)" if stray else ""))


def _code_note(target: Path, shown: str = "") -> str:
    """書いた Python のファイルの、動かす前に分かる誤り(構文・定義も import もしていない名前)の注意。shown は見せる道のり。

    ページ(HTML)と .js のファイルは、JavaScript の構文を確かめる。
    """
    if target.suffix.lower() in _PAGE_SUFFIXES:
        return _script_note(target, shown)
    if target.suffix.lower() not in (".py", ".pyw"):
        return ""
    try:
        code = _read_text(target, 400_000).replace("\r\n", "\n")  # edit_file と同じく、改行は \n にそろえて扱う
    except ToolError:
        return ""
    problem = _syntax_problem(target.name, code)
    if problem and markup_in_python(code):  # 中身が HTML のページ(Python のコードではない): 1行ずつ直しても直らない
        return (f"\n{CODE_PROBLEM}: {MARKUP_IN_PYTHON}(先頭: {code.strip().splitlines()[0][:60]})。Python として動かせません。"
                f"edit_file では直りません: write_code で {shown or target.name} を、Python のコードとして書き直してください"
                "(Flask なら、ページは templates/index.html に分け、render_template('index.html') で返します)。")
    if problem:
        return f"\n{CODE_PROBLEM}: Python の構文の誤りがあり、このままでは動きません。先に直してください。{problem}"
    missing = undefined_names(code)
    if not missing:
        return (_order_note(target, code, shown) or _template_note(target, code, shown)
                or _datetime_note(target, code, shown) or _duplicate_note(target, code, shown) or _api_note(target, code, shown))
    names = "、".join(f"{name}({line} 行目)" for name, line in missing[:4])
    flask = any(name == "app" for name, _ in missing) and "flask" in code.lower()
    fixes = suggest_fixes(code)
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: 定義も import もしていない名前を使っています: {names}。このままでは動かすと NameError で止まります"
            + ("(Flask なら import のあとに app = Flask(__name__) の行が要ります)" if flask else "")
            + "。先に直してください(import を足す、使う前に値を入れる、など)。"
            + (f"\n直し方(この引数のまま edit_file に使えます):{how}" if how else ""))


def _api_note(target: Path, code: str, shown: str) -> str:
    """外の API で頼んでいない項目を、コードが読んでいる(動かすと KeyError か「不明」になる)。"""
    found = knowhow.open_meteo_unrequested(code)
    if not found:
        return ""
    fixes = [{"old": old, "new": new} for _, _, old, new in found if old]
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: {knowhow.unrequested_note(code)}。"
            + (f"\n直し方(この引数のまま edit_file に使えます):{how}" if how else ""))


def _template_note(target: Path, code: str, shown: str) -> str:
    """Flask の render_template に templates/ から書いた名前を渡している(templates/templates/... を探して見つからない)。"""
    fixes = [fix for name, fix in template_path_fixes(code) if not (target.parent / "templates" / name).is_file()]
    if not fixes:
        return ""
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: render_template には、templates フォルダの中での名前だけを渡します(例: 'index.html')。"
            "'templates/index.html' と書くと templates/templates/index.html を探すので、ページを開くと TemplateNotFound に"
            f"なります。\n直し方(この引数のまま edit_file に使えます):{how}")


def _datetime_note(target: Path, code: str, shown: str) -> str:
    """import の形と合わない datetime の使い方(動かすと AttributeError)。"""
    fixes = datetime_fixes(code)
    if not fixes:
        return ""
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: datetime の使い方が import の形と合っていません。このままでは動かすと AttributeError で止まります"
            "(from datetime import datetime なら datetime.now()、import datetime なら datetime.datetime.now() と書きます)。"
            f"\n直し方(この引数のまま edit_file に使えます):{how}")


def _order_note(target: Path, code: str, shown: str) -> str:
    """作る前に使っている名前(上から順に動かすと NameError)と、Flask の app の作り直し(ページが消える)。"""
    problem, fixes = flask_order_fixes(code)
    if not problem:
        early = used_before_defined(code)
        if not early:
            return ""
        problem = ("作る前に使っている名前があります: " + "、".join(f"'{name}'({use} 行目。作るのは {made} 行目)"
                                                              for name, use, made in early)
                   + "。Python はファイルの上から順に動くので、このままでは動かすと NameError で止まります。作る行を、使う所より上に"
                     "移してください")
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: {problem}。" + (f"\n直し方(この順に、この引数のまま edit_file に使えます):{how}" if how else ""))


def _duplicate_note(target: Path, code: str, shown: str) -> str:
    """Flask の @app.route を付けた関数の名前が重なっている(起動したときに AssertionError で止まる)。"""
    found = duplicate_routes(code)
    if not found:
        return ""
    names = "、".join(f"{name}({'・'.join(str(n) for n in lines)} 行目)" for name, lines, _ in found[:3])
    fixes = [fix for _, _, fix in found if fix][:2]
    how = "".join(f"\nedit_file {json.dumps({'path': shown or target.name, **fix}, ensure_ascii=False)}" for fix in fixes)
    return (f"\n{CODE_PROBLEM}: @app.route を付けた関数の名前が重なっています: {names}。Flask では関数の名前がページの名前"
            "(endpoint)になるので、このままでは起動したときに AssertionError: View function mapping is overwriting an "
            "existing endpoint function で止まります。同じものの写しなら片方を消し、別の働きなら片方の名前を変えてください。"
            + (f"\n直し方(後ろの写しを消します。この引数のまま edit_file に使えます):{how}" if how else ""))


def _adds_duplicate(target: Path, before: str, after: str) -> str:
    """変更で、ファイルにもうある関数やクラスと同じ名前のものが増えるなら、その説明(増えなければ空)。"""
    if target.suffix.lower() not in (".py", ".pyw"):
        return ""
    was, now = top_level_defs(before), top_level_defs(after)
    if not was or now is None:
        return ""
    dup = [(name, was[name]) for name, nodes in now.items() if name in was and len(nodes) > len(was[name])]
    if not dup:
        return ""
    where = "、".join(f"{name}({'・'.join(str(n.lineno) for n in nodes)} 行目)" for name, nodes in dup[:3])
    return (f"足そうとした関数(またはクラス)は、もうファイルにあります: {where}。同じ名前のものが2つになるので、ファイルは"
            "変えていません(Flask で @app.route を付けた関数の名前が重なると、起動したときに AssertionError で止まります)。"
            "もう入っているなら、足さずに次へ進んでください。今あるものを直すなら、old に今の関数の全体(def の行から最後の"
            "行まで)を入れて、new に直した関数を書いてください。別の働きのものなら、別の名前にしてください")


def _drop_copies(target: Path, text: str, old: str, new: str) -> tuple[str, str]:
    """new の中の、ファイルの(old の外の)関数やクラスと中身まで同じ写しを除く。戻り値は (残りの new, 知らせる文)。

    小さいAIは、前の編集の new を使い回して、もうある関数をまた足そうとしがち(断ると、消そうとして迷っていた)。
    同じ中身なら足しても何も増えないので、除いて残りを反映すればよい。除くと何も残らないなら、そのまま返す。
    """
    if target.suffix.lower() not in (".py", ".pyw") or not new.strip() or textwrap.dedent(new) != new or old not in text:
        return new, ""
    have, adding = top_level_defs(text), top_level_defs(new)
    if not have or not adding:
        return new, ""
    start = text.count("\n", 0, text.index(old))
    touched = set(range(start, start + old.count("\n") + 1))  # old が占める行(0 から)
    file_lines, new_lines = text.split("\n"), new.split("\n")

    def body(lines: list[str], node: ast.AST) -> list[str]:
        a, b = block_lines(node)
        return [line.rstrip() for line in lines[a:b]]

    drop: list[tuple[int, int]] = []
    names = []
    for name, nodes in adding.items():
        if name not in have or len(nodes) != 1:
            continue
        outside = [n for n in have[name] if not touched & set(range(*block_lines(n)))]
        if any(body(file_lines, n) == body(new_lines, nodes[0]) for n in outside):
            drop.append(block_lines(nodes[0]))
            names.append(name)
    if not drop:
        return new, ""
    kept = [line for i, line in enumerate(new_lines) if not any(a <= i < b for a, b in drop)]
    rest = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")
    if not rest.strip():
        return new, ""
    return rest + ("\n" if new.endswith("\n") else ""), (
        f"\n(new にあった {'、'.join(names)} は、ファイルにあるものと中身まで同じなので、もう一度は足していません)")


def _current_def(text: str, old: str) -> str:
    """old の def(または class)の行の関数が、今のファイルではどうなっているか(old にそのまま使える全体)。無ければ空。"""
    header = next((line.strip() for line in old.splitlines() if line.strip() and not line.strip().startswith("@")), "")
    m = _HEADER.match(header)
    defs = top_level_defs(text) if m else None
    nodes = (defs or {}).get(m.group(1), []) if m else []
    if len(nodes) != 1:
        return ""
    a, b = block_lines(nodes[0])
    block = "\n".join(text.split("\n")[a:b])
    if b - a > 40 or len(block) > 2500:
        return f"今のファイルの {m.group(1)} は {a + 1}〜{b} 行目です(長いので、read_file で確かめて、その一部を old にしてください)"
    return f"今のファイルの {m.group(1)}({a + 1}〜{b} 行目)は次のとおりです。old には、ここから写してください:\n{block}"


def old_hint(ctx: ToolContext, path: str, old: str) -> str:
    """同じ edit_file の失敗を繰り返すAIに見せる、old の関数の今の全体(無ければ空)。"""
    try:
        target = _resolve(ctx, path)
        text = _read_text(target, 2_000_000).replace("\r\n", "\n") if target.is_file() else ""
    except ToolError:
        return ""
    return _current_def(text, old.replace("\r\n", "\n")) if old and old not in text else ""


def code_hint(ctx: ToolContext, path: str) -> str:
    """そのファイルの今の、動かす前に分かる誤りと直し方(なければ空)。同じ失敗を繰り返すAIに、もう一度見せる。"""
    try:
        target = _resolve(ctx, path)
    except ToolError:
        return ""
    return _code_note(target, _relative(ctx, target)).strip() if target.is_file() else ""


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    target = _resolve(ctx, path)
    suffix = target.suffix.lower()
    if suffix in office.OFFICE_SUFFIXES:  # Word・Excel・PowerPoint の名前なら、その形式で作る(中身は Markdown か CSV)
        maker = {".docx": lambda t: office.write_docx(t, content), ".xlsx": lambda t: office.write_xlsx(t, content),
                 ".pptx": lambda t: office.write_pptx(t, content)}[suffix]
        return _office(ctx, path, suffix, maker)
    if target.is_dir():
        raise ToolError(f"フォルダと同じ名前です: {path}")
    existed = target.exists()
    newline = _newline_of(target)  # 上書きでも、元の改行の形(CRLF / LF)を保つ
    repaired = repair_string_newlines(content) if suffix in (".py", ".pyw") else None
    if repaired is not None:  # JSON の \n が改行になって、文字列が切れていた: \n に戻す
        content = repaired
    mended = repair_page_newlines(target.name, content) if suffix in _PAGE_SUFFIXES else None
    if mended is not None:  # 改行のつもりの \n(2文字)で、ページの JavaScript が動かない形だった: 本当の改行にする
        content = mended
    if ctx.by_agent and existed and target.is_file():
        try:
            same = _read_text(target, 2_000_000).replace("\r\n", "\n") == content.replace("\r\n", "\n")
        except ToolError:
            same = False
        if same:  # 直したつもりで、今と同じ中身を書いている(実際にあった): 何も変わらないことを伝える
            raise ToolError(f"書こうとした中身は、今の {_relative(ctx, target)} とまったく同じなので、何も変わりません(前の実行の"
                            "失敗は、この中身のままで起きています)。エラーの文を読んで、原因を直す違う中身にしてください。"
                            "(The file already has exactly this content, so nothing changed.)")
    _backup(ctx, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_encode(content, newline))
    return (f"{'上書きしました(元の内容はバックアップ済み)' if existed else '作成しました'}: {_relative(ctx, target)} ({len(content)} 文字)"
            + (f"\n{STRING_NEWLINES}" if repaired is not None else "") + (f"\n{PAGE_NEWLINES}" if mended is not None else "")
            + _code_note(target, _relative(ctx, target)))


def _make_excel(ctx: ToolContext, path: str, data: str) -> str:
    """Excel を作る。合計の行の数が上の数の合計と違えば、合計の数式に直して、そう伝える。"""
    notes: list[str] = []
    done = _office(ctx, path, ".xlsx", lambda t: office.write_xlsx(t, data, notes))
    return done + "".join(f"\n(直したこと: {note})" for note in notes)


def _office(ctx: ToolContext, path: str, suffix: str, write: Callable[[Path], Path]) -> str:
    """Word・Excel・PowerPoint のファイルを作業フォルダに作る(拡張子は自動で合わせる。上書きの前にバックアップ)。"""
    target = _resolve(ctx, path)
    if target.suffix.lower() != suffix:
        target = _resolve(ctx, _relative(ctx, target.with_suffix(suffix)) if target != ctx.workspace.resolve() else "document" + suffix)
    if target.is_dir():
        raise ToolError(f"フォルダと同じ名前です: {path}")
    existed = target.exists()
    _backup(ctx, target)
    try:
        write(target)
    except (ValueError, TypeError) as e:
        raise ToolError(f"中身を読み取れませんでした: {e}") from e
    done = f"{'上書きしました(元のファイルはバックアップ済み)' if existed else '作成しました'}: {_relative(ctx, target)} ({target.stat().st_size} bytes)"
    if suffix == ".xlsx":  # できた表を、数式の結果つきで見せる(範囲の誤りなどに、AIが自分で気づけるように)
        try:
            done += "\n[できた表]\n" + office.with_results(office.read_text(target, limit=3000))
        except (ValueError, KeyError, OSError):
            pass
    return done


def _reindent(lines: list[str], indent: str) -> list[str]:
    """行のかたまりを、共通の字下げを取ってから indent の深さにそろえる(中の相対的な字下げはそのまま)。"""
    filled = [line for line in lines if line.strip()]
    if not filled:
        return lines
    common = min(len(line) - len(line.lstrip()) for line in filled)
    return [indent + line[common:] if line.strip() else line for line in lines]


_TOKEN = re.compile(r"\w+|[^\w\s]")


def _only_added_or_removed(a: str, b: str) -> bool:
    """a と b の違いが、語の足し引き(と、引用符の違い)だけか。直す前の形の行と、直したあとの行の関係。

    語の単位で比べる(文字の単位だと、'other' と 'error' も文字の足し引きに見えてしまう)。
    """
    x, y = _TOKEN.findall(a), _TOKEN.findall(b)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if tag == "replace" and not set("".join(x[i1:i2] + y[j1:j2])) <= set("'\"`"):
            return False
    return True


def _outside_part(part: str, line: str) -> tuple[str, str]:
    """old(part)が行(line)の一部のとき、行のうち old に当たらない前と後ろ(<div class="weather"> と </div> など)。

    語の単位で比べて、行の頭や終わりにだけある語を探す(行の全体が old なら、どちらも空)。
    """
    x = _TOKEN.findall(part)
    spans = [m.span() for m in _TOKEN.finditer(line)]
    ops = difflib.SequenceMatcher(None, x, [line[a:b] for a, b in spans], autojunk=False).get_opcodes()
    if not ops:
        return "", ""
    head = tail = ""
    tag, i1, _, _, j2 = ops[0]
    if tag == "insert" and i1 == 0 and j2 < len(spans):
        head = line[:spans[j2][0]]
    tag, i1, _, j1, _ = ops[-1]
    if tag == "insert" and i1 == len(x) and j1 > 0:
        tail = line[spans[j1 - 1][1]:]
    return head, tail


_TOP_LEVEL = re.compile(r"(import|from|def|class|async\s+def)\s|@|if\s+__name__")


def _loose_replace(text: str, old: str, new: str) -> tuple[str, str] | None:
    """old がそのままでは見つからないとき、置き換える場所が1か所に決まるなら置き換えた全文と説明(決まらなければ None)。

    小さいAIは、字下げを落とした old や、少し前の形のままの行(直す前の行)を渡しがち。
    1) 行ごとの前後の空白を除くと一致する場所が1つだけ、2) old が1行で、とても近い行(85% 以上)が1つだけ、のときに限る。
    new の字下げは、置き換える場所の字下げにそろえる。
    """
    lines = text.split("\n")
    want = [line.strip() for line in old.strip("\n").split("\n")]
    if not any(want):
        return None
    n = len(want)
    hits = [i for i in range(len(lines) - n + 1) if [line.strip() for line in lines[i:i + n]] == want]
    if len(hits) > 1:
        return None
    if hits:
        start, how = hits[0], "字下げや空白の違いを除くと一致した"
    elif n == 1 and len(want[0]) >= 8:
        def score(line: str) -> float:
            """old と行の近さ。old が行の一部なら、行のうち old に当たる部分(<p> と </p> の間など)とも比べる。"""
            plain = difflib.SequenceMatcher(None, want[0], line).ratio()
            if plain < 0.5:
                return plain
            head, tail = _outside_part(want[0], line)
            core = line[len(head):len(line) - len(tail)].strip()
            return max(plain, difflib.SequenceMatcher(None, want[0], core).ratio()) if core and (head or tail) else plain

        scores = sorted(((score(line.strip()), i) for i, line in enumerate(lines) if line.strip()), reverse=True)
        if not scores or scores[0][0] < 0.85 or (len(scores) > 1 and scores[1][0] > scores[0][0] - 0.1):
            return None
        if not _only_added_or_removed(want[0], lines[scores[0][1]].strip()):
            return None  # 'other.html' と 'error.html' のような、別の語の違いは、別の行かもしれない
        start, how = scores[0][1], "いちばん近い"
    else:
        return None
    indent = re.match(r"[ \t]*", lines[start]).group(0)
    replaced = "\n".join(lines[start:start + n]).strip()
    body = _reindent(new.strip("\n").split("\n"), indent)
    part = ""
    if not hits:  # old が行の一部(<div> の中の {{ ... }} など)なら、old に当たらない行の前と後ろは残す(消すと要素ごと無くなる)
        head, tail = _outside_part(want[0], replaced)
        if head.strip() and not body[0].strip().startswith(head.strip()):
            body[0] = indent + head + body[0].lstrip()
            part = "の、old に当たる部分"
        if tail.strip() and not body[-1].strip().endswith(tail.strip()):
            body[-1] = body[-1].rstrip() + tail
            part = "の、old に当たる部分"
    updated = "\n".join(lines[:start] + body + lines[start + n:])
    where = f"{start + 1} 行目" if n == 1 else f"{start + 1}〜{start + n} 行目"
    kept = "(行の前と後ろは、そのまま残しました)" if part else ""
    return updated, f"(old はそのままでは見つからなかったため、{how} {where}{part}を置き換えました{kept}。置き換える前の行: {replaced[:160]})"


_HEADER = re.compile(r"(?:async\s+def|def|class)\s+(\w+)\b.*:\s*(?:#.*)?$")


def _def_at(tree: ast.AST, line: int) -> ast.AST | None:
    """line 行目(1 から)に def / class の行がある関数やクラス。"""
    return next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 and n.lineno == line), None)


def _exits(stmt: ast.stmt) -> bool:
    """この文まで来ると、必ず return か raise で関数を出るか(あとに続く行は動かない)。try / if の中の return も見る。"""
    def ends(body: list[ast.stmt]) -> bool:
        return bool(body) and _exits(body[-1])

    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    if isinstance(stmt, ast.If):
        return ends(stmt.body) and ends(stmt.orelse)
    if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
        return (((ends(stmt.body) or ends(stmt.orelse)) and all(ends(h.body) for h in stmt.handlers))
                or ends(stmt.finalbody))
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return ends(stmt.body)
    return False


def _whole_def_edit(text: str, old: str, new: str) -> tuple[str, str] | None:
    """old が def(または class)の行だけのとき、小さいAIのつもりどおりに、関数のかたまりの単位で直した全文と説明。

    小さいAIは「関数」を def の行で指し、そのまま置き換えると壊れる編集をしがち(qwen2.5-coder:7b で実際にあった):
    1) new = 足す関数 + old の行: old の上にデコレーター(@app.route など)があると、足した関数がそれを奪う
    2) new = 別の関数だけ: old の関数の中身が、新しい関数の続き(return のあとの動かない行)になる
    3) new = 足す関数 + 中身まで書き直した old の関数: 元の中身が、新しい中身のあとに動かない行として残る
    1) と 2) は old の関数をそのまま残して、足す関数をデコレーターより上に入れる。3) は old の関数のかたまり
    (デコレーターから最後の行まで)を new に置き換える(new に無い元のデコレーターは残す)。
    当てはまらなければ None(そのまま置き換える)。
    """
    header = old.strip()
    m = _HEADER.match(header)
    if not m or text.count(old) != 1:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.split("\n")
    line_index = text.count("\n", 0, text.index(old))
    node = _def_at(tree, line_index + 1)
    if lines[line_index].strip() != header or node is None or node.name != m.group(1):
        return None  # old が行の一部だけ、または def の行ではない
    start, end = block_lines(node)
    indent = re.match(r"[ \t]*", lines[line_index]).group(0)
    decorators = [line.strip() for line in lines[start:line_index]]
    name = node.name
    body = textwrap.dedent(new.strip("\n")).rstrip()
    if body.endswith(header):  # 1) 足す行 + old の行: デコレーターがあるときだけ、その上に足す
        before = body[:len(body) - len(header)].rstrip().split("\n")
        added_decorators = []
        while before and before[-1].strip().startswith("@"):
            added_decorators.insert(0, before.pop().strip())
        added = textwrap.dedent("\n".join(before)).strip("\n")
        if not decorators or not added.strip():
            return None
        try:
            compile(added, "<new>", "exec")
        except (SyntaxError, ValueError):
            return None
        block = (_reindent(added.split("\n"), indent) + [""]
                 + [indent + d for d in decorators + [d for d in added_decorators if d not in decorators]]
                 + lines[line_index:end])
        return ("\n".join(lines[:start] + block + lines[end:]),
                f"(足した行を、{name} のデコレーター({decorators[0]})より上に入れました。デコレーターは、すぐ下の def の"
                "関数に付くので、間に入れると、足した関数に付いてしまいます)")
    try:
        new_tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return None  # def の行だけ(名前を変えるだけ)など: そのまま置き換える
    defs = [n for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not defs:
        return None
    body_lines = body.split("\n")
    same = next((n for n in defs if n.name == name), None)
    if same is None:  # 2) 別の関数だけ: old の関数は残して、その上に足す
        block = _reindent(body_lines, indent) + [""] + lines[start:end]
        return ("\n".join(lines[:start] + block + lines[end:]),
                f"(old の {header} の関数は消さずに残し、new の関数をその上{'(デコレーターより上)' if decorators else ''}に"
                f"足しました。def の行だけを別の関数に置き換えると、{name} の中身が新しい関数の続きになってしまうためです。"
                f"{name} を書き換えるときは、old に関数の全体を入れてください)")
    if not any(_exits(s) for s in same.body):
        return None  # 中身の頭に行を足すだけかもしれない(元の中身は、そのあとに続けて動く)
    # 3) old の関数を書き直している: 関数のかたまりごと置き換える(new に無い元のデコレーターは残す)
    have = {body_lines[d.lineno - 1].strip() for d in same.decorator_list}
    first = block_lines(same)[0]
    body_lines = body_lines[:first] + [d for d in decorators if d not in have] + body_lines[first:]
    return ("\n".join(lines[:start] + _reindent(body_lines, indent) + lines[end:]),
            f"(old は def の行だけでしたが、{name} の関数の全体({start + 1}〜{end} 行目)を new に置き換えました。def の行"
            "だけを置き換えると、元の中身が新しい関数のあとに、動かない行として残ってしまうためです)")


def _many_places(text: str, old: str, count: int) -> str:
    """old が何か所もあるときの説明: 場所(行の番号)と、それぞれを1か所に決める old の例。"""
    lines = text.split("\n")
    examples, at = [], text.find(old)
    while at >= 0 and len(examples) < 3:
        first = text.count("\n", 0, at)
        last = text.count("\n", 0, at + len(old.rstrip("\n"))) + 1
        top = first
        while top > 0 and text.count("\n".join(lines[top:last])) > 1:
            top -= 1
        candidate = "\n".join(lines[top:last])
        examples.append((first + 1, candidate if text.count(candidate) == 1 and len(candidate) <= 400 else ""))
        at = text.find(old, at + len(old))
    where = "・".join(str(n) for n, _ in examples) + (" ほか" if count > len(examples) else "")
    shown = "".join(f"\n- {n} 行目なら old = {json.dumps(c, ensure_ascii=False)}" for n, c in examples if c)
    twice = [(name, nodes) for name, nodes in (top_level_defs(text) or {}).items() if len(nodes) > 1]
    dup = ("。このファイルには、同じ名前の関数が2つ以上あります: "
           + "、".join(f"{name}({'・'.join(str(n.lineno) for n in nodes)} 行目)" for name, nodes in twice[:3])
           + "。同じものの写しなら、まず片方を消してください") if twice else ""
    return (f"old の文字列が {count} か所にあります({where} 行目){dup}。どこを直すか決めて、その場所の上の行も含めた、"
            "1か所に決まる長さの old にしてください" + (f"。例:{shown}" if shown else ""))


def edit_file(ctx: ToolContext, path: str, old: str, new: str) -> str:
    target = _resolve(ctx, path)
    _refuse_office(target)
    if target.suffix.lower() == ".xlsx":  # Excel は、read_file で見える CSV の形のまま編集できる
        def change(text: str) -> str:
            count = text.count(old) if old else 0
            if count != 1:
                raise ToolError("old の文字列が表の中に見つかりません。表の最後に行を足すなら append_file で "
                                "「合計,=SUM(above)」のような CSV の行を追加してください(read_file で表を CSV として確かめられます)"
                                if count == 0 else f"old の文字列が {count} か所にあります。1か所に決まる長さにしてください")
            return text.replace(old, new)
        _edit_sheet(ctx, target, change)
        return f"編集しました: {_relative(ctx, target)}(表を CSV として編集)"
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {path}")
    newline = _newline_of(target)
    # 改行を \n にそろえてから探す(Windows の CRLF のファイルでも、複数行の old が見つかるように)
    text = _read_text(target, 2_000_000).replace("\r\n", "\n")
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if old and old == new:  # 小さいAIがしがちな、何も変えない編集(「直した」つもりのまま先へ進ませない)
        raise ToolError("old と new が同じなので、何も変わりません。new には、直したあとの内容を書いてください"
                        "(直すべき場所がこのファイルではないなら、ほかのファイルを直してください)")
    count = text.count(old)
    loose = _loose_replace(text, old, new) if old.strip() and count == 0 else None
    if loose is not None:  # そのままでは見つからないが、置き換える場所は1か所に決まる
        updated, how = loose
        if updated == text:
            raise ToolError(NO_CHANGE)
        broken = _breaks_python(target, text, updated) or _adds_duplicate(target, text, updated)
        if broken:
            raise ToolError(broken)
        _backup(ctx, target)
        target.write_bytes(_encode(updated, newline))
        return f"編集しました: {_relative(ctx, target)}\n{how}" + _code_note(target, _relative(ctx, target))
    if not old or count == 0:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        near = difflib.get_close_matches(old.strip().splitlines()[0] if old.strip() else "", lines, n=3, cutoff=0.5)
        if old.strip() and (len(new.strip()) >= 12 or "\n" in new.strip()) and new in text:  # その変更は、もう済んでいる
            raise ToolError("old の文字列はファイルにありませんが、new の内容はもうファイルに入っています。この変更は、もう済んで"
                            "います: 同じ編集を繰り返さずに、動かして確かめるか、次の手順へ進んでください")
        shown = _current_def(text, old) if target.suffix.lower() in (".py", ".pyw") else ""
        if not shown and text.strip() and text.count("\n") < 60 and len(text) <= 3000:  # 小さいファイルは、今の全体を見せる
            shown = (f"今の {_relative(ctx, target)} の全体({len(text.splitlines())} 行)は次のとおりです。old には、ここから"
                     f"そのまま写してください:\n{text.rstrip()}")
        raise ToolError("old の文字列がファイルの中に見つかりません。read_file で正確な文字列を確かめてください。"
                        + (f"似ている行: {' / '.join(near)}。" if near and not shown else "")
                        + "ファイルに無い行を足したいときは、足したい場所のすぐ前にある行をそのまま old にして、"
                          "new にその行と足す行を書いてください" + (f"\n{shown}" if shown else ""))
    if count > 1:
        raise ToolError(_many_places(text, old, count))
    if _already_added(text, old, new):  # 同じ追加の2回目(小さいAIは、直らないと同じ追加を繰り返し、同じ行が重なっていた)
        raise ToolError("足そうとした行は、もう入っています(old のすぐそばに同じ行があります)。同じ追加をもう一度すると同じ行が"
                        "重なるので、ファイルは変えていません。まだうまく動かないなら、原因は別のところにあります: 動かした結果や"
                        "サーバーの出力を読み直してください")
    new, copies = _drop_copies(target, text, old, new)  # もうある関数の、中身まで同じ写しは足さない
    whole = _whole_def_edit(text, old, new) if target.suffix.lower() in (".py", ".pyw") else None
    if whole is not None and not _breaks_python(target, text, whole[0]):  # def の行だけの old: 関数のかたまりの単位で直す
        updated, note = whole[0], "\n" + whole[1]
    else:
        updated, note = text.replace(old, new), ""
        broken = _breaks_python(target, text, updated)
        repaired = repair_string_newlines(updated) if broken else None
        if repaired is not None:  # new の中の JSON の \n が改行になって、文字列が切れていた: \n に戻す
            updated, note, broken = repaired, f"\n{STRING_NEWLINES}", ""
        if broken:  # 字下げだけがずれているなら、置き換える場所の字下げに合わせれば正しくなることが多い
            fitted = _fit_indent(text, old, new)
            if fitted is None or _breaks_python(target, text, fitted):
                raise ToolError(broken)
            updated, note = fitted, "\n(new の字下げを、置き換える場所に合わせました)"
    duplicate = _adds_duplicate(target, text, updated)
    if duplicate:  # もうある関数と同じ名前の関数を足す(小さいAIは、関数を足す編集を繰り返して重ねがち)
        raise ToolError(duplicate)
    mended = repair_page_newlines(target.name, updated) if target.suffix.lower() in _PAGE_SUFFIXES else None
    if mended is not None:  # 改行のつもりの \n(2文字)で、ページの JavaScript が動かない形だった: 本当の改行にする
        updated, note = mended, note + f"\n{PAGE_NEWLINES}"
    if updated == text:  # 置き換えても同じ中身: 書かない(時刻だけ変わると、起動し直しを求める確かめが毎回働く)
        raise ToolError(NO_CHANGE)
    _backup(ctx, target)
    target.write_bytes(_encode(updated, newline))  # 元の改行の形のまま書き戻す
    return f"編集しました: {_relative(ctx, target)}{note}{copies}" + _code_note(target, _relative(ctx, target))


def _already_added(text: str, old: str, new: str) -> bool:
    """new が old の前か後ろに行を足すだけの編集で、足す行が、もうファイルの old のすぐそばにある(字下げは見ない)。"""
    if not old.strip() or old not in new or text.count(old) != 1:
        return False
    at, i = text.index(old), new.index(old)

    def lines(part: str) -> list[str]:
        return [line.strip() for line in part.split("\n") if line.strip()]

    before, after = lines(new[:i]), lines(new[i + len(old):])
    if not before and not after:
        return False
    return ((not after or lines(text[at + len(old):])[:len(after)] == after)
            and (not before or lines(text[:at])[-len(before):] == before))


def _fit_indent(text: str, old: str, new: str) -> str | None:
    """old が行の頭(字下げのすぐ後)から始まるとき、new をその行の字下げにそろえて置き換えた全文(できなければ None)。"""
    at = text.find(old)
    start = text.rfind("\n", 0, at) + 1
    if at < 0 or text[start:at].strip():
        return None  # 行の途中から始まる old は、字下げの問題ではない
    indent = text[start:at] + old[:len(old) - len(old.lstrip(" \t"))]  # old 自身が字下げを含むこともある
    lines = _reindent(new.strip("\n").split("\n"), "")
    if len(lines) > old.count("\n") + 7:
        return None  # 大きな貼り付けは、字下げを合わせても正しいとは限らない(プログラム全体を関数の中に入れる、など)
    if indent and any(_TOP_LEVEL.match(line) for line in lines):
        return None  # import・def・class などを、ブロックの中へ入れることになる
    return text[:start] + "\n".join(_reindent(lines, indent)) + text[at + len(old):]


def append_file(ctx: ToolContext, path: str, content: str) -> str:
    target = _resolve(ctx, path)
    suffix = target.suffix.lower()
    if suffix == ".xlsx" and target.is_file():  # Excel は、最後のシートの下に CSV の行を足す
        _edit_sheet(ctx, target, lambda text: text.rstrip("\n") + "\n" + content.strip("\n"))
        return f"追記しました: {_relative(ctx, target)}(表の最後に行を追加)"
    if suffix == ".docx":
        _backup(ctx, target)
        office.append_docx(target, content)
        return f"追記しました: {_relative(ctx, target)}(Word 文書の末尾に追加)"
    if suffix == ".pptx":
        _backup(ctx, target)
        office.append_pptx(target, content)
        return f"追記しました: {_relative(ctx, target)}(PowerPoint の末尾にスライドを追加)"
    _refuse_office(target)
    if target.is_dir():
        raise ToolError(f"フォルダと同じ名前です: {path}")
    newline = _newline_of(target)
    if target.is_file() and target.suffix.lower() in (".py", ".pyw"):
        try:
            before = _read_text(target, 400_000).replace("\r\n", "\n")
        except ToolError:
            before = ""
        after = before + content.replace("\r\n", "\n")
        broken = (_breaks_python(target, before, after) or _adds_duplicate(target, before, after)) if before else ""
        if broken:
            raise ToolError(broken)
    mended = None
    if target.suffix.lower() in _PAGE_SUFFIXES:  # 改行のつもりの \n(2文字)で、JavaScript が動かない形になるなら直す
        try:
            whole = (_read_text(target, 400_000).replace("\r\n", "\n") if target.is_file() else "") + content.replace("\r\n", "\n")
        except ToolError:
            whole = None
        mended = repair_page_newlines(target.name, whole) if whole is not None else None
    _backup(ctx, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mended is not None:
        target.write_bytes(_encode(mended, newline))
    else:
        with target.open("ab") as f:  # ファイルの改行の形に合わせて足す
            f.write(_encode(content, newline))
    return (f"追記しました: {_relative(ctx, target)} ({len(content)} 文字)" + (f"\n{PAGE_NEWLINES}" if mended is not None else "")
            + _code_note(target, _relative(ctx, target)))


def write_code(ctx: ToolContext, path: str, instructions: str) -> str:
    """ファイルの中身を、書く役のAIに書かせて保存する(JSON の中に長いコードを書かせると崩れやすいため)。"""
    if ctx.write_code is None:
        raise ToolError("この道具は今は使えません。write_file を使ってください")
    target = _resolve(ctx, path)
    if target.suffix.lower() in office.OFFICE_SUFFIXES:
        raise ToolError("Word・Excel・PowerPoint は make_word / make_excel / make_slides で作ってください")
    current = None
    if target.is_file():
        try:
            current = _read_text(target, 60000)
        except ToolError:
            current = None
    content = ctx.write_code(_relative(ctx, target), instructions, current)
    if not content.strip():
        raise ToolError("中身を書けませんでした。指示を具体的にして、もう一度試してください")
    result = write_file(ctx, path, content)
    lines = content.splitlines()
    head = "\n".join(lines[:60])[:2500]
    more = f"\n... (全 {len(lines)} 行)" if len(lines) > 60 or len(head) >= 2500 else ""
    return f"{result}\n--- 書いた内容(先頭) ---\n{head}{more}"


def make_folder(ctx: ToolContext, path: str) -> str:
    target = _resolve(ctx, path)
    target.mkdir(parents=True, exist_ok=True)
    return f"フォルダを作成しました: {_relative(ctx, target)}/"


_PATTERN = re.compile(r"[*?\[]")


def _transfer(ctx: ToolContext, path: str, to: str, move: bool) -> str:
    if _PATTERN.search(path or ""):
        return _transfer_many(ctx, path, to, move)
    source, target = _resolve(ctx, path), _resolve(ctx, to)
    if source.is_dir() and source != ctx.workspace.resolve():  # フォルダを渡した(中の写真を分けるつもりで photos/ を渡していた)
        names = sorted(p.name for p in source.iterdir() if p.is_file())
        folder, verb = _relative(ctx, source), "移" if move else "写"
        shown = ", ".join(names[:12]) + (f" ほか {len(names) - 12} 件" if len(names) > 12 else "")
        raise ToolError(f"{folder}/ はフォルダです。中のファイルをまとめて{verb}すときは、path に名前のパターンを書きます(例: "
                        f"\"{folder}/*2023*\" は、名前に 2023 が入るファイルをすべて、to のフォルダへ{verb}します)。"
                        + (f"中のファイル: {shown}" if names else "中にファイルはありません"))
    if not source.is_file():
        listed = _listed_files(ctx, path)
        if listed:  # 「a.jpg b.jpg」のように、いくつかのファイルを1つの path に並べた: まとめて to のフォルダへ
            return _transfer_files(ctx, listed, to, move)
        raise ToolError(f"ファイルがありません: {path}")
    if target.is_dir() or to.rstrip().endswith(("/", "\\")) or (source.suffix and not target.suffix and not target.exists()):
        target = target / source.name  # まだ無いフォルダ(photos/2024 など)へ移すときも、フォルダの中へ(名前のファイルにしない)
    if target.exists():
        raise ToolError(f"移動先に同じ名前のファイルがあります: {to}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        _backup(ctx, source)
        shutil.move(str(source), str(target))
    else:
        shutil.copy2(source, target)
    return f"{'移動' if move else 'コピー'}しました: {_relative(ctx, source)} → {_relative(ctx, target)}" + ("" if move else COPY_KEEPS)


def _transfer_many(ctx: ToolContext, pattern: str, to: str, move: bool) -> str:
    """パターン(photos/*2023* など)に合うファイルを、まとめてフォルダ to へ移す・写す(1つずつ呼ぶより速く、取り違えも減る)。"""
    raw = pattern.strip().replace("\\", "/")
    if Path(raw).expanduser().is_absolute() or raw.startswith("~"):
        if not ctx.anywhere:
            raise ToolError("作業フォルダの外にはアクセスできません(権限を「すべて許可」にすると扱えます)")
        full = Path(os.path.expandvars(raw)).expanduser()
        base, rest = Path(full.anchor), full.relative_to(full.anchor).as_posix()
    else:
        if ctx.workspace is None:
            raise ToolError("作業フォルダが設定されていません")
        base, rest = ctx.workspace.resolve(), raw[2:] if raw.startswith("./") else raw
    try:
        found = sorted(p for p in base.glob(rest) if p.is_file() and ".git" not in p.parts)
    except (ValueError, OSError) as e:
        raise ToolError(f"パターンが正しくありません: {pattern}({e})") from e
    sources = [_resolve(ctx, str(p)) for p in found]  # 作業フォルダの外に出るものは、ここで止まる
    if not sources:
        raise ToolError(f"パターンに合うファイルがありません: {pattern}(list_files で名前を確かめてください)")
    return _transfer_files(ctx, sources, to, move)


def _listed_files(ctx: ToolContext, path: str) -> list[Path]:
    """path に並べた、いくつかのファイル(空白・カンマ・読点で区切ったもの)。どれかが無ければ空(1つの名前として扱う)。"""
    names = [n for n in re.split(r"[\s,;、]+", path.strip()) if n]
    if len(names) < 2:
        return []
    try:
        files = [_resolve(ctx, n) for n in names]
    except ToolError:
        return []
    return files if all(f.is_file() for f in files) else []


def _transfer_files(ctx: ToolContext, sources: list[Path], to: str, move: bool) -> str:
    """いくつかのファイルを、まとめてフォルダ to へ移す・写す(フォルダが無ければ作る)。"""
    verb = "移動" if move else "コピー"
    folder = _resolve(ctx, to)
    if folder.is_file():
        raise ToolError(f"まとめて{verb}するときは、to をフォルダにしてください(同じ名前のファイルがあります: {to})")
    folder.mkdir(parents=True, exist_ok=True)
    done, skipped = [], []
    for source in sources:
        target = folder / source.name
        if target == source:
            continue
        if target.exists():
            skipped.append(source.name)
            continue
        if ctx.changes is not None:
            for rel in (_relative(ctx, source), _relative(ctx, target)):
                if rel not in ctx.changes:
                    ctx.changes[rel] = _snapshot(ctx, rel)
        if move:
            _backup(ctx, source)
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(source, target)
        done.append(source.name)
    text = f"{len(done)} 件を{verb}しました → {_relative(ctx, folder)}/"
    if done:
        text += ": " + ", ".join(done[:30]) + (f" ほか {len(done) - 30} 件" if len(done) > 30 else "")
    if skipped:
        text += f"\n{verb}先に同じ名前があるため{verb}しなかったもの: " + ", ".join(skipped[:20])
    return text + ("" if move or not done else COPY_KEEPS)


def delete_file(ctx: ToolContext, path: str) -> str:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"ファイルがありません(フォルダは削除できません): {path}")
    _backup(ctx, target)
    target.unlink()
    return f"削除しました(バックアップ済み。restore_file で戻せます): {_relative(ctx, target)}"


def restore_file(ctx: ToolContext, path: str) -> str:
    target = _resolve(ctx, path)
    relative = _relative(ctx, target)
    if ctx.backup_dir is None or not ctx.backup_dir.is_dir():
        raise ToolError("バックアップがありません")
    copies = sorted((d / relative for d in ctx.backup_dir.iterdir() if (d / relative).is_file()), key=lambda p: p.parent.name)
    if not copies:
        raise ToolError(f"このファイルのバックアップはありません: {path}")
    latest = copies[-1]
    saved = latest.read_bytes()
    _backup(ctx, target)  # 戻す前の状態も残しておく
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(saved)
    return f"元に戻しました: {relative}({latest.parent.name[:15]} 時点の内容)"


# ---- 実行 -------------------------------------------------------------------

def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932" if sys.platform == "win32" else "utf-8", "replace")


def _finish(proc: subprocess.CompletedProcess, hint: str = "") -> str:
    out, err = _plain(_decode(proc.stdout or b"")).strip(), _plain(_decode(proc.stderr or b"")).strip()
    text = out + (("\n[エラー出力]\n" + err) if err else "")
    return (text.strip() or "(出力なし)") + (f"\n{hint}" if hint else "") + (f"\n[終了コード {proc.returncode}]" if proc.returncode else "")


def python_command() -> list[str]:
    """スクリプトを動かす Python。コマンドの python と同じもの(pip で入れたものが使える)。無ければ Celestial 自身の Python。"""
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found and "windowsapps" not in found.lower():  # Microsoft Store を開くだけの代わりのものは使わない
            return [found]
    if getattr(sys, "frozen", False):
        raise ToolError("Python が見つかりません。Python を入れてから、もう一度試してください")
    return [sys.executable]


def _env() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", **_NO_COLOR}


# 色の制御文字を出させない。Python 3.13 からは、FORCE_COLOR があるだけで(値が 0 でも)色を付けるので、
# NO_COLOR と PYTHON_COLORS=0 も渡す(Node などは FORCE_COLOR=0 で止まる)
_NO_COLOR = {"NO_COLOR": "1", "PYTHON_COLORS": "0", "FORCE_COLOR": "0"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _plain(text: str) -> str:
    """出力から、色やカーソル移動の制御文字を除く(それでも出してくるプログラムがある)。"""
    return _ANSI.sub("", text) if "\x1b" in text else text


def _no_window() -> int:
    """Windows: 黒い窓を出さず、子のプログラムをまとめて止められるようにする。"""
    return (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0


class _TimedOut(Exception):
    def __init__(self, output: str):
        super().__init__(output)
        self.output = output


def _run_captured(command, *, cwd: str, timeout: float, env: dict | None = None, shell: bool = False) -> subprocess.CompletedProcess:
    """終わるまで待って出力を集める。時間切れなら子のプログラムごと止めて、それまでの出力を _TimedOut で知らせる。

    subprocess.run の時間切れはシェルしか止めないため、シェルの先で動くサーバーなどが出力をつかんだまま残ると、いつまでも戻らない。
    """
    popen = subprocess.Popen(command, shell=shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, creationflags=_no_window(), start_new_session=sys.platform != "win32")
    try:
        out, err = popen.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_popen(popen)
        try:
            out, err = popen.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out, err = b"", b""
        raise _TimedOut((_decode(out or b"") + "\n" + _decode(err or b"")).strip()) from None
    return subprocess.CompletedProcess(command, popen.returncode, out, err)


def _timed_out(timeout: float, e: _TimedOut, advice: str = "") -> ToolError:
    tail = e.output[-1500:]
    return ToolError(f"{timeout:.0f}秒以内に終わらなかったため中断しました{advice}" + (f"。それまでの出力:\n{tail}" if tail else ""))


def run_python(ctx: ToolContext, code: str, timeout: float = 30) -> str:
    with tempfile.TemporaryDirectory(prefix="celestial-run-") as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")
        cwd = str(ctx.workspace.resolve()) if ctx.workspace else tmp
        try:
            proc = _run_captured(python_command() + ["-I", "-X", "utf8", str(script)], cwd=cwd, timeout=timeout, env=_env())
        except _TimedOut as e:
            raise _timed_out(timeout, e) from None
    return _finish(proc)


# 動き続けるプログラム(サーバー・ウィンドウのアプリ)。run_file / run_command でも、終わるのを待たずに裏で起動する
# (小さなモデルはサーバーを run_file で動かしがちで、そのままだと時間切れまで待って失敗する)
_SERVER_CODE = re.compile(
    r"serve_forever\s*\(|\b(?:Threading)?(?:HTTP|TCP|UDP)Server\s*\(|\bapp\.run\s*\(|\.run\s*\([^)]*\b(?:host|port)\s*=|"
    r"uvicorn\.run\s*\(|web\.run_app\s*\(|run_simple\s*\(|waitress\.serve\s*\(|socketio\.run\s*\(|\.listen\s*\(|"
    r"createServer\s*\(|Deno\.serve\s*\(|Bun\.serve\s*\(")
_WINDOW_CODE = re.compile(r"\.mainloop\s*\(|\.exec_?\s*\(\s*\)|pygame\.display\.set_mode|webview\.start\s*\(|\bft\.app\s*\(|"
                          r"\bnew\s+BrowserWindow\s*\(")
_SERVER_COMMAND = re.compile(
    r"(?:^|[\s&|;(])(?:(?:python[0-9.]*|py)(?:\.exe)?\s+(?:-\S+\s+)*-m\s+(?:http\.server|flask\s+run|uvicorn|gunicorn|streamlit\s+run)\b|"
    r"flask\s+run\b|uvicorn\s|gunicorn\s|streamlit\s+run\b|manage\.py\s+runserver\b|php\s+-S\s|hugo\s+serve|jekyll\s+serve|"
    r"http-server\b|live-server\b|ng\s+serve\b|rails\s+s(?:erver)?\b|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:start|dev|serve|preview)\b|"
    r"npx\s+(?:serve|vite|http-server|live-server|next\s+(?:dev|start))\b|vite(?:\s+(?:dev|serve|preview))?\s*$|next\s+(?:dev|start)\b|"
    r"electron\s+\.)", re.I)
_RUN_SCRIPT = re.compile(r"""(?:^|[\s&|;(])"?(?:[^\s"&|]*[\\/])?(?:python[0-9.]*|pythonw|py|node|bun|tsx|ts-node)(?:\.exe)?"?"""
                         r"""(?:\s+-[XW]\s*\S+|\s+-(?!m\b|c\b)[^\s"]+)*\s+"?([^\s"&|<>]+\.(?:pyw?|m?js|cjs|ts))\b""", re.I)
_PORT_HINT = re.compile(
    r"\bport\b\s*[=:]?\s*\(?\s*(\d{4,5})\b|listen\s*\(\s*(\d{4,5})\b|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\]):(\d{4,5})\b|"
    r"http\.server\s+(?:-\S+\s+)*(\d{4,5})\b|--port[= ](\d{4,5})\b|\s-p\s+(\d{4,5})\b|runserver\s+(?:[\w.]+:)?(\d{4,5})\b|"
    r"\.(?:pyw?|m?js|cjs|ts)\"?\s+(\d{4,5})\b", re.I)
_PORT_WEAK = re.compile(r",\s*(\d{4,5})\s*\)|\(\s*(\d{4,5})\s*\)")  # ("", 8931) や run_server(8931) のような番号
_DEFAULT_PORTS = ((re.compile(r"http\.server|uvicorn|fastapi|runserver", re.I), 8000), (re.compile(r"flask|\bapp\.run\s*\(", re.I), 5000),
                  (re.compile(r"\bvite\b|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?dev\b", re.I), 5173),
                  (re.compile(r"\bnext\b|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:start|dev)\b|react-scripts", re.I), 3000),
                  (re.compile(r"streamlit", re.I), 8501))


def _long_running(code: str) -> str:
    """動き続けるプログラムの種類("server" か "window")。そうでなければ空。"""
    if not code:
        return ""
    if _SERVER_CODE.search(code) and not (re.search(r"\.shutdown\s*\(", code) and "Thread" in code):  # 自分で止める試験は除く
        return "server"
    return "window" if _WINDOW_CODE.search(code) else ""


_LISTEN_HINT = re.compile(
    r"\bport\b\s*[=:]?\s*\(?\s*(\d{4,5})\b|listen\s*\(\s*(\d{4,5})\b|http\.server\s+(?:-\S+\s+)*(\d{4,5})\b|"
    r"--port[= ](\d{4,5})\b|\s-p\s+(\d{4,5})\b|runserver\s+(?:[\w.]+:)?(\d{4,5})\b|"
    r"(?:Server|\.bind)\(\s*\(\s*['\"][^'\"]*['\"]\s*,\s*(\d{4,5})\s*\)", re.I)


def _listen_ports(text: str) -> list[int]:
    """起動したものが自分で待ち受けるとはっきり分かるポート(port=8080・--port 8080・HTTPServer(('', 8080)) など)。

    つなぎに行く先(requests.get('http://localhost:8080/...') など)は含めない。ほかのプログラムが使っているかを、起動の前に
    確かめるために使う(確かめる相手を取り違えて、つなぎに行くだけのプログラムを止めないように)。
    """
    found: list[int] = []
    for m in _LISTEN_HINT.finditer(text):
        n = int(next(g for g in m.groups() if g))
        if 1024 <= n <= 65535 and n not in found:
            found.append(n)
    return found[:3]


def _bindable(port: int) -> bool:
    """この PC で、そのポートで待ち受けられるか(ほかのプログラムが使っておらず、Windows が予約もしていない)。"""
    with socket.socket() as s:
        try:
            if sys.platform == "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def free_port(near: int) -> int:
    """near の次から順に、この PC で空いているポートの番号(見つからなければ、OS に選ばせた番号)。"""
    for port in range(near + 1, min(near + 40, 65536)):
        if _bindable(port):
            return port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ports(text: str) -> list[int]:
    """起動したものが受け付けるはずのポート(コマンドやコードに書かれた番号。無ければ、よく使われる既定の番号)。"""
    found: list[int] = []
    for pattern in (_PORT_HINT, _PORT_WEAK):
        for m in pattern.finditer(text):
            n = int(next(g for g in m.groups() if g))
            weak = pattern is _PORT_WEAK and n & (n - 1) == 0  # 4096 などの2の累乗は、読み書きの大きさであることが多い
            if 1024 <= n <= 65535 and n not in found and not weak:
                found.append(n)
    if not found:
        found = [port for pattern, port in _DEFAULT_PORTS if pattern.search(text)]
    return list(dict.fromkeys(found))[:3]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.2):
            return True
    except OSError:
        return False


SLOW_MARK = "秒待ってもページの答えが返りませんでした"
SLOW_PAGE = ("つながりましたが、{seconds} 秒待ってもページの答えが返りませんでした。ページを作る処理が止まっているか、とても遅い"
             "ようです(外部の API の呼び出しを待ち続けていることが多い: requests.get などには timeout=5 を付け、失敗しても"
             "「取得できませんでした」と表示してページは返すようにしてください。時間のかかる取得は、ページとは別の /api/... に分けます)")


def _timed_out_error(e: BaseException) -> bool:
    reason = getattr(e, "reason", e)
    return isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower()


def _http_peek(port: int, wait: float = 3) -> str:
    """受け付けを始めたポートを、実際に開いてみた結果(Web サーバーなら、その場で動いているかが分かる)。wait は答えを待つ秒数。"""
    request = urllib.request.Request(f"http://localhost:{port}/", headers={"User-Agent": "Celestial-agent"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=wait) as res:
            body, status, kind = res.read(4000), res.status, res.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body, status, kind = e.read(4000), e.code, ""
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        if _timed_out_error(e):  # つながったが答えがない(ページを作る処理で止まっている)。HTTP でないのとは違う
            return f"ただし http://localhost:{port}/ を開くと、" + SLOW_PAGE.format(seconds=round(wait))
        return (f"ただし http://localhost:{port}/ を開いても HTTP の応答がありませんでした({type(e).__name__})。Web サーバーなら、"
                "http.server(BaseHTTPRequestHandler と serve_forever)などで HTTP の形で応答するようにしてください。")
    page = _decode(body)
    only = _error_only(page, kind) if status < 400 else ""
    if only:
        return (f"http://localhost:{port}/ を開くと HTTP {status} ですが、{ERROR_PAGE}: {only}"
                "(コードの中で例外を受けて、文だけを返しているようです。本当の原因は、サーバーの出力に出ていることが多い)")
    text = _error_title(page) if status >= 500 else ""
    text = text or re.sub(r"\s+", " ", page).strip()[:200]
    return f"http://localhost:{port}/ を開くと HTTP {status}: {text or '(本文なし)'}"


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _error_title(page: str) -> str:
    """エラーのページの題名(Flask の debug のページなら、例外の文そのもの)。"""
    m = _TITLE.search(page)
    if not m:
        return ""
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return re.sub(r"\s*//\s*Werkzeug Debugger$", "", title)[:300]


ERROR_PAGE = "ページの代わりに、エラーの文だけが返っています"
NO_PAGE = "ページ(/)がありません"


def _page_files(cwd: str, command: str) -> list[str]:
    """サーバーのそばにある、見せるはずのページ(templates/*.html・index.html)。サーバーのフォルダからの道のり。"""
    base = Path(cwd)
    folders = [base] + [(base / m.group(1)).parent for m in _RUN_SCRIPT.finditer(command)]
    found: list[str] = []
    for folder in dict.fromkeys(folders):
        for pattern in ("templates/*.html", "index.html", "static/index.html"):
            try:
                found += [p.relative_to(base).as_posix() if p.is_relative_to(base) else p.name for p in sorted(folder.glob(pattern))]
            except OSError:
                continue
    return list(dict.fromkeys(found))[:3]


def _no_page_note(port: int, pages: list[str]) -> str:
    return (f"\n注意: {NO_PAGE}。{'・'.join(pages)} を書いたのに、http://localhost:{port}/ を開くと HTTP 404 です。/ を開いたときに"
            "そのページを返す関数を足してください(Flask なら @app.route('/') の関数で render_template('index.html') を返します。"
            "ページは、サーバーのファイルと同じフォルダの templates の中に置きます)。")
_ERROR_WORDS = re.compile(r"error|exception|traceback|fail|unable|could ?not|cannot|can't|not found|invalid|"
                          r"エラー|失敗|できません|見つかりません|例外", re.I)
_OPEN_TAG = re.compile(r"<[a-zA-Z][^>]*>")


def _error_only(page: str, kind: str = "") -> str:
    """HTTP 200 なのに、ページの代わりに短いエラーの文だけが返っているなら、その文(違えば空)。

    Flask で try: ... except Exception: return 'Error ...' と書くと、テンプレートが見つからないなどのコードの誤りも
    「200 の短い文」になり、開けたように見える(実際にあった)。テンプレートから作ったページ(タグの多いもの)や、
    JSON(API がエラーを返すのは正しい作り)は見ない。
    """
    body = page.strip()
    if not body or "json" in kind.lower() or body[0] in "{[" or len(body) > 600 or len(_OPEN_TAG.findall(body)) > 2:
        return ""
    visible = re.sub(r"\s+", " ", html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", " ", body))).strip()
    return visible[:300] if visible and len(visible) <= 300 and _ERROR_WORDS.search(visible) else ""


def _scripts_run(ctx: ToolContext, command: str) -> str:
    """コマンドが python や node で動かすスクリプトの中身(作業フォルダの中のもの)。"""
    texts = [file_text(ctx, m.group(1)) for m in _RUN_SCRIPT.finditer(command)]
    return "\n".join(t for t in texts if t)


_SHELL_TOKEN = re.compile(r'"[^"]*"?|\'[^\']*\'|&&|\|\||[&|;]|[^"\'&|;]+|.', re.S)
_UNIX_WORDS = ("ls", "cat", "mv", "cp", "rm", "touch", "grep", "pwd", "which", "head", "tail", "chmod", "sed", "awk", "export")


def _fix_segment(segment: str) -> str:
    body = segment.strip()
    if not body:
        return segment
    lead, trail = segment[: len(segment) - len(segment.lstrip())], segment[len(segment.rstrip()):]
    m = re.fullmatch(r"mkdir\s+(?:(?:-p|--parents)\s+)?(.+)", body, re.I)
    if m and not re.search(r"[<>]", m.group(1)):
        paths = [p.strip('"').replace("/", "\\") for p in re.findall(r'"[^"]+"|\S+', m.group(1)) if p not in ("-p", "--parents")]
        if paths:
            return lead + " & ".join(f'(if not exist "{p}" mkdir "{p}")' for p in paths) + trail
    m = re.fullmatch(r'echo\s+(?:"([^"<>&|^%()]*)"|([^"<>&|^%()]+?))\s*(>>?)\s*(\S.*)', body, re.I)
    if m and (m.group(1) or m.group(2) or "").strip():
        return lead + f"(echo {(m.group(1) or m.group(2)).strip()}){m.group(3)} {m.group(4)}" + trail
    m = re.match(r"(python3?|py)(?=\s|$)", body, re.I)
    if m:
        found = shutil.which(m.group(1))
        if found is None or "windowsapps" in found.lower():
            try:
                return lead + f'"{python_command()[0]}"' + body[m.end():] + trail
            except ToolError:
                pass
    return segment


def _for_shell(command: str) -> str:
    """Windows のコマンドプロンプト(cmd.exe)向けに、Unix の癖で書かれたコマンドを直す。他の OS では何もしない。

    - 'Initial commit' のような一重引用符の引数 → "Initial commit"(cmd では効かず、git commit などが失敗する)
    - a; b → a & b(cmd は ; で区切らない)
    - mkdir -p a/b → (if not exist "a\\b" mkdir "a\\b")(-p という名前のフォルダができる・/ が使えない、を防ぐ)
    - echo "text" > file → (echo text)> file(引用符や末尾の空白がファイルに入らないように)
    - python3 などが無い(ストアを開くだけの代わりしか無い)PC では、使える Python に置き換える
    PowerShell を呼ぶコマンドは、引用符の意味が違うので触らない。
    """
    if sys.platform != "win32" or not command.strip() or re.match(r'\s*"?(?:powershell|pwsh)(?:\.exe)?\b', command, re.I):
        return command
    parts: list[str] = []
    segment: list[str] = []
    for m in _SHELL_TOKEN.finditer(command):
        token = m.group(0)
        split = token in ("&&", "||", "&", "|") or (token == ";" and command[m.end():m.end() + 1] in ("", " ", "\t"))
        if split:  # 「set PATH=a;b」のように、空白の続かない ; は区切りではない
            parts += [_fix_segment("".join(segment)), "&" if token == ";" else token]
            segment = []
            continue
        if len(token) >= 2 and token[0] == token[-1] == "'":
            before = command[m.start() - 1] if m.start() else " "
            after = command[m.end()] if m.end() < len(command) else " "
            inner = token[1:-1]
            # 中の " は \" にする(python -c 'print("hi")' など)。cmd の記号も混ざるものは、引用の区切りがずれるので触らない
            if before in " \t=(" and after in " \t&|;)<>" and not ('"' in inner and re.search(r"[&|<>^]", inner)):
                token = '"' + inner.replace('"', '\\"') + '"'
        segment.append(token)
    parts.append(_fix_segment("".join(segment)))
    return "".join(parts)


def _command_hint(command: str, proc: subprocess.CompletedProcess) -> str:
    """よくある失敗に、直し方を添える。"""
    if not proc.returncode:
        return ""
    text = _decode(proc.stdout or b"") + _decode(proc.stderr or b"")
    unix = re.search(r"(?:^|&&|\|\||[&|;(])\s*(?:" + "|".join(_UNIX_WORDS) + r")\b", command)
    if sys.platform == "win32" and unix and re.search(r"is not recognized|認識されていません", text):
        return ("[ヒント] これは Windows のコマンドプロンプトです(ls→dir、cat→type、mv→move、cp→copy、rm→del)。"
                "ファイルやフォルダの操作は list_files・read_file・make_folder・move_file・copy_file・delete_file の道具の方が確実です。")
    if re.search(r"not a git repository", text):
        return ("[ヒント] ここは git のリポジトリではありません。このフォルダをリポジトリにするなら、先に git init を実行してください"
                "(別のフォルダのリポジトリなら、cd フォルダ && git ... のように、そのフォルダで実行します)。")
    if re.search(r"nothing added to commit but untracked files present", text):
        return "[ヒント] まだ git add していないファイルがあります。git add でファイルを加えてから commit してください。"
    if re.search(r"is not recognized|認識されていません", text) and re.match(
            r"\s*(excel|winword|word|powerpnt|powerpoint|acrord32|chrome|msedge|firefox)(\.exe)?\b", command, re.I):
        return ('[ヒント] ファイルをそのアプリで開くには、open の道具を使うか start "" "ファイル名" を実行してください。'
                "中身を確かめるだけなら read_file で読めます(Excel の表も、数式の結果つきで読めます)。")
    absent = re.search(r"'([^'\s]+)' (?:は、内部コマンド|is not recognized)|(?:^|\n)(?:\S+: )?(?:line \d+: )?(\S+): (?:command )?not found",
                       text)
    if absent:  # この PC に無いコマンド: 入れようとせず(choco install jq を試していた。実際にあった)、ある道具で行わせる
        name = absent.group(1) or absent.group(2)
        return (f"[ヒント] {name} は、この PC にありません。ソフトはインストールしないでください(頼まれていないため)。"
                "ある道具で行います: JSON を読む・URL の中身を取るなどは Python でできます(例: python -c \"import json, "
                "urllib.request; print(json.load(urllib.request.urlopen('https://...', timeout=10)))\")。作るプログラムが"
                "使うデータ(天気や緯度・経度など)なら、別の手順で調べず、そのプログラムの中で取ってください。")
    if re.search(r"Please tell me who you are|unable to auto-detect email address", text):
        return ('[ヒント] git の名前とメールが設定されていません。このリポジトリだけに設定するには '
                'git config user.name "Celestial" と git config user.email "celestial@localhost" を実行してから、もう一度 commit してください。')
    return ""


_RUNNERS = {".py": None, ".pyw": None, ".js": ["node"], ".mjs": ["node"], ".cjs": ["node"],
            ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
            ".bat": ["cmd", "/c"], ".cmd": ["cmd", "/c"], ".sh": ["bash"], ".rb": ["ruby"], ".php": ["php"], ".pl": ["perl"],
            ".go": ["go", "run"]}


_DOCUMENTS = {".xlsx", ".xls", ".csv", ".tsv", ".docx", ".doc", ".pptx", ".pdf", ".txt", ".md", ".json", ".html", ".htm", ".xml",
              ".yaml", ".yml", ".toml", ".ini", ".log", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def _split_args(text: str) -> list[str]:
    import shlex
    try:
        return shlex.split(text, posix=os.name != "nt") if text.strip() else []
    except ValueError:
        return text.split()


def run_file(ctx: ToolContext, path: str, args: str = "", timeout: float = 120) -> str:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {path}(先に write_file / write_code で作ってください)")
    suffix = target.suffix.lower()
    if suffix not in _RUNNERS:
        if suffix in _DOCUMENTS:
            raise ToolError(f"{target.name} はプログラムではないので実行できません。中身を確かめるには read_file を使ってください"
                            "(利用者がアプリで開いて見たいときだけ open)")
        raise ToolError(f"この種類のファイル({suffix or '拡張子なし'})は run_file では実行できません。run_command を使ってください")
    runner = _RUNNERS[suffix]
    if runner is None:
        argv = python_command() + ["-X", "utf8", str(target)]
    else:
        if shutil.which(runner[0]) is None:
            raise ToolError(f"{runner[0]} が見つからないため実行できません")
        argv = runner + [str(target)]
    argv += [a.strip('"') for a in _split_args(args)]
    code = file_text(ctx, path) if suffix in (".py", ".pyw", ".js", ".mjs", ".cjs") else ""
    kind = _long_running(code)
    if kind:  # サーバー・ウィンドウのアプリは終わらないので、裏で起動する
        command = subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)
        started = _launch(ctx, command, name=target.stem, keep=kind == "window", cwd=str(target.parent), wait=8.0,
                          ports=_ports(code + "\n" + target.name + " " + args), expect_running=True,
                          listen=_listen_ports(code + "\n" + target.name + " " + args))
        what = "サーバー" if kind == "server" else "ウィンドウのアプリ"
        return f"{target.name} は動き続けるプログラム({what})なので、終わるのを待たずに裏で起動しました。\n{started}"
    try:
        proc = _run_captured(argv, cwd=str(target.parent), timeout=timeout, env=_env())
    except _TimedOut as e:
        raise _timed_out(timeout, e, "(サーバーなど動き続けるものは start_process で起動してください)") from None
    return _finish(proc)


def file_text(ctx: ToolContext, path: str, limit: int = 200_000) -> str:
    """安全の確かめと、確認の画面のために、実行するファイルの中身を読む(読めなければ空)。"""
    try:
        target = _resolve(ctx, path)
        data = target.read_bytes()[:limit]
    except (ToolError, OSError):
        return ""
    return _decode(data)


# PC にソフトを入れるコマンド(エージェントは、頼まれたときだけ使う)
_SYSTEM_INSTALL = re.compile(r"(?:^|&&|\|\||[&;|])\s*(?:winget|choco|scoop|apt(?:-get)?|brew|snap|pacman|dnf|yum)(?:\.exe)?\s+"
                             r"(?:install|upgrade|add)\b", re.I)
_ASKS_INSTALL = re.compile(r"インストール|入れて|導入|セットアップ|\binstall|\bset ?up\b", re.I)


def _refuse_command(ctx: ToolContext, command: str) -> None:
    """動かす前に断るコマンド(利用者に許可を聞くまでもないもの)。

    記号だけのもの(「@」を繰り返していた)と、頼まれていないのに PC にソフトを入れるもの(jq が無いので
    choco install jq を試していた。どちらも実際にあった)。
    """
    if not re.search(r"[A-Za-z0-9぀-ヿ一-鿿]", command):
        raise ToolError(f"実行するコマンドがありません(「{command.strip()[:20]}」は記号だけです)。実行したいコマンドを書いてください")
    if ctx.by_agent and _SYSTEM_INSTALL.search(command) and not _ASKS_INSTALL.search(ctx.task or ""):
        raise ToolError("PC にソフトをインストールするコマンド(winget・choco など)は、頼まれていないので実行しません。この PC に"
                        "ある道具で行ってください(JSON を読む・URL の中身を取るなどは Python でできます)")


def run_command(ctx: ToolContext, command: str, timeout: float = 90) -> str:
    cwd = str(ctx.workspace.resolve()) if ctx.workspace else str(Path.home())
    command = _for_shell(command)
    script = _scripts_run(ctx, command)
    kind = ("window" if re.search(r"electron\s+\.", command, re.I) else "server") if _SERVER_COMMAND.search(command) else _long_running(script)
    if kind:  # サーバーなどは終わらないので、裏で起動する
        started = _launch(ctx, command, keep=kind == "window", cwd=cwd, wait=8.0, ports=_ports(command + "\n" + script),
                          expect_running=True, listen=_listen_ports(command + "\n" + script))
        what = "サーバー" if kind == "server" else "ウィンドウのアプリ"
        return f"動き続けるプログラム({what})を起動するコマンドなので、終わるのを待たずに裏で起動しました。\n{started}"
    try:
        proc = _run_captured(command, cwd=cwd, timeout=timeout, env=_env(), shell=True)
    except _TimedOut as e:
        raise _timed_out(timeout, e, "(サーバーなど動き続けるものは start_process で起動してください)") from None
    return _finish(proc, _command_hint(command, proc))


# ---- 常駐プログラム(サーバーなど) ---------------------------------------------

@dataclass
class _Process:
    name: str
    command: str
    popen: subprocess.Popen
    keep: bool = False
    output: list[str] = field(default_factory=list)
    cwd: str = ""
    ports: list[int] = field(default_factory=list)  # 受け付けるはずのポート(起動の前は空いていたもの)
    started: float = field(default_factory=time.time)
    stopped: bool = False  # stop_process(または画面)で止めた: 終了コードは止めたことによるもので、誤りではない
    asked: set[str] = field(default_factory=set)  # Celestial が開いた URL の道(ページ・ページが読むもの・check_url)

    def text(self, limit: int = 3000) -> str:
        return "".join(self.output)[-limit:]


_PROCS: dict[str, _Process] = {}
_PROCS_LOCK = threading.Lock()
_MAX_BUFFER = 64_000


def _pump(proc: _Process) -> None:
    stream = proc.popen.stdout
    if stream is None:
        return
    for raw in iter(stream.readline, b""):
        proc.output.append(_plain(_decode(raw)))
        while sum(len(x) for x in proc.output) > _MAX_BUFFER and len(proc.output) > 1:
            proc.output.pop(0)
    stream.close()


_ACCESS = re.compile(r'"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (\S+) HTTP/[\d.]+" (\d{3})')


def _quiet_access_log(text: str, asked: set[str] | None = None) -> str:
    """サーバーの出力の、同じ要求の記録(アクセスログ)を1行にまとめる。asked(Celestial が開いた道)を渡すと、そのどれでも
    ない道への要求に、よそから来ていると添える(開いたままのブラウザーのタブが、前のページの /api/time を毎秒開いていて、
    AIがその 404 を見て、要らない直しを続けた。実際にあった)。"""
    kept: list[str] = []
    first: dict[tuple[str, str, str], int] = {}
    count: dict[tuple[str, str, str], int] = {}
    for line in text.split("\n"):
        m = _ACCESS.search(line)
        if not m:
            kept.append(line)
            continue
        key = (m.group(1), m.group(2).split("?")[0], m.group(3))
        if key in first:
            count[key] += 1
            continue
        first[key], count[key] = len(kept), 1
        kept.append(line)
    for key, at in first.items():
        notes = [f"同じ要求がほかに {count[key] - 1} 回"] if count[key] > 1 else []
        if key[1].startswith("/favicon"):
            notes.append("ブラウザーが自動で求めるアイコンです。無くても問題ありません")
        elif asked and key[1] not in asked:
            notes.append("Celestial もこのページも開いていない URL です。よそ(開いたままのブラウザーのタブなど)からの要求なので、"
                         "今の作業とは関係ありません")
        if notes:
            kept[at] += f"  ({'。'.join(notes)})"
    return "\n".join(kept)


def _state(proc: _Process) -> str:
    code = proc.popen.poll()
    if code is None:
        return "実行中"
    return f"止めました(stop_process で止めたので、終了コード {code} は誤りではありません)" if proc.stopped else f"終了(終了コード {code})"


_READY = re.compile(r"https?://|Serving|Running on|Listening|listening on|ready in|Local:", re.I)
_SHOWN_PORT = re.compile(r"https?://[^\s/:]+:(\d{2,5})")  # 起動の表示にある URL の番号(Running on http://127.0.0.1:8000 など)
# 起動したサーバーが受け付け始めるまで待つ秒数。Windows では、0.0.0.0 で待ち受けると名前の逆引き(socket.getfqdn)に
# 12 秒ほどかかることがあり(実際にあった)、そのあいだ Flask などは接続を受け付けない
_START_WAIT = 25.0


ENDED_EARLY = "動き続けるはずのプログラムが、すぐに終わっています"
PORT_BUSY = "ほかのプログラムがすでに使っています"
PORT_IN_USE = re.compile(r"address already in use|EADDRINUSE|WinError 10048|Errno 98\b|Errno 48\b|各ソケット アドレス"
                         r"|only one usage of each socket address|is in use by another program", re.I)
# WSAEACCES(10013): 使われているポートや、Windows が予約しているポートで待ち受けようとした(Flask は日本語の文だけを出す)
_ACCESS_DENIED = re.compile(r"WinError 10013|\b10013\b|forbidden by its access permissions|アクセス許可で禁じられた方法", re.I)


def _process_table() -> dict[int, tuple[int, str]]:
    """動いているプログラムの一覧: PID → (親の PID, 名前)。"""
    table: dict[int, tuple[int, str]] = {}
    try:
        if sys.platform != "win32":
            out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                parts = line.split(None, 2)
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    table[int(parts[0])] = (int(parts[1]), parts[2].strip() if len(parts) > 2 else "")
            return table
        import ctypes
        from ctypes import wintypes

        class _Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return table
        try:
            entry = _Entry()
            entry.dwSize = ctypes.sizeof(_Entry)
            more = kernel.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                more = kernel.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snap)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return table


def _listening_pids(port: int) -> list[int]:
    """そのポートで接続を待ち受けているプログラムの PID(Windows では、同じポートで複数が待ち受けていることがある)。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10, creationflags=_no_window()).stdout
            pids = []
            for line in _decode(out).splitlines():
                parts = line.split()
                # 待ち受けの行は、相手の欄が 0.0.0.0:0 か [::]:0(状態の欄の言葉は Windows の言語で変わるので見ない)
                if (len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].rsplit(":", 1)[-1] == str(port)
                        and parts[2] in ("0.0.0.0:0", "[::]:0") and parts[-1].isdigit()):
                    pids.append(int(parts[-1]))
            return list(dict.fromkeys(pids))
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, timeout=10).stdout.split()
        return list(dict.fromkeys(int(x) for x in out))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _listening_pid(port: int) -> int | None:
    """そのポートで接続を待ち受けているプログラムの PID(最初の1つ)。"""
    pids = _listening_pids(port)
    return pids[0] if pids else None


def _started_by(pid: int, procs: list[_Process], table: dict | None = None) -> _Process | None:
    """その PID が、procs のどれかが起動したもの(子・孫を含む)なら、その記録。"""
    table = _process_table() if table is None else table
    by_pid = {p.popen.pid: p for p in procs}
    current, seen = pid, set()
    while current and current not in seen and len(seen) < 12:  # 親をたどる(シェルの子として動いていることが多い)
        if current in by_pid:
            return by_pid[current]
        seen.add(current)
        current = table.get(current, (0, ""))[0]
    return None


def _replace_running(ctx: ToolContext, command: str, cwd: str, ports: list[int]) -> list[str]:
    """この依頼で前に起動したもののうち、同じコマンド(同じ場所)のものと、使いたいポートで待ち受けているものを止める。

    止めないと、古いほうが答え続けて、直したはずの結果が見えない(Windows では、同じポートで2つが待ち受けられてしまう)。
    この依頼の外で起動したもの(残すと言われたサーバーなど)には触らない。止めたものの説明を返す。
    """
    with _PROCS_LOCK:
        mine = [p for label, p in _PROCS.items() if label in ctx.processes and p.popen.poll() is None]
    if not mine:
        return []
    olds = [p for p in mine if p.command == command and p.cwd == cwd]
    held = []
    if ports:
        table = _process_table()
        for port in ports:
            for pid in _listening_pids(port):
                owner = _started_by(pid, mine, table)
                if owner is not None:
                    held.append(port)
                    if owner not in olds:
                        olds.append(owner)
    for old in olds:
        _kill(old)
    deadline = time.time() + 5
    while held and time.time() < deadline:  # 古いほうがポートを手放すまで
        held = [p for p in held if _port_open(p)]
        if held:
            time.sleep(0.2)
    return [f"{p.name}(PID {p.popen.pid})" for p in olds]


def _port_owner(port: int) -> str:
    """そのポートを使っているプログラムの説明と止め方(Celestial が裏で起動したものなら、その名前)。分からなければ空。"""
    pid = _listening_pid(port)
    if not pid:
        return ""
    table = _process_table()
    exe = table.get(pid, (0, ""))[1]
    with _PROCS_LOCK:
        alive = [p for p in _PROCS.values() if p.popen.poll() is None]
    owner = _started_by(pid, alive, table)
    if owner is not None:
        return f"Celestial が裏で起動した {owner.name}、PID {pid}"
    return f"PID {pid}" + (f" の {exe}" if exe else "")


def _busy_note(port: int | None) -> str:
    """ほかのプログラムが使っているポートのときの説明。そのプログラムは止めさせず、空いている番号を示す。"""
    owner = _port_owner(port) if port else ""
    where = f"ポート {port} は" if port else "ポートは"
    spare = f"(例えば {free_port(port)})" if port else ""
    return (f"{where}{PORT_BUSY}" + (f"({owner})" if owner else "")
            + "。そのプログラムは、この作業で起動したものではないので、止めないでください。ユーザーがポートの番号を指定して"
              f"いないなら、コードのポートの番号を空いている番号{spare}に変えて、もう一度起動してください。ユーザーがこの番号を"
              "指定していたなら、番号は変えずに、このことをユーザーに伝えてください。")


def _launch(ctx: ToolContext, command: str, *, name: str = "", keep: bool = False, cwd: str | None = None, wait: float = 4.0,
            ports: list[int] | None = None, expect_running: bool = False, listen: list[int] | None = None) -> str:
    """コマンドを裏で起動し、最初の出力と、接続を受け付け始めたポートを返す(start_process と、自動で裏に回すときの共通部分)。"""
    cwd = cwd or (str(ctx.workspace.resolve()) if ctx.workspace else str(Path.home()))
    words = command.split()
    first = Path(words[0].strip('"')).stem if words else "process"
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", name or first).strip("-")[:24] or "process"
    replaced = _replace_running(ctx, command, cwd, ports or [])
    again = f"前に起動していた {'、'.join(replaced)} を止めて、起動し直しました。\n" if replaced else ""
    with _PROCS_LOCK:
        alive = {n for n, p in _PROCS.items() if p.popen.poll() is None}
        label, n = base, 2
        while label in alive:
            label, n = f"{base}-{n}", n + 1
    held = [p for p in (listen or []) if _port_open(p) or _listening_pids(p)]
    if held:  # 待ち受けるはずのポートを、ほかのプログラムがもう使っている: 起動すると、その接続を横取りすることがある
        return (f"{again}起動していません。{_busy_note(held[0])}"
                "(Windows では同じポートで2つが待ち受けられてしまい、そのプログラムへの接続を横取りすることがあるため、"
                "起動しませんでした)")
    watch = [p for p in (ports or []) if not _port_open(p)]  # 前から使われている番号(別のプログラム)は見ない
    busy = [p for p in (ports or []) if p not in watch]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **_NO_COLOR}
    try:
        popen = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env, creationflags=_no_window(), start_new_session=sys.platform != "win32")
    except OSError as e:
        raise ToolError(f"起動できませんでした: {e}") from e
    proc = _Process(label, command, popen, keep=keep, cwd=cwd, ports=list(watch))
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    with _PROCS_LOCK:
        _PROCS[label] = proc
    if label not in ctx.processes:
        ctx.processes.append(label)
    start, opened = time.time(), []
    limit = max(wait, _START_WAIT) if watch else wait  # サーバーは、受け付け始めるまで十数秒かかることがある
    while time.time() - start < limit and popen.poll() is None:
        time.sleep(0.25)
        opened = [p for p in watch if _port_open(p)]
        shown = proc.text(4000)
        if opened or (not watch and time.time() - start > 1.0 and _READY.search(shown)):
            time.sleep(0.3)  # 起動の表示が出そろうのを少し待つ
            break
        if watch and time.time() - start > wait:  # 延ばして待っているとき: 別の番号で受け付けたと表示されたら、もう待たない
            listed = {int(n) for n in _SHOWN_PORT.findall(shown)}
            if listed and not listed & set(watch):
                break
    # ページの答えは check_url と同じだけ待つ(外部の API を待つページは、3 秒では結果が分からなかった: 実際は HTTP 500 だった)
    peek = _http_peek(opened[0], wait=_CHECK_WAIT) if opened and popen.poll() is None else ""
    if peek:
        proc.asked.add("/")
    if popen.poll() is None and peek:
        time.sleep(0.2)  # 開いたことで終わってしまう作りかどうか
    if re.search(r"を開くと HTTP 404\b", peek):  # ページを書いたのに、/ でそれを返していない(API だけのサーバーなら 404 で正しい)
        pages = _page_files(cwd, command)
        peek += _no_page_note(opened[0], pages) if pages else ""
    elif re.search(r"を開くと HTTP 200\b", peek) and popen.poll() is None:  # ページが読み込む JavaScript と、呼ぶ API まで開く
        url = f"http://127.0.0.1:{opened[0]}/"
        status, kind, page = _get_local(url)
        if status == 200 and "html" in kind.lower():
            peek += page_wiring(url, page, proc.asked)
    output = _quiet_access_log(proc.text(2000).strip(), proc.asked) or "(まだ出力なし)"
    if popen.poll() is not None:
        if PORT_IN_USE.search(output) or (busy and _ACCESS_DENIED.search(output)):  # コードの誤りではなく、ポートの取り合い
            taken = busy[0] if busy else next(iter(ports or []), None)
            return f"{again}{label} はすぐに終了しました(終了コード {popen.returncode})。{_busy_note(taken)}出力:\n{output}"
        if _ACCESS_DENIED.search(output) and ports:  # 使っているプログラムはないのに使えない: Windows が予約している番号
            return (f"{again}{label} はすぐに終了しました(終了コード {popen.returncode})。ポート {ports[0]} は、この PC では"
                    f"使えません(Windows が予約している番号のようです)。コードのポートの番号を、空いている番号(例えば "
                    f"{free_port(ports[0])})に変えて、もう一度起動してください。出力:\n{output}")
        why = (f"。{ENDED_EARLY}(サーバーは止めるまで待ち受け続け、ウィンドウのアプリは開いたままになるはずです)。"
               "コードを直して、もう一度動かしてください") if expect_running else ""
        return f"{again}{label} はすぐに終了しました(終了コード {popen.returncode}){why}。出力:\n{output}"
    after = "依頼が終わっても動かし続けます" if proc.keep else "依頼が終わったら止めます"
    text = f"{again}{label} を起動しました(PID {popen.pid}、{after})。"
    if opened:
        text += f"\nポート {opened[0]} で接続を受け付けています。{peek}\n詳しく確かめるには check_url を使ってください。"
    elif watch:
        text += (f"\nポート {', '.join(map(str, watch))} はまだ受け付けていません(起動に時間がかかっているか、別の番号です)。"
                 "process_output で出力を確かめてください。")
    if busy:  # 起動の前から、別のプログラムがそのポートを使っていた(確かめたときの返事を取り違えないように)
        owner = _port_owner(busy[0])
        text += (f"\n注意: ポート {busy[0]} は、起動する前から{PORT_BUSY}" + (f"({owner})" if owner else "")
                 + "。このプログラムがそのポートにつなぐだけなら問題ありません。待ち受けるつもりなら、確かめても、そのプログラムが"
                   "答えるかもしれません: そのプログラムは止めずに、このプログラムの番号を空いている番号"
                   f"(例えば {free_port(busy[0])})に変えて、起動し直してください。")
    return text + f"\nこれまでの出力:\n{output}"


def start_process(ctx: ToolContext, command: str, name: str = "", keep: str = "") -> str:
    command = _for_shell(command)
    text = command + "\n" + _scripts_run(ctx, command)
    ports = _ports(text)
    return _launch(ctx, command, name=name, keep=str(keep).strip().lower() in ("yes", "true", "1", "はい"),
                   ports=ports, wait=8.0 if ports else 4.0, listen=_listen_ports(text))


def _find_process(name: str) -> _Process:
    key = name.strip()
    with _PROCS_LOCK:
        proc = _PROCS.get(key)
        if proc is None and key:
            matches = [p for n, p in _PROCS.items() if key in n]
            proc = matches[-1] if matches else None
        names = ", ".join(_PROCS) or "なし"
    if proc is None:
        raise ToolError(f"その名前の常駐プログラムはありません: {name}(あるもの: {names})")
    return proc


def process_output(ctx: ToolContext, name: str) -> str:
    proc = _find_process(name)
    return f"{proc.name}: {_state(proc)}\n{_quiet_access_log(proc.text().strip(), proc.asked) or '(出力なし)'}"


def _kill_popen(popen: subprocess.Popen) -> None:
    if popen.poll() is not None:
        return
    try:
        if sys.platform == "win32":  # シェルの子のプログラム(サーバーなど)ごと止める
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(popen.pid)], capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(popen.pid, signal.SIGTERM)
        popen.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            popen.kill()
        except OSError:
            pass


def _kill(proc: _Process) -> None:
    _kill_popen(proc.popen)


STOPPED = "stop_process で止めたので、今は動いていません"
STALE_SERVER = "このサーバーを起動したあとで、コードを変えています"
_RELOADS = re.compile(r"debug\s*=\s*True|--(?:debug|reload)\b|use_reloader\s*=\s*True", re.I)  # 自分で読み直すサーバー


def _stale_files(ctx: ToolContext, proc: "_Process | None") -> list[str]:
    """この依頼で起動したサーバーが読み込むコード(.py)とテンプレート(templates/ の中)のうち、起動したあとで変えたもの。

    Python のサーバーは、起動した時のコードとテンプレートのまま動く(Flask は debug でなければテンプレートも読み直さない)。
    書き直したあとで起動し直さずに確かめると、変える前のサーバーが答えて「成功」になっていた(実際にあった)。
    """
    if proc is None or not proc.cwd or not ctx.changes or _RELOADS.search(proc.command):
        return []
    base = Path(proc.cwd).resolve()
    stale = []
    for rel in ctx.changes:
        path = ctx.workspace / rel
        try:
            if not path.is_file() or path.stat().st_mtime <= proc.started:
                continue
            path.resolve().relative_to(base)
        except (OSError, ValueError):
            continue
        suffix = path.suffix.lower()
        if suffix in (".py", ".pyw") or ("templates" in path.parts and suffix in (".html", ".htm", ".jinja", ".j2")):
            stale.append(rel)
    if not stale:
        return []
    script = re.search(r"[\w./\\-]+\.pyw?\b", proc.command)  # 動かしているファイルが debug=True なら、自分で読み直す
    try:
        main = (base / script.group(0)).read_text(encoding="utf-8", errors="replace") if script else ""
    except OSError:
        main = ""
    return [] if _RELOADS.search(main) else stale


def stop_process(ctx: ToolContext, name: str) -> str:
    proc = _find_process(name)
    if proc.popen.poll() is not None:
        return f"{proc.name} は、もう動いていません({_state(proc)})"
    _kill(proc)
    proc.stopped = True
    where = (f"。ポート {', '.join(map(str, proc.ports))} は、もう受け付けません(開くには、起動し直してください)"
             if proc.ports else "")
    return f"{proc.name} を止めました{where}"


_SCRIPT_ARG = re.compile(r'"([^"]+\.(?:py|pyw|js|mjs|cjs))"|(\S+\.(?:py|pyw|js|mjs|cjs))(?=\s|$)', re.I)


def restart_hint(ctx: ToolContext, command: str, cwd: str, port: int) -> tuple[str, str]:
    """止めたサーバーを、利用者がもう一度動かすための (コマンド, 言い方)。

    run_file で起動したものは、コマンドが python の道のりと -X utf8 を含む長いものなので、利用者が打つ形
    (python app.py)にする。フォルダは、作業フォルダからの道のりで言う。
    """
    script = _SCRIPT_ARG.search(command)
    shown = command
    if script and re.search(r"python|node", command, re.I):
        name = Path(script.group(1) or script.group(2)).name
        shown = f"{'node' if name.lower().endswith(('.js', '.mjs', '.cjs')) else 'python'} {name}"
    folder = f"{cwd} "
    if ctx.workspace is not None:
        try:
            inside = Path(cwd).resolve().relative_to(ctx.workspace.resolve())
            folder = "作業フォルダ" if str(inside) == "." else f"作業フォルダの {inside.as_posix()} "
        except (ValueError, OSError):
            pass
    return shown, f"{folder}で `{shown}` を実行し、http://localhost:{port}/ を開いてください"


def stop_processes(ctx: ToolContext, stopped: list[tuple[str, str]] | None = None) -> list[str]:
    """この依頼で起動した常駐プログラムのうち、残すと言われていないものを止める。残したものの説明を返す。

    stopped を渡すと、止めたサーバー(ポートで受け付けていたもの)の、動かし直すための (コマンド, 言い方) を足す。
    """
    kept = []
    for label in dict.fromkeys(ctx.processes):  # 同じ名前で起動し直したものは1つとして扱う
        with _PROCS_LOCK:
            proc = _PROCS.get(label)
        if proc is None or proc.popen.poll() is not None:
            continue
        if proc.keep:
            kept.append(f"{proc.name}(PID {proc.popen.pid}): {proc.command}")
        else:
            if stopped is not None and proc.ports and any(_port_open(p) for p in proc.ports):
                stopped.append(restart_hint(ctx, proc.command, proc.cwd, next(p for p in proc.ports if _port_open(p))))
            _kill(proc)
    return kept


def running_processes() -> list[dict]:
    """動いている常駐プログラムの一覧(画面での表示・停止用)。"""
    with _PROCS_LOCK:
        procs = list(_PROCS.values())
    return [{"name": p.name, "command": p.command, "pid": p.popen.pid, "started": p.started, "keep": p.keep}
            for p in procs if p.popen.poll() is None]


def stop_named(name: str) -> bool:
    with _PROCS_LOCK:
        proc = _PROCS.get(name)
    if proc is None or proc.popen.poll() is not None:
        return False
    _kill(proc)
    proc.stopped = True
    return True


_LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


PAGE_PROBLEM = "ページとサーバーが食い違っています"
_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>([\s\S]*?)</script\s*>", re.I)
_SCRIPT_SRC = re.compile(r"""\bsrc\s*=\s*['"]([^'"]+)['"]""", re.I)
_FETCH_PATH = re.compile(r"""fetch\(\s*['"`]([^'"`\s]+)['"`]""")
# fetch の結果(JSON)を受け取る変数: .then(data => ...) / const data = await res.json()
_JSON_VAR = re.compile(r"""\.json\(\)\s*\)?\s*\.then\(\s*(?:async\s*)?\(?\s*(\w+)\s*\)?\s*=>"""
                       r"""|(?:const|let|var)\s+(\w+)\s*=\s*await\s+[\w.]+\.json\(\)""")
_JS_MEMBERS = {"length", "toFixed", "toString", "toLocaleString", "toUpperCase", "toLowerCase", "map", "forEach", "filter",
               "find", "join", "slice", "includes", "indexOf", "split", "replace", "trim", "push", "concat", "reduce", "some", "every"}


def _get_local(url: str, limit: int = 200_000) -> tuple[int, str, str]:
    """この PC の URL を GET する。戻り値は (状態の番号, 種類, 本文)。つながらなければ (0, "", 理由)。"""
    request = urllib.request.Request(url, headers={"User-Agent": "Celestial-agent"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as res:
            return res.status, res.headers.get("Content-Type", ""), _decode(res.read(limit))
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), _decode(e.read(20_000))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return 0, "", str(getattr(e, "reason", e))


def _resolves(data: object, parts: list[str]) -> bool:
    """JSON の中を data.a.b[0] のようにたどれるか(.length や .toFixed など、JavaScript の決まった呼び方は通す)。"""
    node = data
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return part in _JS_MEMBERS
    return True


OUTSIDE_DOWN = "外のサービスが時間内に答えなかったため"
_TIMED_OUT = re.compile(r"timed out|ReadTimeout|ConnectTimeout|Max retries exceeded|タイムアウト", re.I)


def _error_reply(text: str) -> str:
    """答えが JSON のエラー({"error": ...})なら、その全体(短く)。違えば空。HTTP 200 で返すサーバーもある。"""
    body = text.strip()
    if not body.startswith("{") or len(body) > 2000:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict) or not any(data[k] for k in data if str(k).lower() in ("error", "errors", "err")):
        return ""
    return json.dumps(data, ensure_ascii=False)[:600]


def _json_error(status: int, kind: str, text: str) -> bool:
    """サーバーが、例外を受けてエラーの JSON を返している(HTTP 500 など。HTTP 200 で {"error": ...} を返すものも)。"""
    body = text.strip()
    if status >= 500:
        return len(body) <= 2000 and ("json" in kind.lower() or body.startswith("{"))
    return status == 200 and bool(_error_reply(body))


def _outside_down(status: int, kind: str, text: str) -> bool:
    """サーバーが、外のサービスを待ち切れなかったことを JSON のエラーで返している(コードは例外を受けて正しく答えている)。"""
    return _json_error(status, kind, text) and bool(_TIMED_OUT.search(text))


# これより長く待ってから返ったエラーの JSON は、外のサービスを待ち切れなかったことが多い(requests の timeout=5 など)
_SLOW_ERROR = 4.0
_SLOW_SEEN: dict[str, float] = {}  # 2回とも遅いエラーだった URL → その時刻(しばらくは開き直さない)
_SLOW_KEEP = 120.0


def _slow_error(status: int, kind: str, text: str, seconds: float) -> bool:
    return seconds >= _SLOW_ERROR and _json_error(status, kind, text) and not _TIMED_OUT.search(text)


def _recovered_note(seconds: float) -> str:
    return (f"(1回目は {seconds:.0f} 秒待ってからエラーの答えでしたが、もう一度開くと答えました。外のサービスが一時的に"
            "遅かったようで、コードの誤りではありません)")


def _slow_note(seconds: float, again: str) -> str:
    return (f"注意: サーバーは {seconds:.0f} 秒ほど待ってから、エラーの JSON を返しました({again})。{OUTSIDE_DOWN}と考えられ、"
            "コードは例外を受けて答えているので、コードの誤りではありません(外のサービスが戻れば、正しく返ります)。そのあいだ、"
            "ページはその部分に「取得できませんでした」と出すようにしてください。原因をはっきりさせたいときは、except で例外の"
            "内容(str(e))もエラーの JSON に入れます")


def _retry_slow(url: str, status: int, kind: str, text: str, seconds: float) -> tuple[int, str, str, str]:
    """何秒も待ってから返ったエラーの JSON を、もう一度だけ開く。戻り値は (状態の番号, 種類, 本文, 添える注意)。

    外のサービス(天気の API など)が一時的に遅いだけのエラーを、コードの誤りとして直させないため(5 秒待ってからの
    HTTP 500 で、AIがエラーの文言を書き換えて起動し直していた。実際にあった)。少し前にも2回とも遅かった URL は開き直さない
    (外のサービスが止まっているあいだ、確かめるたびに倍待っていた)。
    """
    if time.monotonic() - _SLOW_SEEN.get(url, float("-inf")) < _SLOW_KEEP:
        return status, kind, text, _slow_note(seconds, "少し前に開いたときも同じでした")
    again = time.monotonic()
    status2, kind2, text2 = _get_local(url)
    seconds2 = time.monotonic() - again
    if status2 == 200 and not _error_reply(text2):
        return status2, kind2, text2, _recovered_note(seconds)
    if _slow_error(status2, kind2, text2, seconds2):
        _SLOW_SEEN[url] = time.monotonic()
        return status2, kind2, text2, _slow_note(max(seconds, seconds2), "もう一度開いても同じでした")
    return status, kind, text, ""  # 2回目はすぐ返るエラー(または、つながらない): 1回目の結果で、今までどおり扱う


_QUICK_WAIT = 2.0  # すぐ返ったエラーの JSON を、開き直すまでに待つ秒数
CAUGHT_ERROR = "サーバーのコードが例外を受け止めて返した、エラーの答えです"


def _quick_note(first: str) -> str:
    return (f"(1回目はエラーの答え({first[:100]})でしたが、少し待ってもう一度開くと答えました。外のサービスなどが一時的に"
            "失敗しただけで、コードの誤りではありません: コードは変えないでください)")


def _caught_note() -> str:
    return (f"これは、{CAUGHT_ERROR}(少し待って開き直しても同じでした)。答えに本当の原因が出ていなければ、except で例外の"
            "内容も返すようにして(例: except Exception as e: return jsonify({'error': str(e)}))、起動し直してから確かめ、"
            "その原因を直してください")


def _retry_quick(url: str, status: int, kind: str, text: str) -> tuple[int, str, str, str]:
    """すぐ返ったエラーの JSON を、少し待ってもう一度だけ開く。戻り値は (状態の番号, 種類, 本文, 添える注意)。

    外のサービス(天気の API)が一度だけ失敗しても、サーバーは例外を受け止めて {"error": "取得できませんでした"} を返す。
    AIはこれをコードの誤りと思い、直す所の無い編集(old と new が同じ)を繰り返した(実際にあった。そのあとは答えていた)。
    2回目も同じなら、サーバーのコードが返したエラーとして、原因の見つけ方を添える。
    """
    time.sleep(_QUICK_WAIT)
    status2, kind2, text2 = _get_local(url)
    if status2 == 200 and not _error_reply(text2):
        return status2, kind2, text2, _quick_note(_error_reply(text) or text.strip())
    if status2 == 0:  # 2回目はつながらなかった: 1回目の答えで
        return status, kind, text, _caught_note()
    if _outside_down(status2, kind2, text2) or not _json_error(status2, kind2, text2):
        return status2, kind2, text2, ""  # 外のサービスを待ち切れなかった・エラーの JSON ではない: 呼ぶ側の扱いのまま
    return status2, kind2, text2, _caught_note()


def _get_api(url: str) -> tuple[int, str, str, str]:
    """ページが呼ぶ API を開く(エラーの JSON が返ったら、もう一度だけ開く)。"""
    started = time.monotonic()
    status, kind, text = _get_local(url)
    seconds = time.monotonic() - started
    if _slow_error(status, kind, text, seconds):
        return _retry_slow(url, status, kind, text, seconds)
    if _json_error(status, kind, text) and not _outside_down(status, kind, text):
        return _retry_quick(url, status, kind, text)
    return status, kind, text, ""


def _outside_note() -> str:
    return (f"注意: {OUTSIDE_DOWN}、サーバーはエラーの JSON を返しています。コードは例外を受けて答えているので、コードの"
            "誤りではありません(外のサービスが戻れば、正しく返ります)。そのあいだ、ページはその部分に「取得できませんでした」"
            "と出すようにしてください")


_ERROR_KEYS = {"error", "errors", "err", "message", "msg", "detail", "details", "warning", "reason", "status", "ok",
               "success", "code", "exception"}


def _guarded(js: str, name: str, chain: str) -> bool:
    """ページが name+chain を「無いかもしれない」ものとして扱っているか(失敗したときだけ返る error などの項目)。

    条件として調べているだけ(if (data.x)・data.x ? :・data.x && ・!data.x・'x' in data など)のときに、エラーの項目か、
    すべての使い方が条件なら真。サーバーがいつも返す値を条件の中だけで読むことは少ないので、食い違いを見逃しにくい。
    """
    expr = re.escape(name + chain)
    key = re.findall(r"\w+", chain)[-1]
    tests = (rf"if\s*\(\s*!*\s*{expr}\s*(?:\)|&&|\|\||[!=]==?)", rf"{expr}\s*(?:\?|&&|\|\||[!=]==?\s*(?:undefined|null))",
             rf"(?:&&|\|\||!)\s*{expr}\b(?!\s*\()", rf"typeof\s+{expr}\b")
    guarded = sum(len(re.findall(t, js)) for t in tests)
    if len(chain.strip(".").split(".")) == 1 and re.search(rf"""['"]{re.escape(key)}['"]\s+in\s+{re.escape(name)}\b|"""
                                                         rf"""{re.escape(name)}\.hasOwnProperty\(\s*['"]{re.escape(key)}['"]""", js):
        guarded += 1
    if not guarded:
        return False
    uses = len(re.findall(rf"{expr}(?![\w\[.])", js))
    return key.lower() in _ERROR_KEYS or guarded >= uses


def _script_error(attrs: str, code: str) -> str:
    """ページの JavaScript の構文の誤りの説明(誤りの行つき)。誤りが無い・JavaScript でない・確かめられなければ空。"""
    kind = script_kind(attrs)
    found = js_syntax_error(code, kind == "module") if kind and code.strip() else None
    if not found:
        return ""
    lines = code.split("\n")
    shown = lines[found[0] - 1].strip()[:160] if 0 < found[0] <= len(lines) else ""
    return f"{found[1]}(<script> の中の {found[0]} 行目: {shown})"


def page_wiring(url: str, html: str, asked: set[str] | None = None) -> str:
    """作ったページを確かめる: ページが読み込む JavaScript と、JavaScript が呼ぶ API を、同じサーバーで実際に開いてみる。

    画面とサーバーの食い違い(呼ぶ URL がサーバーに無い・JSON に無い項目を読む)は、ページを開いただけでは分からない。
    見つけたら PAGE_PROBLEM の見出しを付けて知らせる。外のサイトの JavaScript や API は開かない。
    """
    origin = urllib.parse.urlparse(url).netloc
    lines: list[str] = []
    problems: list[str] = []
    code: list[str] = []
    runnable: list[tuple[int, str]] = []  # 動かして確かめる JavaScript(ページの順の、HTML の中の位置と中身)
    outside_js = syntax_broken = False
    for tag in list(_SCRIPT_TAG.finditer(html))[:8]:
        attrs, body = tag.group(1), tag.group(2)
        src = _SCRIPT_SRC.search(attrs)
        if not src:
            code.append(body)
            broken = _script_error(attrs, body)
            if broken:
                syntax_broken = True
                problems.append(f"ページの <script> に、JavaScript の構文の誤りがあります(ブラウザーでは、この <script> が動きません): {broken}")
            elif script_kind(attrs) == "classic":
                runnable.append((tag.start(), body))
            continue
        target = urllib.parse.urljoin(url, src.group(1))
        if urllib.parse.urlparse(target).netloc != origin:
            outside_js = True  # 外のサイトの JavaScript(CDN など): 開かないので、それが作る名前は分からない
            continue
        if asked is not None:
            asked.add(urllib.parse.urlparse(target).path)
        status, _, text = _get_local(target)
        lines.append(f"- 読み込む JavaScript {src.group(1)} → " + (f"HTTP {status}" if status else f"つながりません({text[:80]})"))
        if status == 200:
            code.append(text)
            plain = re.sub(r"\bsrc\s*=\s*\S+", "", attrs)
            broken = _script_error(plain, text)
            if broken:
                syntax_broken = True
                problems.append(f"ページが読み込む {src.group(1)} に、JavaScript の構文の誤りがあります: {broken}")
            elif script_kind(plain) == "classic":  # defer・async は、ページを読み終えてから動く
                runnable.append((len(html) if re.search(r"\b(?:defer|async)\b", attrs, re.I) else tag.start(), text))
        else:
            problems.append(f"ページが読み込む {src.group(1)} を、サーバーが返しません(HTTP {status})")
    js = "\n".join(code)
    replies: dict[str, object] = {}
    bodies: dict[str, tuple[int, str]] = {}  # ページを動かすときに fetch へ返す、同じサーバーの答え(道のり → 状態, 本文)
    for raw in list(dict.fromkeys(_FETCH_PATH.findall(js)))[:4]:
        path = raw.split("?")[0]
        if "${" in path or "+" in path:
            continue  # 組み立てて作る URL は、ここでは確かめない
        target = urllib.parse.urljoin(url, path)
        if urllib.parse.urlparse(target).netloc != origin:
            continue
        if asked is not None:
            asked.add(urllib.parse.urlparse(target).path)
        status, kind, text, slow = _get_api(target)
        failed = _error_reply(text) if status == 200 else ""  # HTTP 200 でも、中身はエラーの答え
        if status != 200 or failed:
            # エラーの答えは、ページが読む項目の照らし合わせに使わない(外のサービスが止まっているあいだの {"error": ...} に
            # data.humidity が無いのを「食い違い」とし、AIがエラーの答えに 'humidity': None を足していた。実際にあった)
            lines.append(f"- fetch('{raw}') → " + (f"HTTP {status}" if status else f"つながりません({text[:80]})")
                         + (f"(エラーの答え): {failed[:300]}" if failed else ""))
            if status == 404:
                problems.append(f"ページが呼ぶ {path} が、サーバーにありません(HTTP 404)。サーバーにこの URL を作るか、ページ側の URL を直してください")
            elif slow and OUTSIDE_DOWN in slow:  # 何秒も待ってからのエラーが続いた: 外のサービスを待ち切れなかった
                lines.append(f"  {slow}")
            elif _outside_down(status, kind, text):  # 外のサービスが答えないだけ: コードの誤りではない
                lines.append(f"  {_outside_note()}")
            elif failed:
                problems.append(f"ページが呼ぶ {path} が、エラーの答えを返しました: {failed[:200]}。"
                                + (slow if CAUGHT_ERROR in slow else "サーバーの出力と、例外の内容を確かめて直してください"))
            elif status >= 500:
                problems.append(f"ページが呼ぶ {path} で、サーバーがエラーになりました(HTTP {status}): {text.strip()[:200]}"
                                + (f"。{slow}" if CAUGHT_ERROR in slow else ""))
            continue
        bodies[urllib.parse.urlparse(target).path] = (status, text)  # エラーの答えは渡さない(ページの fetch は答えが返らないまま)
        try:
            replies[path] = json.loads(text)
        except ValueError:
            lines.append(f"- fetch('{raw}') → HTTP 200(JSON ではありません)")
            continue
        lines.append(f"- fetch('{raw}') → HTTP 200 JSON: {json.dumps(replies[path], ensure_ascii=False)[:300]}"
                     + (f"\n  {slow}" if slow else ""))
    if replies:
        for name in {a or b for a, b in _JSON_VAR.findall(js)}:
            for chain in sorted(set(re.findall(rf"\b{re.escape(name)}((?:\.\w+|\[['\"]?\w+['\"]?\])+)", js))):
                if _guarded(js, name, chain):  # 無いときの分かれ道を持っている項目(失敗したときだけ返る error など)
                    continue
                if not any(_resolves(data, re.findall(r"\w+", chain)) for data in replies.values()):
                    have = "; ".join(f"{p}: {', '.join(map(str, d))[:120]}" if isinstance(d, dict) else f"{p}: {type(d).__name__}"
                                     for p, d in replies.items())
                    problems.append(f"ページは {name}{chain} を読んでいますが、サーバーの JSON にその項目がありません(ある項目は {have})。"
                                    "ページが読む名前と、サーバーが返す名前をそろえてください")
    # ページの JavaScript を実際に動かす(HTML に無い要素を探して止まる・天気を番号のまま出す、は開いただけでは分からない)
    run = pagerun.run_page(url, html, runnable, bodies, outside_js) if runnable and not syntax_broken else None
    if run is not None:
        notes, found = pagerun.describe(run, [body for _, body in runnable])
        lines += notes
        problems += found
    if not lines and not problems:
        return ""
    out = "\n\n[ページの確認: ページが使う JavaScript と API を開いた結果]\n" + "\n".join(lines)
    if problems:
        out += f"\n{PAGE_PROBLEM}:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(problems))
    return out


def _starting(ctx: ToolContext) -> bool:
    """この依頼で起動した常駐プログラムが、まだ動いているか(起動の途中かもしれない)。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in ctx.processes]
    return any(p is not None and p.popen.poll() is None for p in procs)


def _await_port(ctx: ToolContext, port: int, limit: float = 0.0) -> float:
    """起動中のサーバーが、そのポートで受け付け始めるのを待つ。待った秒数を返す(受け付けなければ 0)。"""
    start, limit = time.time(), limit or _START_WAIT
    while time.time() - start < limit and _starting(ctx):
        if _port_open(port):
            return max(time.time() - start, 0.1)
        time.sleep(0.5)
    return 0.0


def _open(ctx: ToolContext, target: str) -> str:
    """開く。この PC の中のページ(http://localhost:番号/...)なら、中身を実際に取得した結果も添える。

    小さいAIは、作ったページを「ブラウザで開いて画面撮影」で確かめようとしがちで、画面撮影には別のウィンドウが写っていても
    「表示されました」と報告していた。開いたページが HTTP 500 などなら、その場で分かるようにする。
    """
    inside = _in_workspace(ctx, target)
    if inside is not None:  # 作業フォルダの中のもの: 作業フォルダから開く(Celestial を動かしているフォルダから探していた)
        rel = _relative(ctx, inside)
        if inside.is_file() and ctx.changes is not None and rel in ctx.changes and not _ASKS_TO_OPEN.search(ctx.task):
            # この依頼で作った・変えたファイルを、頼まれていないのにアプリで開こうとしている(確かめのつもり。利用者の画面に
            # Excel などが開く): 開かずに、中身を見せる
            try:
                shown = _HANDLERS["read_file"](ctx, path=rel)
            except ToolError as e:
                shown = f"ERROR: {e}"
            return (f"アプリでは開いていません({rel} はこの依頼で作ったファイルで、開くよう頼まれていないため。利用者の画面には何も"
                    f"開いていません)。中身は次のとおりです:\n{shown}\n(The file was not opened in an app because the user did "
                    "not ask for it; its content is shown above. Do not open it again: if the goal is reached, finish now "
                    "with the answer tool.)")
        target = str(inside)
    parsed = urllib.parse.urlparse(target.strip())
    local = parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in _LOCAL
    ours = _ours_on(ctx, parsed.port or (443 if parsed.scheme == "https" else 80)) if local else None
    if ours is not None and not ours.keep:
        # 依頼が終わると止めるサーバーのページ: タブを開くと、止めたあとも動かないページのまま残り、止まったサーバーを呼び
        # 続ける(前の試しのタブが /api/time を毎秒呼び続けていた。実際にあった)。開かずに、中身だけ確かめる
        opened = (f"{NOT_OPENED}({ours.name} は依頼が終わると止めるので、開いたタブは動かないまま残ります。"
                  "使うときは、利用者が起動してから開きます)。")
    else:
        opened = pc.open_target(target)
    if not local:
        return opened
    try:
        checked = check_url(ctx, target)
    except ToolError as e:
        checked = f"ERROR: {e}"
    return f"{opened}\n{OPENED_PAGE}\n{checked}"


_ASKS_TO_OPEN = re.compile(r"開いて|開く|開け|見せて|\b(?:open|show me)\b", re.I)


def _in_workspace(ctx: ToolContext, target: str) -> Path | None:
    """target が作業フォルダの中のファイルやフォルダなら、その場所(アプリの名前や URL なら None)。"""
    raw = target.strip().strip('"')
    if ctx.workspace is None or not raw or re.match(r"^[a-z][a-z0-9+.-]+://", raw, re.I):
        return None
    try:
        place = _resolve(ctx, raw)
    except ToolError:
        return None
    return place if place.exists() else None


OPENED_PAGE = "--- このページの中身を取得した結果(check_url と同じ) ---"
NOT_OPENED = "ブラウザーでは開いていません"
_CHECK_WAIT = 15  # ページの答えを待つ秒数


def check_url(ctx: ToolContext, url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "file":  # 作ったファイルを file:// で確かめようとした: 作業フォルダの中なら、中身を読んで返す
        place = _in_workspace(ctx, urllib.request.url2pathname(parsed.path))
        if place is not None and place.is_file():
            rel = _relative(ctx, place)
            return (f"(file:// はページの URL ではないので、作業フォルダのファイル {rel} として中身を読みました)\n"
                    + _HANDLERS["read_file"](ctx, path=rel))
    if parsed.scheme not in ("http", "https") or host not in _LOCAL:
        raise ToolError("確かめられるのは、この PC の中の URL(http://localhost:番号/...)だけです")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    waited = 0.0
    # 起動したばかりのサーバー: 受け付け始めるまで少し待つ。接続を試すのは、この依頼で起動したものがあるときだけ
    # (試しの接続も、相手には接続1回に見える。1回だけ答えるようなプログラムの邪魔をしない)
    if _starting(ctx) and not _port_open(port):
        waited = _await_port(ctx, port)
    target = url.strip().replace("0.0.0.0", "127.0.0.1")
    request = urllib.request.Request(target, headers={"User-Agent": "Celestial-agent"})
    not_http = ("接続はできましたが、HTTP の応答が返りませんでした({})。HTTP のサーバーになっているか"
                "(Python なら http.server の BaseHTTPRequestHandler と serve_forever など)を確かめてください")
    started = time.monotonic()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=_CHECK_WAIT) as res:
            body, status, kind = res.read(200_000), res.status, res.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body, status, kind = e.read(20_000), e.code, e.headers.get("Content-Type", "")
    except (http.client.HTTPException, ConnectionResetError, ConnectionAbortedError) as e:
        raise ToolError(not_http.format(type(e).__name__)) from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (http.client.HTTPException, ConnectionResetError, ConnectionAbortedError)):
            raise ToolError(not_http.format(type(reason).__name__)) from e
        if isinstance(reason, ConnectionRefusedError):
            halted = _stopped_on(ctx, port)
            if halted:  # 自分で止めたサーバーを開いた(止めたことを忘れて「落ちた」と思い、起動し直しがち)
                raise ToolError(f"接続できませんでした。このポートで受け付けていた {halted.name} は、{STOPPED}"
                                "(もう一度開くには、起動し直してください)") from e
            still = f"起動したプログラムは動いていますが、{_START_WAIT:.0f} 秒待っても" if _starting(ctx) else ""
            raise ToolError(f"接続できませんでした({still}このポートで待ち受けているサーバーがありません)。サーバーが動いているか"
                            "(process_output)と、番号が合っているかを確かめてください") from e
        if _timed_out_error(e) and _port_open(port):
            tail = _server_tail(ctx)
            raise ToolError(SLOW_PAGE.format(seconds=_CHECK_WAIT)
                            + (f"\n--- サーバーの出力(最後の部分) ---\n{tail}" if tail else "")) from e
        raise ToolError(f"接続できませんでした({reason})。サーバーが起動しているか、番号が合っているかを確かめてください") from e
    text = _decode(body)
    slow = caught = ""
    if _slow_error(status, kind, text, time.monotonic() - started):  # 何秒も待ってからのエラー: 外のサービスが一時的に遅いだけかも
        status, kind, text, slow = _retry_slow(target, status, kind, text, time.monotonic() - started)
    elif _json_error(status, kind, text) and not _outside_down(status, kind, text):
        # すぐ返ったエラーの JSON: 外のサービスが一度だけ失敗したのかもしれない。少し待って、もう一度だけ開く
        status, kind, text, slow = _retry_quick(target, status, kind, text)
        if CAUGHT_ERROR in slow:  # 2回とも同じエラー: 原因の見つけ方は、答えのあとに添える(最初の行は HTTP の状態のまま)
            caught, slow = f"\n注意: {slow}", ""
    proc = _proc_on(ctx, port)
    if proc is not None:
        proc.asked.add(parsed.path or "/")
    wiring = page_wiring(target, text, proc.asked if proc else None) if status == 200 and "html" in kind.lower() else ""
    note = f"(サーバーが受け付け始めるまで {waited:.0f} 秒待ちました)\n" if waited >= 1 else ""
    stale = _stale_files(ctx, proc)
    if stale:  # 起動したあとでコードを変えた: 答えたのは、変える前のコードのサーバー
        note = (f"{STALE_SERVER}({'、'.join(stale[:4])})。動いているサーバーは、変える前のコードのままです。run_file(または "
                "start_process)で起動し直してから、もう一度確かめてください。以下は、変える前のサーバーの答えです。\n") + note
    if slow and OUTSIDE_DOWN in slow:  # 2回とも遅いエラー: 外のサービスを待ち切れなかった
        return f"{note}HTTP {status}  {kind}\n{text.strip()[:600]}\n{slow}"
    if slow:  # もう一度開くと答えた
        note += slow + "\n"
    if _outside_down(status, kind, text):  # 外のサービスが答えないだけ(コードは例外を受けて答えている)
        return f"{note}HTTP {status}  {kind}\n{text.strip()[:600]}\n{_outside_note()}"
    if status >= 500:  # サーバーの中のエラー: ページの HTML より、エラーの文と、サーバーが出した Traceback が役に立つ
        title = _error_title(text)
        tail = _server_tail(ctx)
        shown = (f"エラー: {title}\n" if title else text[:600] + "\n") + (f"--- サーバーの出力(最後の部分) ---\n{tail}" if tail else "")
        return f"{note}HTTP {status}  {kind}\n{shown}{caught}"
    if status == 404 and parsed.path in ("", "/"):  # ページを書いたのに、/ でそれを返していない
        proc = _proc_on(ctx, port)
        pages = _page_files(proc.cwd, proc.command) if proc and proc.cwd else []
        if pages:
            return f"{note}HTTP 404  {kind}\n{text.strip()[:300]}" + _no_page_note(port, pages)
    if status < 400 and _error_only(text, kind):  # 開けたように見えて、中身はエラーの文だけ
        tail = _server_tail(ctx)
        return (f"{note}HTTP {status}  {kind}\n{text.strip()[:600]}\n注意: {ERROR_PAGE}(コードの中で例外を受けて、文だけを"
                "返しているようです)。" + (f"\n--- サーバーの出力(最後の部分。本当の原因が出ていることが多い) ---\n{tail}" if tail else ""))
    return (f"{note}HTTP {status}  {kind}\n{text[:2000]}" + ("\n... (長いため省略)" if len(text) > 2000 else "") + wiring
            + caught)


def server_problems(ctx: ToolContext, seen: list[str] | None = None) -> list[str]:
    """この依頼で起動して動いているサーバーのうち、ページ(/)を開くと HTTP 500 などのエラーになるもの、ページの代わりに
    エラーの文だけを返すもの、ページが呼ぶ API や JavaScript がかみ合わないもの(報告の前の確かめ)。

    確かなものだけを挙げる: 受け付けていない・HTTP でない(ソケットのサーバーなど)・404 は、作りによっては正しいので挙げない。
    seen を渡すと、問題なく開けたサーバーの、開いた URL と返った中身を足す(報告を書くAIに、実物を渡すため)。
    """
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in dict.fromkeys(ctx.processes)]
    problems = []
    deadline = time.time() + _START_WAIT
    for proc in procs:
        if proc is None or proc.popen.poll() is not None or not proc.ports:
            continue
        while not _port_open(proc.ports[0]) and proc.popen.poll() is None and time.time() < deadline:
            time.sleep(0.5)  # 起動の途中(受け付け始めるまで十数秒かかることがある)
        if proc.popen.poll() is not None or not _port_open(proc.ports[0]):
            continue
        peek = _http_peek(proc.ports[0], wait=_CHECK_WAIT)
        status = re.search(r"を開くと HTTP (\d{3})", peek)
        pages = _page_files(proc.cwd, proc.command) if status and status.group(1) == "404" and proc.cwd else []
        if pages:
            peek += _no_page_note(proc.ports[0], pages)
        if (status and status.group(1).startswith("5")) or ERROR_PAGE in peek or SLOW_MARK in peek or pages:
            problems.append(f"{proc.name}: {peek}")
        elif status and status.group(1) == "200":  # ページは開ける: 読み込む JavaScript と、呼ぶ API まで開く
            url = f"http://127.0.0.1:{proc.ports[0]}/"
            code, kind, page = _get_local(url)
            proc.asked.add("/")
            wiring = page_wiring(url, page, proc.asked) if code == 200 and "html" in kind.lower() else ""
            if PAGE_PROBLEM in wiring:
                found = wiring.split(PAGE_PROBLEM + ":", 1)[1].strip()
                problems.append(f"{proc.name}: http://localhost:{proc.ports[0]}/ は開けますが、"
                                + " / ".join(line.lstrip("- ").strip() for line in found.splitlines() if line.strip()))
            elif seen is not None:
                seen.append(f"{proc.name}: http://localhost:{proc.ports[0]}/ → HTTP 200"
                            + "".join(f"\n  {line}" for line in wiring.strip().splitlines()[1:]))
    return problems


def serving(ctx: ToolContext) -> bool:
    """この依頼で起動したサーバーのうち、今も接続を受け付けているものがあるか。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in dict.fromkeys(ctx.processes)]
    return any(p is not None and p.popen.poll() is None and p.ports and _port_open(p.ports[0]) for p in procs)


def running_server(ctx: ToolContext, name: str) -> bool:
    """その名前の常駐プログラムが、この依頼で起動したサーバーで、今も接続を受け付けているか。"""
    try:
        proc = _find_process(name)
    except ToolError:
        return False
    return (proc.name in ctx.processes and proc.popen.poll() is None and bool(proc.ports)
            and _port_open(proc.ports[0]))


def _ours_on(ctx: ToolContext, port: int) -> _Process | None:
    """この依頼で起動したもののうち、そのポートで受け付けている(はずの)もの。動いているものを先に選ぶ。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in dict.fromkeys(ctx.processes)]
    mine = [p for p in procs if p is not None and port in p.ports]
    return max(mine, key=lambda p: (p.popen.poll() is None, p.started)) if mine else None


def _stopped_on(ctx: ToolContext, port: int) -> _Process | None:
    """この依頼で起動して stop_process で止めたもののうち、そのポートで受け付けていたもの(今そこで動いているものが
    無いときだけ)。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in dict.fromkeys(ctx.processes)]
    if any(p is not None and p.popen.poll() is None and port in p.ports for p in procs):
        return None
    halted = [p for p in procs if p is not None and p.stopped and port in p.ports]
    return max(halted, key=lambda p: p.started) if halted else None


def _proc_on(ctx: ToolContext, port: int) -> _Process | None:
    """この依頼で起動して動いているもののうち、そのポートで受け付けているもの。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in dict.fromkeys(ctx.processes)]
    alive = [p for p in procs if p is not None and p.popen.poll() is None and port in p.ports]
    return max(alive, key=lambda p: p.started) if alive else None


def is_server_file(ctx: ToolContext, path: str) -> bool:
    """作業フォルダのそのファイルが、Web サーバーなどの待ち受けるプログラムか(報告の前の確かめに使う)。"""
    return _long_running(file_text(ctx, path)) == "server"


def _server_tail(ctx: ToolContext, limit: int = 1500) -> str:
    """この依頼で起動して動いているもののうち、最後に起動したものの出力の最後(Traceback が入っていることが多い)。"""
    with _PROCS_LOCK:
        procs = [_PROCS.get(label) for label in ctx.processes]
    alive = [p for p in procs if p is not None and p.popen.poll() is None]
    if not alive:
        return ""
    last = max(alive, key=lambda p: p.started)
    return _quiet_access_log(last.text(limit).strip(), last.asked)


# ---- PC操作・ネット・ワーカー(実体は pc.py / web.py / 管理AI) ----------------

_ABOUT_SCREEN = re.compile(r"画面|スクリーン|スクショ|ウィンドウ|デスクトップ|映って|写って|見えて|操作|クリック|入力して|押して|"
                           r"開いて|開いた|\b(?:screen\w*|window\w*|desktop|click|type|press|open)\b", re.I)
_NOT_OPERATING = {"screenshot", "system_info", "list_windows", "list_processes", "clipboard_read",
                  "wait", "volume", "media", "speak", "notify_pc", "network_info"}


def _screenshot(ctx: ToolContext, question: str = "") -> str:
    if ctx.by_agent and not ctx.operated and not _ABOUT_SCREEN.search(ctx.task):
        # 画面やアプリの操作の依頼ではなく、この依頼で PC を操作してもいない: 画面には、利用者が使っているほかのアプリなど、
        # 作業と関係のないものが写る(作った Excel を確かめるつもりで、利用者の画面を撮っていた。実際にあった)
        raise ToolError("利用者の画面は撮っていません(この依頼は画面やアプリの操作ではないため。画面には、作業と関係のない、"
                        "利用者が使っているほかのアプリが写ります)。作ったファイルは read_file で、作ったページは check_url で"
                        "確かめてください。(The user's screen was not captured: the request is not about the screen.)")
    folder = ctx.scratch_dir or Path(tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "screen.png"
    width, height = pc.screenshot(path)
    if ctx.describe_image is None:
        return f"画面を保存しました({width}×{height})が、画像を見られるAIが無いため内容は分かりません: {path}"
    prompt = ("This is a screenshot of the user's screen. " + (f"Answer this: {question}\n" if question.strip() else "") +
              "Describe what is visible: which apps/windows are open, the main text, and the approximate pixel position (x, y) "
              f"of important buttons or fields. The image is {width}x{height} pixels.")
    return f"[画面 {width}×{height}]\n" + ctx.describe_image(path, prompt)


_KEY_HUNT = re.compile(r"(API|ＡＰＩ)\s*キー|アクセスキー|\bapi[ _-]?key|\baccess token|アカウントを(作|登録)|サインアップ|\bsign[ -]?up", re.I)
_KEY_VERB = re.compile(r"取得|入手|発行|調べ|教え|作成|登録|設定|\b(get|obtain|find|create|register)\b", re.I)
_UNSEEN = re.compile(r"\b[\w-]+\.(?:py|js|ts|html|css|json|csv|tsv|txt|md|log|xlsx|docx|pptx|yaml|yml|toml|ini|cfg)\b|出力|実行結果|"
                     r"\b(?:output|the log)\b", re.I)
_LOOK_AT = re.compile(r"解析|分析|読んで|読み取|確認|調べ|直して|修正|要約|説明|整形|"
                      r"\b(?:parse|analy[sz]e|read|check|fix|summari[sz]e|explain|review)\b", re.I)


def delegate(ctx: ToolContext, role: str, task: str) -> str:
    """専門のワーカー(別のAI)に、それだけで完結する仕事を頼む。ワーカーには作業フォルダ・出力・画面・ネットが見えない。"""
    text = task.strip()
    if not text:
        raise ToolError("task が空です。頼みたいことを、必要な文章ごと task に書いてください")
    if _KEY_HUNT.search(text) and _KEY_VERB.search(text):
        raise ToolError("ワーカーのAIは、サービスへの登録や API キーの取得ができません(ネットにもつながっていません)。"
                        "キーのいらないサービスを使ってください(天気なら Open-Meteo: https://api.open-meteo.com/v1/forecast)。"
                        "どうしてもキーが要るときは、コードでは環境変数から読むようにして、最後の報告で利用者に設定のしかたを伝えてください")
    if _UNSEEN.search(text) and _LOOK_AT.search(text) and "\n" not in text and len(text) < 300:
        raise ToolError("ワーカーのAIには、作業フォルダのファイルやプログラムの出力が見えません。"
                        "read_file や process_output で中身を読んでから、必要な部分を task に貼り付けて頼むか、自分で道具を使って進めてください")
    return ctx.delegate(role, text)


def _number(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as e:
        raise ToolError(f"{name} は数字で指定してください") from e


def _safe_sql_readonly(sql: str) -> str:
    clean = re.sub(r"^\s+|\s+$", "", sql).rstrip(";")
    if not clean:
        return ""
    if ";" in clean:
        raise ToolError("SQL は 1 文だけ指定してください")
    first = clean.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
        raise ToolError("安全のため、SELECT / WITH / PRAGMA / EXPLAIN の読み取りクエリのみ実行できます")
    if first == "PRAGMA" and "=" in clean:
        raise ToolError("読み取り用の PRAGMA のみ実行できます")
    return clean


def _format_sql_rows(cols: list[str], rows: list[tuple], max_rows: int = 80) -> str:
    shown = rows[:max_rows]
    if not cols:
        return "(結果列なし)"
    lines = [" | ".join(str(c) for c in cols), " | ".join("---" for _ in cols)]
    for r in shown:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    note = f"\n... (全 {len(rows)} 行中 {len(shown)} 行を表示)" if len(rows) > len(shown) else f"\n({len(rows)} 行)"
    return "\n".join(lines) + note


def _coerce_sql_val(raw):
    if isinstance(raw, (int, float)):
        return raw
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return ""
    plain = s.replace(",", "")
    if re.fullmatch(r"[-+]?\d+", plain):
        return int(plain)
    if re.fullmatch(r"[-+]?(?:\d+\.\d+|\.\d+)", plain):
        return float(plain)
    return s


def csv_query(ctx: ToolContext, path: str, sql: str = "") -> str:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {path}")
    raw = _read_text(target, 2_000_000)
    suffix = target.suffix.lower()
    headers: list[str] = []
    data_rows: list[list] = []
    if suffix == ".json":
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise ToolError(f"JSON を読めません: {e}") from e
        if not isinstance(obj, list) or not obj:
            raise ToolError("JSON 配列のファイルのみ SQL 検索できます")
        if isinstance(obj[0], dict):
            headers = list(dict.fromkeys(k for row in obj if isinstance(row, dict) for k in row.keys()))
            data_rows = [[row.get(h, "") if isinstance(row, dict) else "" for h in headers] for row in obj]
        elif isinstance(obj[0], list):
            headers = [str(x) for x in obj[0]]
            data_rows = obj[1:]
        else:
            raise ToolError("JSON 配列の各要素はオブジェクトまたは配列にしてください")
    else:
        delim = "\t" if suffix == ".tsv" else ","
        parsed = list(csv.reader(io.StringIO(raw), delimiter=delim))
        if not parsed:
            raise ToolError("表データが空です")
        headers = [h.strip() or f"col{i+1}" for i, h in enumerate(parsed[0])]
        data_rows = parsed[1:]

    cols = []
    seen_c: dict[str, int] = {}
    for i, h in enumerate(headers):
        base = re.sub(r"\W+", "_", h).strip("_") or f"col{i+1}"
        if base[0].isdigit():
            base = f"c_{base}"
        idx = seen_c.get(base, 0)
        seen_c[base] = idx + 1
        cols.append(f"{base}_{idx+1}" if idx else base)

    db = sqlite3.connect(":memory:")
    try:
        col_defs = ", ".join(f'"{c}"' for c in cols)
        db.execute(f"CREATE TABLE t ({col_defs})")
        marks = ", ".join("?" * len(cols))
        coerced = [[_coerce_sql_val(r[i] if i < len(r) else "") for i in range(len(cols))] for r in data_rows]
        db.executemany(f"INSERT INTO t VALUES ({marks})", coerced)
        q = _safe_sql_readonly(sql)
        if not q:
            sample = db.execute("SELECT * FROM t LIMIT 5").fetchall()
            return (f"テーブル名: t / 列: {', '.join(cols)} / 行数: {len(data_rows)}\n"
                    + _format_sql_rows(cols, sample, max_rows=5))
        cur = db.execute(q)
        out_cols = [d[0] for d in (cur.description or [])]
        return _format_sql_rows(out_cols, cur.fetchall())
    except sqlite3.Error as e:
        raise ToolError(f"SQL エラー: {e}") from e
    finally:
        db.close()


def sqlite_query(ctx: ToolContext, path: str, sql: str = "") -> str:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"ファイルがありません: {path}")
    uri = target.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        q = _safe_sql_readonly(sql)
        if not q:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()]
            if not tables:
                return "(テーブルがありません)"
            info = []
            for tbl in tables[:30]:
                cnt = db.execute(f'SELECT COUNT(*) FROM "{tbl.replace(chr(34), chr(34)*2)}"').fetchone()[0]
                tcols = [c[1] for c in db.execute(f'PRAGMA table_info("{tbl.replace(chr(34), chr(34)*2)}")').fetchall()]
                info.append(f"- {tbl} ({cnt} 行): {', '.join(tcols)}")
            return "\n".join(info)
        cur = db.execute(q)
        out_cols = [d[0] for d in (cur.description or [])]
        return _format_sql_rows(out_cols, cur.fetchall())
    except sqlite3.Error as e:
        raise ToolError(f"SQLite エラー: {e}") from e
    finally:
        db.close()


def git_info(ctx: ToolContext, action: str = "status", target: str = "", limit: str = "20") -> str:
    if ctx.workspace is None:
        raise ToolError("作業フォルダが設定されていません")
    if shutil.which("git") is None:
        raise ToolError("git コマンドが見つかりません")
    act = (action or "status").strip().lower()
    if act not in ("status", "diff", "log", "branch", "show"):
        raise ToolError("action は status / diff / log / branch / show のいずれかを指定してください")
    n = max(1, min(100, int(_number(limit or "20", "limit"))))
    cmd = ["git"]
    if act == "status":
        cmd += ["status", "--short", "--branch"]
    elif act == "branch":
        cmd += ["branch", "-a"]
    elif act == "log":
        cmd += ["log", f"-n{n}", "--oneline", "--decorate"]
    elif act == "diff":
        cmd += ["diff", "--stat"] if not target.strip() else ["diff"]
    elif act == "show":
        cmd += ["show", "--stat", target.strip() or "HEAD"]
    if act in ("status", "diff", "log") and target.strip():
        _resolve(ctx, target.strip())  # パストラバーサル防止
        cmd += ["--", target.strip()]
    try:
        proc = _run_captured(cmd, cwd=str(ctx.workspace.resolve()), timeout=15)
    except _TimedOut as e:
        raise _timed_out(15, e)
    out = _finish(proc)
    return out or "(変更や該当履歴はありません)"


def regex_replace(ctx: ToolContext, path: str, pattern: str, replacement: str, dry_run: str = "") -> str:
    if not pattern:
        raise ToolError("pattern が空です")
    try:
        rx = re.compile(pattern, re.M)
    except re.error as e:
        raise ToolError(f"正規表現が正しくありません: {e}") from e
    dry = dry_run.strip().lower() in ("1", "true", "yes", "dry", "preview")
    root = ctx.workspace.resolve() if ctx.workspace else None
    if _PATTERN.search(path):
        if root is None:
            raise ToolError("作業フォルダが設定されていません")
        files = [p for p in sorted(root.rglob("*")) if p.is_file() and fnmatch.fnmatch(_relative(ctx, p), path.replace("\\", "/"))
                 and not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)][:60]
    else:
        tgt = _resolve(ctx, path)
        if not tgt.is_file():
            raise ToolError(f"ファイルがありません: {path}")
        files = [tgt]
    if not files:
        return "一致するファイルが見つかりませんでした"
    changed_files, total_hits = [], 0
    for f in files:
        try:
            text = _read_text(f, 500_000)
        except ToolError:
            continue
        new_text, count = rx.subn(replacement, text)
        if not count:
            continue
        total_hits += count
        rel = _relative(ctx, f)
        if not dry:
            newline = _newline_of(f)
            _backup(ctx, f)
            f.write_bytes(_encode(new_text, newline))
            if ctx.changes is not None and rel not in ctx.changes:
                ctx.changes[rel] = text
        changed_files.append(f"{rel} ({count} 箇所)")
    if not changed_files:
        return f"パターンに一致する箇所はありませんでした({len(files)} ファイルを確認)"
    prefix = "[プレビューのみ・未保存] " if dry else ""
    return f"{prefix}置換結果({total_hits} 箇所 / {len(changed_files)} ファイル):\n" + "\n".join(f"- {x}" for x in changed_files)


def archive_tool(ctx: ToolContext, action: str, path: str, to: str = "") -> str:
    act = (action or "").strip().lower()
    if act not in ("pack", "unpack", "list"):
        raise ToolError("action は pack / unpack / list のいずれかを指定してください")
    src = _resolve(ctx, path)
    if act == "list":
        if not src.is_file():
            raise ToolError(f"アーカイブが見つかりません: {path}")
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src, "r") as z:
                names = z.namelist()
        elif src.name.lower().endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(src, "r:*") as tf:
                names = tf.getnames()
        else:
            raise ToolError("対応形式は .zip / .tar.gz / .tgz です")
        return "\n".join(names[:200]) or "(空のアーカイブ)"

    if act == "unpack":
        if not src.is_file():
            raise ToolError(f"アーカイブが見つかりません: {path}")
        dest_dir = _resolve(ctx, to) if to.strip() else src.parent / src.stem.removesuffix(".tar")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_root = dest_dir.resolve()
        extracted = 0
        def _safe_member(rel_name: str) -> Path:
            cand = (dest_root / rel_name).resolve()
            if dest_root not in cand.parents and cand != dest_root:
                raise ToolError(f"アーカイブ内の不正なパスを拒否しました: {rel_name}")
            return cand
        if src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src, "r") as z:
                for member in z.infolist():
                    if member.is_dir():
                        continue
                    out_p = _safe_member(member.filename)
                    out_p.parent.mkdir(parents=True, exist_ok=True)
                    out_p.write_bytes(z.read(member.filename))
                    extracted += 1
        elif src.name.lower().endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(src, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    out_p = _safe_member(member.name)
                    out_p.parent.mkdir(parents=True, exist_ok=True)
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        out_p.write_bytes(fobj.read())
                        extracted += 1
        else:
            raise ToolError("対応形式は .zip / .tar.gz / .tgz です")
        return f"展開しました: {_relative(ctx, dest_dir)} ({extracted} ファイル)"

    # pack
    if not src.exists():
        raise ToolError(f"対象が見つかりません: {path}")
    out_arc = _resolve(ctx, to) if to.strip() else src.with_suffix(".zip")
    if out_arc.is_dir():
        out_arc = out_arc / f"{src.name}.zip"
    _backup(ctx, out_arc)
    out_arc.parent.mkdir(parents=True, exist_ok=True)
    items = [src] if src.is_file() else [p for p in sorted(src.rglob("*")) if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(src).parts)]
    if out_arc.name.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(out_arc, "w:gz") as tf:
            for item in items:
                arcname = item.name if src.is_file() else item.relative_to(src).as_posix()
                tf.add(item, arcname=arcname)
    else:
        with zipfile.ZipFile(out_arc, "w", zipfile.ZIP_DEFLATED) as z:
            for item in items:
                arcname = item.name if src.is_file() else item.relative_to(src).as_posix()
                z.write(item, arcname=arcname)
    return f"作成しました: {_relative(ctx, out_arc)} ({len(items)} ファイル)"


def _make_word(ctx: ToolContext, path: str, content: str, title: str = "", mode: str = "overwrite") -> str:
    if (mode or "").strip().lower() == "append":
        return _office(ctx, path, ".docx", lambda t: office.append_docx(t, content))
    return _office(ctx, path, ".docx", lambda t: office.write_docx(t, content, title or None))


def _make_slides(ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> str:
    if (mode or "").strip().lower() == "append":
        return _office(ctx, path, ".pptx", lambda t: office.append_pptx(t, content))
    return _office(ctx, path, ".pptx", lambda t: office.write_pptx(t, content))


def _make_chart(ctx: ToolContext, path: str, data: str, kind: str = "bar", title: str = "") -> str:
    target = _resolve(ctx, path)
    if target.suffix.lower() not in (".svg", ".html"):
        target = _resolve(ctx, _relative(ctx, target.with_suffix(".html")) if target.suffix else path + ".html")
    _backup(ctx, target)
    try:
        office.render_chart(target, data, kind=kind or "bar", title=title)
    except (ValueError, TypeError) as e:
        raise ToolError(f"グラフを作れませんでした: {e}") from e
    return f"作成しました: {_relative(ctx, target)} ({target.stat().st_size} bytes)"


def _news_tool(ctx: ToolContext, topic: str = "", lang: str = "ja", limit: str = "8") -> str:
    n = max(1, min(20, int(_number(limit or "8", "limit"))))
    source, items = web.news(lang or "ja", limit=n, topic=(topic or "").strip())
    if not items:
        return f"ニュース({source})に該当する記事はありませんでした"
    lines = [f"[{source} ニュース ({len(items)} 件)]"]
    for i, item in enumerate(items, 1):
        meta = " / ".join(x for x in (item.get("outlet", ""), item.get("date", "")[:16]) if x)
        lines.append(f"{i}. {item['title']}" + (f" ({meta})" if meta else "") + f"\n   URL: {item['url']}")
        if item.get("summary"):
            lines.append(f"   要約: {item['summary']}")
    return "\n".join(lines)


def _http_request(ctx: ToolContext, url: str, method: str = "GET", headers: str = "", body: str = "") -> str:
    hdrs: dict[str, str] | None = None
    if headers.strip():
        try:
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                raise ValueError("headers must be a JSON object")
            hdrs = {str(k): str(v) for k, v in parsed.items()}
        except ValueError as e:
            raise ToolError(f"headers は JSON オブジェクト形式で指定してください: {e}") from e
    return web.request(url, method=method or "GET", headers=hdrs, body=body)


def _kb_search(ctx: ToolContext, query: str, top_k: str = "5") -> str:
    if ctx.kb is None:
        raise ToolError("ナレッジベースが利用できません")
    k = max(1, min(15, int(_number(top_k or "5", "top_k"))))
    hits = ctx.kb.search(query, top_k=k)
    if not hits:
        return f"知識ベースに「{query}」と一致する箇所は見つかりませんでした"
    return "\n\n".join(f"[{i}] {h['title']} (関連度 {h['score']:.2f} / {h['source']})\n{h['text']}"
                       for i, h in enumerate(hits, 1))


def _kb_add(ctx: ToolContext, title: str, content: str = "", path: str = "", url: str = "", tags: str = "") -> str:
    if ctx.kb is None:
        raise ToolError("ナレッジベースが利用できません")
    if path.strip():
        target = _resolve(ctx, path.strip())
        res = ctx.kb.add_file(target, tags=tags) if target.is_file() else ctx.kb.add_folder(target, tags=tags)
        return f"知識ベースに登録しました: {json.dumps(res, ensure_ascii=False)}"
    if url.strip():
        if not ctx.allow_net:
            raise ToolError("ネットの利用が許可されていないため URL は登録できません")
        res = ctx.kb.add_url(url.strip(), title=title, tags=tags)
        return f"URL を知識ベースに登録しました: {res['title']} ({res['chunks']} チャンク)"
    if not content.strip():
        raise ToolError("content、path、url のいずれかを指定してください")
    res = ctx.kb.add_note(title, content, tags=tags)
    return f"知識ベースに保存しました: {res['title']} ({res['chunks']} チャンク)"


def _save_memory(ctx: ToolContext, action: str, fact: str = "") -> str:
    if ctx.memorize is None:
        raise ToolError("メモリ機能は現在利用できません")
    return ctx.memorize(action, fact)


def _check_single_file(path: Path) -> list[str]:
    suf = path.suffix.lower()
    raw = path.read_text(encoding="utf-8", errors="replace")
    issues: list[str] = []
    if suf == ".py":
        try:
            ast.parse(raw, filename=path.name)
        except SyntaxError as e:
            return [f"構文エラー (L{e.lineno or '?'}): {e.msg}"]
        for msg in (duplicate_routes(raw), markup_in_python(raw), undefined_names(raw), used_before_defined(raw)):
            if msg:
                issues.append(msg)
    elif suf in (".js", ".mjs"):
        msg = js_syntax_error(raw)
        if msg:
            issues.append(msg)
    elif suf in (".html", ".htm"):
        msg = script_problem(raw, path.name)
        if msg:
            issues.append(msg)
    elif suf == ".json":
        try:
            json.loads(raw)
        except ValueError as e:
            issues.append(f"JSON エラー: {e}")
    elif suf in (".yaml", ".yml"):
        try:
            import yaml
            yaml.safe_load(raw)
        except Exception as e:
            issues.append(f"YAML エラー: {e}")
    elif suf == ".toml":
        import tomllib
        try:
            tomllib.loads(raw)
        except Exception as e:
            issues.append(f"TOML エラー: {e}")
    return issues


def _code_check(ctx: ToolContext, path: str = ".") -> str:
    target = _resolve(ctx, path or ".")
    if not target.exists():
        raise ToolError(f"対象が見つかりません: {path}")
    exts = {".py", ".js", ".mjs", ".html", ".htm", ".json", ".yaml", ".yml", ".toml"}
    if target.is_file():
        files = [target]
    else:
        files = [p for p in sorted(target.rglob("*"))
                 if p.is_file() and p.suffix.lower() in exts
                 and not any(part in _SKIP_DIRS for part in p.relative_to(target).parts)][:120]
    if not files:
        return "検証対象のコード/設定ファイルが見つかりませんでした"
    report = []
    for f in files:
        errs = _check_single_file(f)
        if errs:
            rel = _relative(ctx, f)
            report.append(f"- {rel}: " + " / ".join(errs))
    if not report:
        return f"問題は見つかりませんでした ({len(files)} ファイル確認)"
    return f"検出された問題 ({len(report)}/{len(files)} ファイル):\n" + "\n".join(report)


def _strip_diff_path(raw: str) -> str:
    p = raw.strip().split("\t", 1)[0].strip()
    if (p.startswith("a/") or p.startswith("b/")) and not p.startswith(("./", "../")):
        return p[2:]
    return p


def _parse_unified_diff(patch: str) -> list[tuple[str, str, list[tuple[int, list[str]]]]]:
    lines = patch.replace("\r\n", "\n").splitlines()
    files: list[tuple[str, str, list[tuple[int, list[str]]]]] = []
    old_p, new_p = "", ""
    hunks: list[tuple[int, list[str]]] = []
    cur_start: int | None = None
    cur_lines: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if hunks or (old_p and new_p):
                if cur_start is not None:
                    hunks.append((cur_start, cur_lines))
                    cur_start, cur_lines = None, []
                files.append((old_p, new_p, hunks))
                hunks = []
            old_p = _strip_diff_path(lines[i][4:])
            new_p = _strip_diff_path(lines[i + 1][4:])
            i += 2
            continue
        m = re.match(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", ln)
        if m:
            if cur_start is not None:
                hunks.append((cur_start, cur_lines))
            cur_start = int(m.group(1))
            cur_lines = []
        elif cur_start is not None:
            if ln.startswith((" ", "+", "-")):
                cur_lines.append(ln)
            elif ln.startswith("\\ No newline"):
                pass
            elif ln == "":
                cur_lines.append(" ")
        i += 1
    if cur_start is not None:
        hunks.append((cur_start, cur_lines))
    if old_p or new_p or hunks:
        files.append((old_p, new_p, hunks))
    return files


def _apply_hunks(src_lines: list[str], hunks: list[tuple[int, list[str]]]) -> list[str]:
    out = list(src_lines)
    offset = 0
    for old_start, hlines in hunks:
        expected = [ln[1:] for ln in hlines if ln[0] in (" ", "-")]
        replaced = [ln[1:] for ln in hlines if ln[0] in (" ", "+")]
        if not expected:
            pos = max(0, min(len(out), old_start - 1 + offset))
        else:
            guess = max(0, old_start - 1 + offset)
            pos = -1
            for delta in [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5]:
                cand = guess + delta
                if 0 <= cand and cand + len(expected) <= len(out) and out[cand:cand + len(expected)] == expected:
                    pos = cand
                    break
            if pos < 0:
                raise ToolError(f"パッチの該当箇所が見つかりません (行 {old_start} 付近)")
        out[pos:pos + len(expected)] = replaced
        offset += len(replaced) - len(expected)
    return out


def _apply_patch(ctx: ToolContext, patch: str) -> str:
    entries = _parse_unified_diff(patch or "")
    if not entries:
        raise ToolError("有効な unified diff (--- / +++ / @@) が見つかりませんでした")
    planned: list[tuple[str, Path, bytes | None, bool]] = []
    for old_p, new_p, hunks in entries:
        is_delete = new_p == "/dev/null"
        is_create = old_p == "/dev/null"
        rel_arg = old_p if is_delete else (new_p or old_p)
        if not rel_arg or rel_arg == "/dev/null":
            raise ToolError("パッチ内に対象ファイル名がありません")
        target = _resolve(ctx, rel_arg)
        if is_delete:
            if not target.is_file():
                raise ToolError(f"削除対象がありません: {rel_arg}")
            planned.append(("delete", target, None, True))
            continue
        if target.is_file() and not is_create:
            newline = _newline_of(target)
            raw_text = target.read_text(encoding="utf-8", errors="replace")
            had_trailing_nl = raw_text.endswith("\n")
            base_lines = raw_text.splitlines()
            new_lines = _apply_hunks(base_lines, hunks)
            joined = "\n".join(new_lines) + ("\n" if (had_trailing_nl or (not base_lines and new_lines)) else "")
            planned.append(("modify", target, _encode(joined, newline), False))
        else:
            new_lines = [ln[1:] for _, hl in hunks for ln in hl if ln.startswith("+")]
            if not new_lines and not hunks:
                raise ToolError(f"変更ハンクが空です: {rel_arg}")
            joined = "\n".join(new_lines) + ("\n" if new_lines else "")
            planned.append(("create", target, _encode(joined, os.linesep), False))

    summaries = []
    for kind, target, payload, _ in planned:
        rel = _relative(ctx, target)
        if ctx.changes is not None and rel not in ctx.changes:
            ctx.changes[rel] = _snapshot(ctx, rel)
        _backup(ctx, target)
        if kind == "delete":
            target.unlink()
            summaries.append(f"削除: {rel}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            assert payload is not None
            target.write_bytes(payload)
            summaries.append(f"{'作成' if kind == 'create' else '更新'}: {rel}")
    return "パッチを適用しました:\n" + "\n".join(f"- {s}" for s in summaries)


def _convert_file(ctx: ToolContext, path: str, to: str) -> str:
    src = _resolve(ctx, path)
    if not src.is_file():
        raise ToolError(f"変換元ファイルがありません: {path}")
    dest = _resolve(ctx, to)
    _backup(ctx, dest)
    try:
        out = office.convert_file(src, dest)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise ToolError(f"変換できませんでした: {e}") from e
    return f"変換しました: {_relative(ctx, src)} → {_relative(ctx, out)} ({out.stat().st_size} bytes)"


def _image_edit(ctx: ToolContext, action: str, path: str, to: str = "", width: str = "", height: str = "",
                box: str = "", angle: str = "", text: str = "", extras: str = "") -> str:
    src = _resolve(ctx, path)
    if not src.is_file():
        raise ToolError(f"画像ファイルがありません: {path}")
    act = (action or "").strip().lower()
    if act == "info":
        try:
            return media.image_info(src)
        except Exception as e:
            raise ToolError(f"画像情報を読めませんでした: {e}") from e
    dest = _resolve(ctx, to) if to.strip() else src
    _backup(ctx, dest)
    w = int(_number(width, "width")) if width.strip() else None
    h = int(_number(height, "height")) if height.strip() else None
    ang = float(_number(angle, "angle")) if angle.strip() else 0.0
    crop_box = None
    if box.strip():
        parts = [p.strip() for p in re.split(r"[,\s]+", box.strip()) if p.strip()]
        if len(parts) != 4:
            raise ToolError("box は left,top,right,bottom の4数値で指定してください")
        crop_box = tuple(int(float(x)) for x in parts)
    extra_paths: list[Path] = []
    if extras.strip():
        raw_items = json.loads(extras) if extras.strip().startswith("[") else [x.strip() for x in extras.split(",") if x.strip()]
        extra_paths = [_resolve(ctx, str(x)) for x in raw_items]
    try:
        return media.image_edit(src, dest, act, width=w, height=h, box=crop_box, angle=ang, text=text, extras=extra_paths)
    except (ValueError, OSError) as e:
        raise ToolError(f"画像を処理できませんでした: {e}") from e


def _qr_barcode(ctx: ToolContext, text: str, path: str, format: str = "qr", scale: str = "8") -> str:
    dest = _resolve(ctx, path)
    _backup(ctx, dest)
    sc = int(_number(scale or "8", "scale"))
    try:
        return media.generate_barcode(text, dest, fmt=format or "qr", scale=sc)
    except (ValueError, OSError) as e:
        raise ToolError(f"生成できませんでした: {e}") from e


def _scan_barcode(ctx: ToolContext, path: str) -> str:
    src = _resolve(ctx, path)
    if not src.is_file():
        raise ToolError(f"画像ファイルがありません: {path}")
    try:
        return media.scan_barcode(src)
    except (ValueError, OSError) as e:
        raise ToolError(f"読み取れませんでした: {e}") from e


def _web_search(ctx: ToolContext, query: str, lang: str = "ja", limit: str = "8") -> str:
    n = max(1, min(15, int(_number(limit or "8", "limit"))))
    text, _ = web.search(query, lang=lang or "ja", limit=n)
    return text


def _web_extract(ctx: ToolContext, url: str, mode: str = "links", limit: str = "40") -> str:
    n = max(1, min(100, int(_number(limit or "40", "limit"))))
    return web.extract_page(url, mode=mode or "links", limit=n)


def _download_file(ctx: ToolContext, url: str, path: str) -> str:
    if ctx.workspace is None or not ctx.allow_write:
        raise ToolError("ダウンロードを保存するにはファイルの変更権限が必要です")
    dest = _resolve(ctx, path)
    _backup(ctx, dest)
    nbytes, ctype = web.download_file(url, dest)
    return f"保存しました: {_relative(ctx, dest)} ({nbytes} bytes" + (f", {ctype}" if ctype else "") + ")"


def _speak_tool(ctx: ToolContext, text: str, path: str = "", rate: str = "0") -> str:
    r = int(_number(rate or "0", "rate"))
    if path.strip():
        if ctx.workspace is None or not ctx.allow_write:
            raise ToolError("音声ファイルを保存するにはファイルの変更権限が必要です")
        dest = _resolve(ctx, path.strip())
        _backup(ctx, dest)
        return pc.speak(text, save_wav=dest, rate=r)
    return pc.speak(text, rate=r)


def _notify_pc(ctx: ToolContext, title: str, text: str) -> str:
    return pc.notify_desktop(title, text)


def _network_info(ctx: ToolContext, host: str = "", port: str = "") -> str:
    p = int(_number(port, "port")) if port.strip() else None
    return pc.network_info(host.strip() or None, p)


_HANDLERS: dict[str, Callable[..., str]] = {
    "calc": lambda ctx, expression: calc(expression),
    "delegate": delegate,
    "list_files": list_files, "tree": tree, "find_files": find_files, "read_file": read_file, "search_files": search_files,
    "csv_query": csv_query, "sqlite_query": sqlite_query, "git_info": git_info,
    "code_check": _code_check, "scan_barcode": _scan_barcode,
    "write_file": write_file, "write_code": write_code, "edit_file": edit_file, "apply_patch": _apply_patch,
    "append_file": append_file, "make_folder": make_folder,
    "move_file": lambda ctx, path, to: _transfer(ctx, path, to, True),
    "copy_file": lambda ctx, path, to: _transfer(ctx, path, to, False),
    "delete_file": delete_file, "restore_file": restore_file,
    "regex_replace": regex_replace, "archive": archive_tool,
    "make_word": _make_word,
    "make_excel": lambda ctx, path, data: _make_excel(ctx, path, data),
    "make_slides": _make_slides,
    "make_chart": _make_chart,
    "convert_file": _convert_file,
    "image_edit": _image_edit,
    "qr_barcode": _qr_barcode,
    "run_python": run_python, "run_file": run_file, "run_command": run_command, "start_process": start_process,
    "process_output": process_output, "stop_process": stop_process, "check_url": check_url,
    "system_info": lambda ctx: pc.system_info(),
    "list_windows": lambda ctx: "\n".join(title for _, title in pc.list_windows()) or "(ウィンドウなし)",
    "list_processes": lambda ctx: pc.list_processes(),
    "screenshot": _screenshot,
    "clipboard_read": lambda ctx: pc.clipboard_read() or "(クリップボードは空、または文字ではありません)",
    "clipboard_write": lambda ctx, text: pc.clipboard_write(text),
    "open": lambda ctx, target: _open(ctx, target),
    "focus_window": lambda ctx, title: pc.focus_window(title),
    "close_window": lambda ctx, title: pc.close_window(title),
    "type_text": lambda ctx, text, window=None: pc.type_text(text, window),
    "press_keys": lambda ctx, keys, window=None: pc.press_keys(keys, window),
    "media": lambda ctx, action: pc.media(action),
    "speak": _speak_tool,
    "notify_pc": _notify_pc,
    "network_info": _network_info,
    "click": lambda ctx, x, y, button="left": pc.click(int(_number(x, "x")), int(_number(y, "y")), button),
    "scroll": lambda ctx, amount: pc.scroll(int(_number(amount, "amount"))),
    "volume": lambda ctx, action: pc.volume(action),
    "wait": lambda ctx, seconds: pc.wait(_number(seconds, "seconds")),
    "web_search": _web_search,
    "web_extract": _web_extract,
    "download_file": _download_file,
    "wikipedia": lambda ctx, query, lang="ja": web.wikipedia(query, lang),
    "news": _news_tool,
    "fetch_url": lambda ctx, url: web.fetch_url(url),
    "http_request": _http_request,
    "kb_search": _kb_search,
    "kb_add": _kb_add,
    "save_memory": _save_memory,
}
_MAY_BE_EMPTY = {"content", "new", "replacement", "sql"}  # 空文字に意味がある引数(空のファイルを作る、文字列を消す)

# ファイルを変える道具と、変わるファイルを指す引数(変更の記録・差分の表示に使う)
_CHANGING = {"write_file": ("path",), "write_code": ("path",), "edit_file": ("path",), "append_file": ("path",), "delete_file": ("path",),
             "restore_file": ("path",), "move_file": ("path", "to"), "copy_file": ("to",),
             "regex_replace": ("path",), "archive": ("to",),
             "make_word": ("path",), "make_excel": ("path",), "make_slides": ("path",), "make_chart": ("path",),
             "convert_file": ("to",), "image_edit": ("to", "path"), "qr_barcode": ("path",),
             "download_file": ("path",), "speak": ("path",)}
_MISSING = "\x00missing"  # 変える前に無かったファイルの印


def _snapshot(ctx: ToolContext, rel: str) -> str | None:
    """今の内容(文字のファイルなら文字。無ければ _MISSING、文字でなければ None)。"""
    path = ctx.workspace.resolve() / rel
    if not path.is_file():
        return _MISSING
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > 2_000_000 or b"\x00" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def summarize_changes(ctx: ToolContext, limit: int = 6000) -> list[dict]:
    """記録した変更を [{path, status(added/modified/deleted), added, removed, diff}] にする(Claude Code のような差分の表示用)。"""
    out = []
    for rel, before in (ctx.changes or {}).items():
        after = _snapshot(ctx, rel)
        if before == after:
            continue
        status = "added" if before == _MISSING else "deleted" if after == _MISSING else "modified"
        old = "" if before in (_MISSING, None) else before
        new = "" if after in (_MISSING, None) else after
        binary = (before is None and status != "added") or (after is None and status != "deleted")
        diff = [] if binary else list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="", n=2))
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        text = "\n".join(diff[2:])  # 先頭の --- +++ の2行は省く
        out.append({"path": rel, "status": status, "added": added, "removed": removed, "binary": binary,
                    "diff": text if len(text) <= limit else text[:limit] + "\n…(長いため省略)"})
    return sorted(out, key=lambda c: c["path"])


def summarize_call(name: str, args: dict) -> str:
    """画面に出す1行の要約(何に対する操作か)。"""
    if name == "delegate":
        return f"{args.get('role', '')}: {str(args.get('task', '')).strip().splitlines()[0][:70] if args.get('task') else ''}"
    for key in ("path", "target", "title", "url", "query", "expression", "command", "keys", "pattern", "text", "code", "action", "question"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0][:80]
            if key == "path" and isinstance(args.get("to"), str):
                return f"{first} → {args['to'][:60]}"
            return first
    if "x" in args and "y" in args:
        return f"({args['x']}, {args['y']})"
    return ""


def _preview(name: str, args: dict, ctx: ToolContext | None = None) -> str:
    """確認のときに見せる中身(書き込む内容、実行するコードなど)。"""
    if name == "run_file" and ctx is not None:
        head = f"{args.get('path', '')} {args.get('args', '')}".strip()
        return (head + "\n---\n" + file_text(ctx, str(args.get("path", "")), 3000)).strip()
    if name in ("write_file", "append_file", "make_word", "make_slides"):
        return str(args.get("content", ""))[:3000]
    if name in ("make_excel", "make_chart"):
        return str(args.get("data", ""))[:3000]
    if name == "regex_replace":
        return f"pattern: {args.get('pattern', '')}\nreplacement: {args.get('replacement', '')}"
    if name == "http_request":
        return f"{args.get('method', 'GET')} {args.get('url', '')}\n{str(args.get('body', ''))[:1500]}".strip()
    if name == "write_code":
        return "AIが次の指示で中身を書きます:\n" + str(args.get("instructions", ""))[:3000]
    if name == "edit_file":
        return f"--- 変更前\n{str(args.get('old', ''))[:1500]}\n+++ 変更後\n{str(args.get('new', ''))[:1500]}"
    if name == "apply_patch":
        return str(args.get("patch", ""))[:3000]
    if name in ("convert_file", "download_file"):
        return f"{args.get('path') or args.get('url', '')} → {args.get('to') or args.get('path', '')}"
    if name == "qr_barcode":
        return f"[{args.get('format', 'qr')}] {str(args.get('text', ''))[:500]}"
    for key in ("code", "command", "text", "keys", "target"):
        if isinstance(args.get(key), str):
            return args[key][:3000]
    return ""


def _require_read(ctx: ToolContext, path: str) -> None:
    """前からあるファイルを、この依頼で読まないまま丸ごと書き直そうとしたら止める(Claude Code と同じ決まり)。

    gemma3:4b が、calc.py も、それを使う test_calc.py も読まずに calc.py を書き直し、テストが使う mul を消していた(実際に
    あった)。この依頼で作った・変えたファイル、空のファイル、Word などの文書は除く。
    """
    try:
        target = _resolve(ctx, path)
    except ToolError:
        return
    if not target.is_file() or target.suffix.lower() in office.OFFICE_SUFFIXES:
        return
    rel = _relative(ctx, target)
    if rel in (ctx.changes or {}) or rel in ctx.read:
        return
    try:
        if not _read_text(target, 400_000).strip():
            return
    except ToolError:
        return
    try:
        made_now = bool(ctx.started) and target.stat().st_mtime >= ctx.started
    except OSError:
        made_now = False
    if made_now:  # この依頼で動かしたプログラムが作った・変えたファイル(素数を書き出した primes.txt など)
        raise ToolError(f"{rel} はもうあります(この依頼で動かしたプログラムが作ったか、変えたファイルです)。読まずに書き直すと、"
                        "その中身が消えます。read_file で中身を確かめてください(書き直すときも、読んでから)。(The file already "
                        "exists: a program you ran made it. Read it with read_file before you change it.)")
    raise ToolError(f"{rel} は依頼の前からあるファイルで、この依頼ではまだ読んでいません。読まずに丸ごと書き直すと、今の中身(ほかの"
                    "ファイルが使う関数など)が消えます。read_file で読んでから、直す所だけを edit_file で直してください(書き"
                    "直すときも、読んでから)。(Read it first with read_file, then change only what is needed with edit_file.)")


def execute(ctx: ToolContext, name: str, args: dict) -> str:
    """道具を1回使う。結果を文字列で返す。使えない・拒否された・失敗したときは ToolError。"""
    tool = TOOLS.get(name)
    if tool is None or tool not in available_tools(ctx):
        raise ToolError(f"使えない道具です: {name}")
    values = {}
    for key in tool.required + tool.optional:
        value = args.get(key)
        if value is None or value == "":
            if key in tool.required:
                if key not in _MAY_BE_EMPTY:
                    raise ToolError(f"引数 {key} が必要です")
                values[key] = ""
            continue
        values[key] = (
            value if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
            else str(value)
        )

    if name in ("run_command", "start_process"):
        _refuse_command(ctx, values.get("command", ""))
    level, reason = guard.OK, ""
    if ctx.guard and name in ("run_command", "start_process"):
        level, reason = guard.check_command(values.get("command", ""), workspace=ctx.workspace, allow_net=ctx.allow_net)
    elif ctx.guard and name == "run_python":
        level, reason = guard.check_python(values.get("code", ""), workspace=ctx.workspace, allow_net=ctx.allow_net)
    elif ctx.guard and name == "run_file":
        body = file_text(ctx, values.get("path", ""))
        kind = Path(values.get("path", "")).suffix.lower()
        if kind in (".py", ".pyw"):
            level, reason = guard.check_python(body, workspace=ctx.workspace, allow_net=ctx.allow_net)
        elif body:
            level, reason = guard.check_command(body, workspace=ctx.workspace, allow_net=ctx.allow_net)
            if kind not in (".ps1", ".bat", ".cmd", ".sh") and level == guard.ASK:
                level, reason = guard.OK, ""  # JavaScript などは、取り返しのつかない操作だけを止める(比較の > などを書き込みと取り違えない)
        if level == guard.OK and values.get("args"):
            level, reason = guard.check_command(values["args"], workspace=ctx.workspace, allow_net=ctx.allow_net)
    if level == guard.BLOCK:
        raise ToolError(f"安全のため実行しませんでした({reason}は行いません)。別の方法を取るか、できなかったと答えてください")
    needs_ok = tool.risky and (ctx.confirm or (ctx.confirm_commands and tool.group in ("run", "pc")))
    if needs_ok or level == guard.ASK:
        if ctx.approve is None:
            why = f"({reason})" if reason else ""
            raise ToolError(f"この操作{why}には利用者の確認が必要ですが、確認を取る手段がないため実行しませんでした")
        request = {"tool": name, "label": tool.label, "summary": summarize_call(name, values), "preview": _preview(name, values, ctx)}
        if reason:
            request["reason"] = reason
        if not ctx.approve(request):
            raise ToolError("利用者がこの操作を許可しませんでした。別の方法を取るか、できなかったと答えてください")

    if ctx.by_agent and name in ("write_file", "write_code") and values.get("path"):
        _require_read(ctx, values["path"])
    many = name in ("move_file", "copy_file") and bool(_PATTERN.search(values.get("path", ""))  # まとめての移動は中で記録する
                                                        or _listed_files(ctx, values.get("path", "")))
    if ctx.changes is not None and name in _CHANGING and not many:
        for key in _CHANGING[name]:  # 変える前の内容を覚える(同じファイルを何度変えても、最初の内容と比べる)
            if values.get(key):
                path = values[key]
                suffix = {"make_word": ".docx", "make_excel": ".xlsx", "make_slides": ".pptx"}.get(name)
                if suffix and not path.lower().endswith(suffix):  # 文書の道具は拡張子を自動で付ける
                    path = str(Path(path).with_suffix(suffix)) if Path(path).suffix else path + suffix
                try:
                    rel = _relative(ctx, _resolve(ctx, path))
                except ToolError:
                    continue
                if rel not in ctx.changes:
                    ctx.changes[rel] = _snapshot(ctx, rel)
    try:
        result = _HANDLERS[name](ctx, **values)
    except (pc.PCError, web.WebError) as e:
        raise ToolError(str(e)) from e
    except OSError as e:
        raise ToolError(f"操作できませんでした: {e}") from e
    result = result if isinstance(result, str) else str(result)
    if tool.group == "pc" and name not in _NOT_OPERATING and not (name == "open" and (
            NOT_OPENED in result or result.startswith("アプリでは開いていません"))):
        ctx.operated = True  # アプリを開いた・入力した など: このあとは、画面を撮って確かめてよい
    return result if len(result) <= MAX_OUTPUT else result[:MAX_OUTPUT] + "\n... (長いため省略)"


# ---- 実行記録との照合 -------------------------------------------------------

_CHECKS = [
    # (やったことになる道具, 依頼の言い回し, 回答の「やりました」, 訂正文)
    ({"write_file", "write_code", "edit_file", "apply_patch", "append_file", "make_folder", "move_file", "copy_file",
      "delete_file", "restore_file", "make_word", "make_excel", "make_slides", "make_chart", "convert_file",
      "image_edit", "qr_barcode", "download_file", "regex_replace", "archive"},
     re.compile(r"保存|作成|作って|書き込|書いて|書き出|出力して|編集|修正して|直して|変更|削除|消して|移動|コピー|create|save|write|edit|delete|move", re.I),
     re.compile(r"(作成|保存|書き込み|書き出し|出力|編集|修正|変更|削除|移動|コピー)(しました|済み|完了)|書き込んで|(created|saved|written|wrote|edited|updated|deleted|moved)\b", re.I),
     "ファイルの作成・変更は行われていません"),
    ({"run_python", "run_file", "run_command", "start_process"},
     re.compile(r"実行|動かして|走らせ|テスト|run|execute", re.I),
     re.compile(r"実行(しました|した結果|結果)|(executed|ran)\b", re.I),
     "コードやコマンドは実行されていません"),
    ({"open", "start_process", "run_file", "run_command", "run_python"},
     re.compile(r"起動|立ち上げ|launch", re.I),
     re.compile(r"(起動し|立ち上げ)ました|launched\b", re.I),
     "プログラムの起動は行われていません"),
    ({"open", "type_text", "press_keys", "click", "close_window", "focus_window", "clipboard_write", "volume", "media", "run_command"},
     re.compile(r"開いて|入力して|打って|クリック|押して|閉じて|open|type|click", re.I),
     re.compile(r"(開き|入力し|クリックし|押し|閉じ)ました|(opened|typed|clicked|closed)\b", re.I),
     "PCの操作は行われていません"),
]


def fact_check(task: str, answer: str, calls: list[dict], ctx: ToolContext) -> str | None:
    """回答が「やっていないこと」をやったと述べていたら、実行記録に基づく訂正文を返す。

    小さなモデルは、道具を使わずに「ファイルを作成しました」と答えることがある。
    実際に何をしたかは記録で確定できるので、AIの申告ではなく記録を正とする。
    """
    done = {c["name"] for c in calls if c["ok"]}
    notes = [fact for tools, ask, claim, fact in _CHECKS if not (tools & done) and ask.search(task) and claim.search(answer)]
    if not notes:
        return None
    return ("【実行記録による訂正】" + "。".join(notes) + "(許可されていない、道具が使われなかった、または操作が失敗したため)。"
            "下の回答のうち、該当する記述は事実ではありません。")
