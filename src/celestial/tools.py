"""ワーカーが使える道具(ツール)。

安全のための決まり:
- ファイルを扱う道具は、設定した作業フォルダの中だけに届く(外へ出るパスは拒否する)。
- ファイルを書き換える・消す前に、必ず元のファイルをバックアップへ写す(restore_file で戻せる)。
- 書き込み・実行・PC操作・ネットは、それぞれ設定で明示的に許可されたときだけ使える。
- 影響のある操作(risky)は、実行の直前に利用者へ内容を見せて確認を取る(設定で省略できる)。
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import office, pc, web

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
    approve: Callable[[dict], bool] | None = None
    describe_image: Callable[[Path, str], str] | None = None  # 画像を見て説明するAI(スクリーンショット用)
    delegate: Callable[[str, str], str] | None = None  # 専門のワーカーに仕事を頼む (役割, 依頼) → 回答
    scratch_dir: Path | None = None  # スクリーンショットなどの置き場
    changes: dict | None = None  # 変えたファイルの記録 {作業フォルダからの位置: 最初の内容(新しく作ったなら None)}。None なら記録しない


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
    Tool("calc", "Evaluate an arithmetic expression exactly. args: expression (e.g. \"17*23\", \"sqrt(2)*100\").", "計算", ("expression",)),
    Tool("delegate", "Ask a specialist AI worker to do one self-contained piece of work and get its answer. "
                     "args: role (general / code / reason / summarize / translate / write / extract), task (the full request, including any text to process).",
         "ワーカーに依頼", ("role", "task"), (), "team"),
    # ---- ファイル(読み取り) ----
    Tool("list_files", "List files and folders in the workspace. args: path (folder, default \".\").", "ファイル一覧", (), ("path",), "files"),
    Tool("find_files", "Find files by name pattern anywhere in the workspace. args: pattern (e.g. \"*.py\", \"*report*\").", "ファイルを探す", ("pattern",), (), "files"),
    Tool("read_file", "Read a text file in the workspace. args: path, start (first line number, default \"1\").", "ファイルを読む", ("path",), ("start",), "files"),
    Tool("search_files", "Find lines containing a text in workspace files. args: query, path (folder, default \".\").", "ファイル内を検索", ("query",), ("path",), "files"),
    # ---- ファイル(変更) ----
    Tool("write_file", "Create a text file, or replace the whole content of an existing one. args: path, content.", "ファイルを書く", ("path", "content"), (), "write", True),
    Tool("edit_file", "Replace one exact piece of text in a file. args: path, old (exact existing text, must appear once), new.", "ファイルを編集", ("path", "old", "new"), (), "write", True),
    Tool("append_file", "Add text to the end of a file (creates it if missing). args: path, content.", "ファイルに追記", ("path", "content"), (), "write", True),
    Tool("make_folder", "Create a folder in the workspace. args: path.", "フォルダを作成", ("path",), (), "write"),
    Tool("move_file", "Move or rename a file inside the workspace. args: path, to.", "ファイルを移動", ("path", "to"), (), "write", True),
    Tool("copy_file", "Copy a file inside the workspace. args: path, to.", "ファイルをコピー", ("path", "to"), (), "write", True),
    Tool("delete_file", "Delete a file (a backup is kept). args: path.", "ファイルを削除", ("path",), (), "write", True),
    Tool("restore_file", "Undo: bring back the most recent backup of a file. args: path.", "ファイルを元に戻す", ("path",), (), "write", True),
    # ---- 文書(Word・Excel・PowerPoint) ----
    Tool("make_word", "Create a Word document (.docx). args: path, content (Markdown: # headings, - bullets, 1. numbered lists, "
                      "| tables |, **bold**, ``` code blocks, --- for a page break), title (optional document title).",
         "Word 文書を作成", ("path", "content"), ("title",), "write", True),
    Tool("make_excel", "Create an Excel workbook (.xlsx). args: path, data (CSV text, a Markdown table, or JSON: a list of rows, "
                       "a list of objects, or {\"sheet name\": rows}). The first row is the header (row 1), data starts at row 2. "
                       "Write totals and calculations as Excel formulas starting with = instead of computing them yourself. "
                       "=SUM(above) (also AVERAGE/MAX/MIN/COUNT) means all data rows above in the same column, "
                       "e.g. CSV data \"月,売上\\n4月,1200\\n5月,1350\\n6月,1280\\n合計,=SUM(above)\". Numbers like 1,200 and 15% are stored as numbers.",
         "Excel ブックを作成", ("path", "data"), (), "write", True),
    Tool("make_slides", "Create a PowerPoint presentation (.pptx). args: path, content (Markdown: each '# ' heading starts a new slide, "
                        "'- ' lines are bullet points, indent two spaces for sub-points; the first slide is the title slide and its lines are the subtitle).",
         "PowerPoint を作成", ("path", "content"), (), "write", True),
    # ---- 実行 ----
    Tool("run_python", "Run Python code and get its printed output. args: code.", "Python を実行", ("code",), (), "run", True),
    Tool("run_command", "Run a shell command in the workspace folder (e.g. tests, git status) and get its output. args: command.", "コマンドを実行", ("command",), (), "run", True),
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
    Tool("wait", "Wait a moment (e.g. for an app to open). args: seconds (max 10).", "待つ", ("seconds",), (), "pc"),
    # ---- ネット ----
    Tool("wikipedia", "Look up a topic in Wikipedia. args: query, lang (\"ja\" or \"en\", default ja).", "Wikipedia を調べる", ("query",), ("lang",), "net"),
    Tool("fetch_url", "Read the text of a web page. args: url.", "ウェブページを読む", ("url",), (), "net"),
]}


def available_tools(ctx: ToolContext) -> list[Tool]:
    groups = {"calc"}
    if ctx.delegate is not None:
        groups.add("team")
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
    return [t for t in TOOLS.values() if t.group in groups]


def step_schema(tools: list[Tool]) -> dict:
    """AIの1手を表す JSON スキーマ。道具ごとに必須の引数を強制し、引数なしの空振りを防ぐ。"""
    text = {"type": "string"}
    variants = [{
        "type": "object", "required": ["thought", "tool", "args"],
        "properties": {"thought": text, "tool": {"type": "string", "enum": [t.name]},
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
          "log": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "factorial": math.factorial, "gcd": math.gcd}
_CONSTS = {"pi": math.pi, "e": math.e}


def calc(expression: str) -> str:
    text = unicodedata.normalize("NFKC", expression).replace("×", "*").replace("÷", "/").replace("^", "**")
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)  # 1,000 → 1000
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise ToolError(f"式として読めません: {expression}") from e

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
    target = (root / (path or ".")).resolve()
    if target != root and root not in target.parents:
        raise ToolError("作業フォルダの外にはアクセスできません")
    return target


def _relative(ctx: ToolContext, target: Path) -> str:
    return target.relative_to(ctx.workspace.resolve()).as_posix()


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
    copy = ctx.backup_dir / stamp / _relative(ctx, target)
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, copy)


def list_files(ctx: ToolContext, path: str = ".") -> str:
    target = _resolve(ctx, path)
    if not target.is_dir():
        raise ToolError(f"フォルダがありません: {path}")
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = [f"{_relative(ctx, p)}{'/' if p.is_dir() else f'  ({p.stat().st_size} bytes)'}" for p in entries[:200]]
    if len(entries) > 200:
        lines.append(f"... 他 {len(entries) - 200} 件")
    return "\n".join(lines) or "(空のフォルダ)"


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
    if target.suffix.lower() in office.OFFICE_SUFFIXES:  # Word・Excel・PowerPoint は中身の文字を取り出して読む
        try:
            return f"[{target.name} の中身(文字だけを取り出したもの)]\n" + (office.read_text(target) or "(文字がありません)")
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
    needle, hits, scanned = query.lower(), [], 0
    files = [target] if target.is_file() else (p for p in target.rglob("*") if p.is_file())
    for file in files:
        if _SKIP_DIRS & set(file.relative_to(root).parts) or file.stat().st_size > 1_000_000:
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
                hits.append(f"{file.relative_to(root).as_posix()}:{number}: {line.strip()[:200]}")
                if len(hits) >= 50:
                    return "\n".join(hits) + "\n... (50件で打ち切り)"
    return "\n".join(hits) or "見つかりませんでした"


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
    _backup(ctx, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_encode(content, newline))
    return f"{'上書きしました(元の内容はバックアップ済み)' if existed else '作成しました'}: {_relative(ctx, target)} ({len(content)} 文字)"


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
    return f"{'上書きしました(元のファイルはバックアップ済み)' if existed else '作成しました'}: {_relative(ctx, target)} ({target.stat().st_size} bytes)"


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
    count = text.count(old)
    if not old or count == 0:
        raise ToolError("old の文字列がファイルの中に見つかりません。read_file で正確な文字列を確かめてください")
    if count > 1:
        raise ToolError(f"old の文字列が {count} か所にあります。前後の行も含めて、1か所に決まる長さにしてください")
    _backup(ctx, target)
    target.write_bytes(_encode(text.replace(old, new), newline))  # 元の改行の形のまま書き戻す
    return f"編集しました: {_relative(ctx, target)}"


def append_file(ctx: ToolContext, path: str, content: str) -> str:
    target = _resolve(ctx, path)
    _refuse_office(target)
    if target.suffix.lower() == ".xlsx" and target.is_file():  # Excel は、最後のシートの下に CSV の行を足す
        _edit_sheet(ctx, target, lambda text: text.rstrip("\n") + "\n" + content.strip("\n"))
        return f"追記しました: {_relative(ctx, target)}(表の最後に行を追加)"
    if target.is_dir():
        raise ToolError(f"フォルダと同じ名前です: {path}")
    newline = _newline_of(target)
    _backup(ctx, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as f:  # ファイルの改行の形に合わせて足す
        f.write(_encode(content, newline))
    return f"追記しました: {_relative(ctx, target)} ({len(content)} 文字)"


def make_folder(ctx: ToolContext, path: str) -> str:
    target = _resolve(ctx, path)
    target.mkdir(parents=True, exist_ok=True)
    return f"フォルダを作成しました: {_relative(ctx, target)}/"


def _transfer(ctx: ToolContext, path: str, to: str, move: bool) -> str:
    source, target = _resolve(ctx, path), _resolve(ctx, to)
    if not source.is_file():
        raise ToolError(f"ファイルがありません: {path}")
    if target.is_dir():
        target = target / source.name
    if target.exists():
        raise ToolError(f"移動先に同じ名前のファイルがあります: {to}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        _backup(ctx, source)
        shutil.move(str(source), str(target))
    else:
        shutil.copy2(source, target)
    return f"{'移動' if move else 'コピー'}しました: {_relative(ctx, source)} → {_relative(ctx, target)}"


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


def _finish(proc: subprocess.CompletedProcess) -> str:
    out, err = _decode(proc.stdout or b"").strip(), _decode(proc.stderr or b"").strip()
    text = out + (("\n[エラー出力]\n" + err) if err else "")
    return (text.strip() or "(出力なし)") + (f"\n[終了コード {proc.returncode}]" if proc.returncode else "")


def run_python(ctx: ToolContext, code: str, timeout: float = 30) -> str:
    with tempfile.TemporaryDirectory(prefix="celestial-run-") as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")
        cwd = str(ctx.workspace.resolve()) if ctx.workspace else tmp
        try:
            proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script)], cwd=cwd, timeout=timeout,
                                  stdin=subprocess.DEVNULL, capture_output=True)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"{timeout:.0f}秒以内に終わらなかったため中断しました") from e
    return _finish(proc)


def run_command(ctx: ToolContext, command: str, timeout: float = 90) -> str:
    cwd = str(ctx.workspace.resolve()) if ctx.workspace else str(Path.home())
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{timeout:.0f}秒以内に終わらなかったため中断しました") from e
    return _finish(proc)


# ---- PC操作・ネット・ワーカー(実体は pc.py / web.py / 管理AI) ----------------

def _screenshot(ctx: ToolContext, question: str = "") -> str:
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


def _number(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as e:
        raise ToolError(f"{name} は数字で指定してください") from e


_HANDLERS: dict[str, Callable[..., str]] = {
    "calc": lambda ctx, expression: calc(expression),
    "delegate": lambda ctx, role, task: ctx.delegate(role, task),
    "list_files": list_files, "find_files": find_files, "read_file": read_file, "search_files": search_files,
    "write_file": write_file, "edit_file": edit_file, "append_file": append_file, "make_folder": make_folder,
    "move_file": lambda ctx, path, to: _transfer(ctx, path, to, True),
    "copy_file": lambda ctx, path, to: _transfer(ctx, path, to, False),
    "delete_file": delete_file, "restore_file": restore_file,
    "make_word": lambda ctx, path, content, title="": _office(ctx, path, ".docx", lambda t: office.write_docx(t, content, title or None)),
    "make_excel": lambda ctx, path, data: _office(ctx, path, ".xlsx", lambda t: office.write_xlsx(t, data)),
    "make_slides": lambda ctx, path, content: _office(ctx, path, ".pptx", lambda t: office.write_pptx(t, content)),
    "run_python": run_python, "run_command": run_command,
    "system_info": lambda ctx: pc.system_info(),
    "list_windows": lambda ctx: "\n".join(title for _, title in pc.list_windows()) or "(ウィンドウなし)",
    "list_processes": lambda ctx: pc.list_processes(),
    "screenshot": _screenshot,
    "clipboard_read": lambda ctx: pc.clipboard_read() or "(クリップボードは空、または文字ではありません)",
    "clipboard_write": lambda ctx, text: pc.clipboard_write(text),
    "open": lambda ctx, target: pc.open_target(target),
    "focus_window": lambda ctx, title: pc.focus_window(title),
    "close_window": lambda ctx, title: pc.close_window(title),
    "type_text": lambda ctx, text, window=None: pc.type_text(text, window),
    "press_keys": lambda ctx, keys, window=None: pc.press_keys(keys, window),
    "media": lambda ctx, action: pc.media(action),
    "click": lambda ctx, x, y, button="left": pc.click(int(_number(x, "x")), int(_number(y, "y")), button),
    "scroll": lambda ctx, amount: pc.scroll(int(_number(amount, "amount"))),
    "volume": lambda ctx, action: pc.volume(action),
    "wait": lambda ctx, seconds: pc.wait(_number(seconds, "seconds")),
    "wikipedia": lambda ctx, query, lang="ja": web.wikipedia(query, lang),
    "fetch_url": lambda ctx, url: web.fetch_url(url),
}
_MAY_BE_EMPTY = {"content", "new"}  # 空文字に意味がある引数(空のファイルを作る、文字列を消す)

# ファイルを変える道具と、変わるファイルを指す引数(変更の記録・差分の表示に使う)
_CHANGING = {"write_file": ("path",), "edit_file": ("path",), "append_file": ("path",), "delete_file": ("path",),
             "restore_file": ("path",), "move_file": ("path", "to"), "copy_file": ("to",),
             "make_word": ("path",), "make_excel": ("path",), "make_slides": ("path",)}
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


def _preview(name: str, args: dict) -> str:
    """確認のときに見せる中身(書き込む内容、実行するコードなど)。"""
    if name in ("write_file", "append_file", "make_word", "make_slides"):
        return str(args.get("content", ""))[:3000]
    if name == "make_excel":
        return str(args.get("data", ""))[:3000]
    if name == "edit_file":
        return f"--- 変更前\n{str(args.get('old', ''))[:1500]}\n+++ 変更後\n{str(args.get('new', ''))[:1500]}"
    for key in ("code", "command", "text", "keys", "target"):
        if isinstance(args.get(key), str):
            return args[key][:3000]
    return ""


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
        values[key] = value if isinstance(value, str) else str(value)

    if tool.risky and ctx.confirm:
        if ctx.approve is None:
            raise ToolError("この操作には利用者の確認が必要ですが、確認を取る手段がありません")
        request = {"tool": name, "label": tool.label, "summary": summarize_call(name, values), "preview": _preview(name, values)}
        if not ctx.approve(request):
            raise ToolError("利用者がこの操作を許可しませんでした。別の方法を取るか、できなかったと答えてください")

    if ctx.changes is not None and name in _CHANGING:
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
    return result if len(result) <= MAX_OUTPUT else result[:MAX_OUTPUT] + "\n... (長いため省略)"


# ---- 実行記録との照合 -------------------------------------------------------

_CHECKS = [
    # (やったことになる道具, 依頼の言い回し, 回答の「やりました」, 訂正文)
    ({"write_file", "edit_file", "append_file", "make_folder", "move_file", "copy_file", "delete_file", "restore_file",
      "make_word", "make_excel", "make_slides"},
     re.compile(r"保存|作成|作って|書き込|書いて|書き出|出力して|編集|修正して|直して|変更|削除|消して|移動|コピー|create|save|write|edit|delete|move", re.I),
     re.compile(r"(作成|保存|書き込み|書き出し|出力|編集|修正|変更|削除|移動|コピー)(しました|済み|完了)|書き込んで|(created|saved|written|wrote|edited|updated|deleted|moved)\b", re.I),
     "ファイルの作成・変更は行われていません"),
    ({"run_python", "run_command"},
     re.compile(r"実行|動かして|走らせ|テスト|run|execute", re.I),
     re.compile(r"実行(しました|した結果|結果)|(executed|ran)\b", re.I),
     "コードやコマンドは実行されていません"),
    ({"open", "type_text", "press_keys", "click", "close_window", "focus_window", "clipboard_write", "volume", "media"},
     re.compile(r"開いて|起動|立ち上げ|入力して|打って|クリック|押して|閉じて|open|launch|type|click", re.I),
     re.compile(r"(開き|起動し|立ち上げ|入力し|クリックし|押し|閉じ)ました|(opened|launched|typed|clicked|closed)\b", re.I),
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
