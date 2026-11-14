"""書いたコードの、動かす前に分かる誤りを見つける(小さいモデルがよくやる「定義していない名前を使う」など)。

例: Flask のアプリで `@app.route('/')` と書いたのに `app = Flask(__name__)` を書き忘れる。
構文は正しいので compile では見つからず、動かすと NameError ですぐに止まる。

見逃しても害の少ない側に倒す: 名前がファイルのどこかで一度でも作られて(代入・import・def・引数など)いれば、
使う場所に関係なく「定義あり」とみなす。どこでも作られていない名前だけを知らせる。
"""
from __future__ import annotations

import ast
import builtins
import functools
import os
import re
import shutil
import subprocess
import tempfile

_ALWAYS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__", "__loader__", "__package__",
                                "__builtins__", "__path__", "__annotations__", "__dict__", "__class__",
                                "__qualname__", "__module__", "__cached__"}
_TYPE_PARAMS = tuple(getattr(ast, name) for name in ("TypeVar", "ParamSpec", "TypeVarTuple") if hasattr(ast, name))


def _injects_names(node: ast.AST) -> bool:
    """名前を見えない形で作る書き方か(globals() / vars() / locals() / exec / Enum._convert_)。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):  # vars(obj) は読むだけなので除く(vars() は locals() と同じ)
        return func.id in ("globals", "locals", "exec", "execfile") or (func.id == "vars" and not node.args)
    return isinstance(func, ast.Attribute) and func.attr == "_convert_"


def undefined_names(code: str) -> list[tuple[str, int]]:
    """どこでも作られていないのに使われている名前と、最初に使われた行(構文の誤りや、* の import があれば空)。"""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    bound = set(_ALWAYS)
    for node in ast.walk(tree):
        if _injects_names(node):
            return []  # globals().update(...) などで名前を作っている: どの名前があるか分からない
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return []  # どの名前が入るか分からない
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif _TYPE_PARAMS and isinstance(node, _TYPE_PARAMS):  # def first[T](...) (Python 3.12 から)
            bound.add(node.name)
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
            found[node.id] = min(found.get(node.id, node.lineno), node.lineno)
    return sorted(found.items(), key=lambda item: item[1])


_MODULES = {"os", "sys", "json", "time", "re", "math", "random", "subprocess", "shutil", "socket", "threading", "csv", "sqlite3",
            "logging", "collections", "itertools", "functools", "glob", "base64", "hashlib", "uuid", "statistics", "io",
            "tempfile", "platform", "string", "textwrap", "copy", "pprint", "argparse", "traceback", "webbrowser", "requests",
            "asyncio", "pathlib", "urllib", "calendar", "zoneinfo", "secrets", "html", "signal", "queue", "struct", "zipfile",
            "decimal", "inspect", "typing", "dataclasses", "enum", "operator", "heapq", "bisect", "contextlib", "getpass"}
_FROM = {"Path": "from pathlib import Path", "dataclass": "from dataclasses import dataclass",
         "defaultdict": "from collections import defaultdict", "Counter": "from collections import Counter",
         "deque": "from collections import deque", "ZoneInfo": "from zoneinfo import ZoneInfo",
         "timedelta": "from datetime import timedelta", "sleep": "from time import sleep"}
_FLASK_NAMES = {"Flask", "render_template", "jsonify", "request", "redirect", "url_for", "send_from_directory", "abort",
                "session", "Response", "make_response", "flash", "send_file", "render_template_string", "Blueprint"}
_CLASS_METHODS = r"(?:now|today|utcnow|strptime|fromtimestamp|fromisoformat|combine)\("
# datetime.now() のように、datetime をクラスとして使う書き方(datetime.datetime.now() の後ろの部分は数えない)
_DATETIME_CLASS = re.compile(r"(?<![\w.])datetime\." + _CLASS_METHODS)
_DATETIME_MODULE = re.compile(r"(?<![\w.])datetime\.datetime\.")


def _anchor(lines: list[str], index: int) -> str:
    """lines[index] の行を、ファイルの中で1か所に決まるまで上の行を足した文字列(edit_file の old に使う)。"""
    text = "\n".join(lines)
    start = index
    while start > 0 and text.count("\n".join(lines[start:index + 1])) > 1:
        start -= 1
    return "\n".join(lines[start:index + 1])


def suggest_fixes(code: str) -> list[dict]:
    """定義していない名前のうち、直し方が決まっているもの(import 漏れ・Flask の app の書き忘れ)の、edit_file の引数の例。

    小さいAIは、直し方の説明より、そのまま写せる old と new のほうが確実に直せる。決まらないものは挙げない。
    """
    missing = undefined_names(code)
    if not missing:
        return []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    lines = code.split("\n")
    first_use = dict(missing)
    flask_line = next((i for i, line in enumerate(lines) if re.match(r"from flask import [^()\n]+$", line.strip())), None)
    has_flask = "flask" in code.lower()
    imports, flask_names, need_app = [], [], False
    for name, _ in missing:
        if name == "app" and has_flask:
            need_app = True
        elif name in _FLASK_NAMES and has_flask:
            flask_names.append(name)
        elif name == "datetime":
            imports.append("from datetime import datetime" if _DATETIME_CLASS.search(code) else "import datetime")
        elif name in _FROM:
            imports.append(_FROM[name])
        elif name in _MODULES:
            imports.append(f"import {name}")
    if need_app and not re.search(r"from flask import [^\n]*\bFlask\b|^import flask\b", code, re.M) and "Flask" not in flask_names:
        flask_names.append("Flask")
    fixes = []
    flask_fix = None
    if flask_names and flask_line is not None:  # 今の「from flask import ...」の行に名前を足す
        old = _anchor(lines, flask_line)
        flask_fix = {"old": old, "new": old.rstrip() + ", " + ", ".join(dict.fromkeys(flask_names))}
        fixes.append(flask_fix)
    elif flask_names:
        imports.insert(0, "from flask import " + ", ".join(dict.fromkeys(flask_names)))
    tops = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if need_app:  # app = Flask(__name__) は、最初に app を使う行より前の import のあとに置く
        tops = [n for n in tops if (n.end_lineno or n.lineno) < first_use["app"]]
        if not tops and not imports:
            need_app = False
    added = list(dict.fromkeys(imports)) + (["", "app = Flask(__name__)"] if need_app else [])
    if added:
        if tops:
            last = (tops[-1].end_lineno or tops[-1].lineno) - 1
            if flask_fix is not None and last == flask_line:  # 同じ行を old にする2つの編集は、1つにまとめる
                flask_fix["new"] += "\n" + "\n".join(added)
            else:
                old = _anchor(lines, last)
                fixes.append({"old": old, "new": old + "\n" + "\n".join(added)})
        else:
            index = next((i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")), None)
            if index is not None:
                old = _anchor(lines, index)
                fixes.append({"old": old, "new": "\n".join(added) + "\n" + ("\n" if need_app else "") + old})
    return fixes[:2]


_IN_TEMPLATES = re.compile(r"(?:\./)?templates[/\\]")
_TEMPLATES_PREFIX = re.compile(r"""(['"])(?:\./)?templates[/\\]+""")


def template_path_fixes(code: str) -> list[tuple[str, dict]]:
    """Flask の render_template に 'templates/index.html' のように templates/ から渡している所と、その直し方。

    render_template は templates フォルダの中を探すので、templates/ を付けると見つからない(TemplateNotFound)。
    except で受けていると「エラーの文だけのページ」になり、原因が見えにくい(実際にあった)。template_folder を変えて
    いるなら挙げない。戻り値は (渡している名前, edit_file の引数 {"old", "new"}) の並び。
    """
    if "render_template" not in code or "template_folder" in code:
        return []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    strings: dict[str, ast.Constant] = {}  # name = 'templates/...' を、あとで render_template(name) で渡す書き方
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    strings[target.id] = node.value
    lines = code.split("\n")
    found: dict[int, tuple[str, dict]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) != "render_template":
            continue
        arg = node.args[0]
        const = arg if isinstance(arg, ast.Constant) else strings.get(arg.id) if isinstance(arg, ast.Name) else None
        if const is None or not isinstance(const.value, str) or not _IN_TEMPLATES.match(const.value):
            continue
        index = const.lineno - 1
        if index in found or index >= len(lines):
            continue
        line = lines[index]
        fixed = _TEMPLATES_PREFIX.sub(r"\1", line)
        if fixed != line:
            old = _anchor(lines, index)
            found[index] = (const.value, {"old": old, "new": old[:len(old) - len(line)] + fixed})
    return list(found.values())[:2]


def datetime_fixes(code: str) -> list[dict]:
    """import の形と合わない datetime の使い方(動かすと AttributeError)の、edit_file の引数の例。

    from datetime import datetime のあとの datetime.datetime.now() と、import datetime のあとの datetime.now()。
    小さいAIは両方を混ぜがち(実際にあった)。両方の import があるなら、どちらも正しいので挙げない。
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    as_class = as_module = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            as_class |= any(a.name == "datetime" and a.asname is None for a in node.names)
        elif isinstance(node, ast.Import):
            as_module |= any(a.name == "datetime" and a.asname is None for a in node.names)
    if as_class == as_module:
        return []
    pattern, fixed = ((_DATETIME_MODULE, lambda line: _DATETIME_MODULE.sub("datetime.", line)) if as_class else
                      (_DATETIME_CLASS, lambda line: re.sub(r"(?<![\w.])datetime\.(?=" + _CLASS_METHODS + ")", "datetime.datetime.", line)))
    lines = code.split("\n")
    fixes = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") or not pattern.search(line):
            continue
        old = _anchor(lines, index)
        fixes.append({"old": old, "new": old[:len(old) - len(line)] + fixed(line)})
    return fixes[:2]


_SAME_NAME_OK = ("overload", "register", "setter", "getter", "deleter")  # 同じ名前で作り直すのが正しい書き方
_ROUTES = ("route", "get", "post", "put", "delete", "patch")


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return getattr(target, "attr", None) or getattr(target, "id", None) or ""


def _is_route(node: ast.expr) -> bool:
    """@app.route('/') や @bp.get('/x') のような、関数の名前がそのままページの名前(endpoint)になるデコレーター。"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _ROUTES
            and not any(k.arg == "endpoint" for k in node.keywords))


_UNCLOSED = re.compile(r"unterminated (?:f-)?string literal|EOL while scanning string literal")


def repair_string_newlines(code: str, limit: int = 8) -> str | None:
    """文字列の中に本当の改行が入って閉じていないところを、\\n の2文字に戻したコード(直らない・直す所が無ければ None)。

    JSON で渡したコードの "print('a\\n')" の \\n が改行になって、文字列が切れていた(小さいAIで何度もあった)。閉じていない
    文字列の次の行に同じ引用符があるとき(空の行は続けてよい)だけつなぎ、最後に構文が通ったときだけ返す。
    """
    fixed, joins = code, 0
    while joins <= limit:
        try:
            compile(fixed, "<check>", "exec")
        except SyntaxError as e:
            lines = fixed.split("\n")
            i = (e.lineno or 0) - 1
            if not _UNCLOSED.search(e.msg or "") or not 0 <= i < len(lines) - 1:
                return None
            m = re.match(r"[rRbBfFuU]{0,2}(['\"])", lines[i][max((e.offset or 1) - 1, 0):])
            following = lines[i + 1]
            if m is None or (following.strip() and m.group(1) not in following):
                return None
            lines[i] += "\\n" + following
            del lines[i + 1]
            fixed = "\n".join(lines)
            joins += 1
            continue
        except ValueError:
            return None
        return fixed if joins else None
    return None


def top_level_defs(code: str) -> dict[str, list[ast.AST]] | None:
    """モジュールの一番上の段の関数とクラス(名前 → 作っている所の並び)。構文の誤りがあれば None。

    同じ名前で作り直すのが正しい書き方(@overload、@f.register、property の setter、名前 _)は数えない。
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    found: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or node.name == "_":
            continue
        if any(_decorator_name(d) in _SAME_NAME_OK for d in node.decorator_list):
            continue
        found.setdefault(node.name, []).append(node)
    return found


def block_lines(node: ast.AST) -> tuple[int, int]:
    """関数やクラスの、デコレーターから最後の行までの範囲(0 から数えた行の番号。終わりは含まない)。"""
    first = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    return first - 1, node.end_lineno or node.lineno


def _removal(lines: list[str], start: int, end: int) -> dict:
    """lines[start:end] のかたまりを消す edit_file の引数(old が1か所に決まるまで、上の行を足す)。"""
    text = "\n".join(lines)
    top = start
    while top > 0 and text.count("\n".join(lines[top:end])) > 1:
        top -= 1
    return {"old": "\n".join(lines[top:end]), "new": "\n".join(lines[top:start])}


def duplicate_routes(code: str) -> list[tuple[str, list[int], dict | None]]:
    """Flask で、同じ名前の関数を2つ以上 @app.route などに使っている所。

    関数の名前がページの名前(endpoint)になるので、起動したときに AssertionError: View function mapping is
    overwriting an existing endpoint function で止まる。小さいAIは、関数を足す編集を繰り返して同じ関数を重ねがち
    (実際にあった)。戻り値は (名前, def の行の並び, 直し方の edit_file の引数 か None)。中身まで同じ写しなら、
    後ろの写しを消す直し方を付ける(中身が違うなら、どちらを残すかは決められないので付けない)。
    """
    if "flask" not in code.lower():
        return []
    defs = top_level_defs(code) or {}
    lines = code.split("\n")
    found = []
    for name, nodes in defs.items():
        routed = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and any(_is_route(d) for d in n.decorator_list)]
        if len(routed) < 2:
            continue
        blocks = [block_lines(n) for n in routed]
        first = [line.strip() for line in lines[blocks[0][0]:blocks[0][1]]]
        same = next(((s, e) for s, e in blocks[1:] if [line.strip() for line in lines[s:e]] == first), None)
        found.append((name, [n.lineno for n in routed], _removal(lines, *same) if same else None))
    return found


_LATER_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_COMPOUND = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith) + (
    (getattr(ast, "TryStar"),) if hasattr(ast, "TryStar") else ()) + ((ast.Match,) if hasattr(ast, "Match") else ())


def _loads_now(stmt: ast.stmt) -> list[ast.Name]:
    """モジュールを読み込むときに、その文がすぐ使う名前(関数の中身・lambda・内包表記は、あとで動くので含めない)。"""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        roots = stmt.decorator_list + stmt.args.defaults + [d for d in stmt.args.kw_defaults if d is not None]
    elif isinstance(stmt, ast.ClassDef):
        roots = stmt.decorator_list + stmt.bases + [k.value for k in stmt.keywords]
    elif isinstance(stmt, ast.AnnAssign):  # 型の注釈は見ない(from __future__ import annotations なら、あとで読まれる)
        roots = [stmt.value] if stmt.value is not None else []
    elif isinstance(stmt, _COMPOUND + (ast.Import, ast.ImportFrom)):
        return []  # 条件や例外で名前が変わる書き方は見ない
    else:
        roots = [stmt]
    found: list[ast.Name] = []
    todo = list(roots)
    while todo:
        node = todo.pop()
        if isinstance(node, _LATER_SCOPES):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return found


def _binds(stmt: ast.stmt) -> set[str]:
    """その文が、モジュールの段で作る名前。"""
    names: set[str] = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    for node in ast.walk(stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def used_before_defined(code: str) -> list[tuple[str, int, int]]:
    """モジュールの上から順に動かしたとき、作る前に使っている名前(動かすと NameError)。(名前, 使う行, 作る行) の並び。

    例: 足した @app.route('/api/time') の関数が、app = Flask(__name__) の行より上にある(qwen2.5-coder:7b で実際に
    あった)。名前はファイルのどこかで作られているので、定義していない名前の確かめでは見つからない。
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    later: dict[str, int] = {}
    for stmt in tree.body:
        for name in _binds(stmt):
            later.setdefault(name, stmt.lineno)
    bound = set(_ALWAYS)
    found: dict[str, tuple[int, int]] = {}
    for stmt in tree.body:
        for node in _loads_now(stmt):
            if node.id not in bound and node.id in later and later[node.id] > node.lineno and node.id not in found:
                found[node.id] = (node.lineno, later[node.id])
        bound |= _binds(stmt)
    return [(name, use, made) for name, (use, made) in found.items()][:3]


def _flask_apps(tree: ast.Module) -> dict[str, list[ast.Assign]]:
    """モジュールの段の app = Flask(__name__) の行(名前 → 行の並び)。"""
    apps: dict[str, list[ast.Assign]] = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call) and _decorator_name(stmt.value) == "Flask"):
            apps.setdefault(stmt.targets[0].id, []).append(stmt)
    return apps


def flask_order_fixes(code: str) -> tuple[str, list[dict]]:
    """Flask の app の作り方の順番の誤りと、直し方(edit_file の引数)。誤りがなければ ("", [])。

    1) @app.route を、app = Flask(__name__) より上で使っている: 動かすと NameError で止まる
    2) app = Flask(__name__) が2回ある: 2回目で作り直され、それより前に付けたページ(/api/time など)が消えて 404 になる
       (1 を直そうとして、小さいAIは上にもう1行 app = Flask(__name__) を足しがち。実際にあった)
    """
    if "flask" not in code.lower():
        return "", []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return "", []
    lines = code.split("\n")
    for name, assigns in _flask_apps(tree).items():
        if len(assigns) > 1:  # 2回目の行を消す
            index = assigns[1].lineno - 1
            old = _anchor(lines, index)
            return (f"{name} = Flask(...) が2回あります({assigns[0].lineno} 行目と {assigns[1].lineno} 行目)。2回目で {name} が"
                    "作り直されるので、それより前に付けた @app.route のページが消えて、開くと 404 になります。2回目の行を消して"
                    "ください", [{"old": old, "new": old[:len(old) - len(lines[index])].rstrip("\n")}])
    for name, use, made in used_before_defined(code):
        assigns = _flask_apps(tree).get(name, [])
        if not assigns or assigns[0].lineno != made or assigns[0].end_lineno != made:
            continue
        line = lines[made - 1]
        # 1) app = Flask(__name__) を、最初に使う文のすぐ上へ移す(上に足してから、下の行を消す)
        first = next(s for s in tree.body if block_lines(s)[0] < use <= (s.end_lineno or s.lineno))
        start = block_lines(first)[0]
        top = _anchor(lines, start)
        fix1 = {"old": top, "new": top[:len(top) - len(lines[start])] + line.strip() + "\n\n" + lines[start]}
        moved = code.replace(top, fix1["new"], 1).split("\n")
        index = made - 1 + 2  # 2行足したので、元の行は2行下に動く
        old = _anchor(moved, index)
        fix2 = {"old": old, "new": old[:len(old) - len(moved[index])].rstrip("\n")}
        return (f"'{name}' を、作る前に使っています({use} 行目。{name} = Flask(__name__) は {made} 行目)。Python はファイルの"
                f"上から順に動くので、このままでは動かすと NameError で止まります。{name} = Flask(__name__) を、最初に使う所より"
                "上に移してください(上に足してから、下の行を消します。2つ残すと、ページが消えます)", [fix1, fix2])
    return "", []


_MARKUP_LINE = re.compile(r"\s*(?:<(?:!DOCTYPE|/?[a-zA-Z][\w-]*)\b|[\w-]+\s*:\s*[^;=()]+;\s*$|[.#]?[\w-]+(?:\s*[,>]\s*[.#]?[\w-]+)*\s*\{\s*$|\}\s*$)",
                          re.I)


def markup_in_python(code: str) -> bool:
    """Python のファイルの中身が、HTML や CSS そのもの(Python のコードではない)か。

    書く役のAIが、app.py に <!DOCTYPE html> から始まるページを書いていた(qwen2.5-coder:7b で実際にあった)。
    先頭が <!DOCTYPE か <html、または行の6割以上がタグや CSS の行のとき。
    """
    lines = [line for line in code.split("\n") if line.strip()]
    if not lines:
        return False
    if re.match(r"\s*<(?:!DOCTYPE|html)\b", lines[0], re.I):
        return True
    return len(lines) >= 5 and sum(1 for line in lines if _MARKUP_LINE.match(line)) >= len(lines) * 0.6


def python_problem(path: str, code: str) -> str | None:
    """Python のファイルの、動かす前に分かる誤り(なければ None)。"""
    missing = undefined_names(code)
    if not missing:
        return None
    names = ", ".join(f"'{name}' (line {line})" for name, line in missing[:4])
    return f"{path}: NameError: these names are used but never defined or imported: {names}"


# ---- JavaScript(ページの中の <script> と .js のファイル) ----

_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_TEMPLATE_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)  # Flask(Jinja)のテンプレートの書き方
_TAG_GAP = re.compile(r"(?<=>)((?:\\n)+)(?=[ \t]*<)")  # タグとタグの間の \n(2文字)


def script_kind(attrs: str) -> str | None:
    """<script> の属性から、JavaScript の種類("classic" か "module")。JavaScript でない(JSON など)・外のファイルなら None。"""
    if re.search(r"\bsrc\s*=", attrs, re.I):
        return None
    kind = re.search(r"""\btype\s*=\s*["']?([\w/+.-]*)""", attrs, re.I)
    name = kind.group(1).lower() if kind else ""
    if name == "module":
        return "module"
    return "classic" if name in ("", "text/javascript", "application/javascript") else None


def page_scripts(path: str, text: str) -> list[tuple[int, int, str, bool]]:
    """ページ(HTML)の中の JavaScript: [(中身の始まりの位置, その行, 中身, module か)]。.js のファイルなら、ファイル全体。"""
    if path.lower().endswith((".js", ".mjs")):
        return [(0, 1, text, path.lower().endswith(".mjs"))]
    found = []
    for m in _SCRIPT_BLOCK.finditer(text):
        kind = script_kind(m.group(1))
        if kind and m.group(2).strip():
            found.append((m.start(2), text.count("\n", 0, m.start(2)) + 1, m.group(2), kind == "module"))
    return found


@functools.lru_cache(maxsize=1)
def _node() -> str | None:
    return shutil.which("node")


def js_syntax_error(code: str, module: bool = False) -> tuple[int, str] | None:
    """JavaScript の構文の誤り (中身の中での行, 説明)。誤りが無い・node が無い・確かめられないときは None。

    ブラウザーと node の JavaScript の文法は同じなので、node --check で確かめる(動かしはしない)。
    """
    node = _node()
    if not node:
        return None
    plain = _TEMPLATE_TAG.sub(lambda m: ("null" if m.group(0).startswith("{{") else "") + "\n" * m.group(0).count("\n"), code)
    fd, name = tempfile.mkstemp(suffix=".mjs" if module else ".js", prefix="celestial-check-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(plain)
        done = subprocess.run([node, "--check", name], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if done.returncode == 0:
        return None
    message = re.search(r"^(SyntaxError: .+)$", done.stderr or "", re.M)
    if not message:
        return None  # 構文の誤りではない(node が動かなかった、など)
    where = re.search(re.escape(name) + r":(\d+)", done.stderr)
    return (int(where.group(1)) if where else 1), message.group(1).strip()


def _stray_newlines(line: str) -> list[int]:
    """行の中の \n(2文字)のうち、引用符の外にあるものの位置(行の頭は引用符の外とみなす)。"""
    spots, quote, i = [], "", 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif line.startswith("\\n", i):
            spots.append(i)
            i += 2
            continue
        elif line.startswith("//", i):
            break  # 行の残りはコメント
        i += 1
    return spots


def repair_js_newlines(code: str, module: bool = False, limit: int = 12) -> str | None:
    """JavaScript の、改行のつもりの \n(2文字。引用符の外にあるもの)を本当の改行にしたもの。

    node で構文を確かめ、誤りの行に引用符の外の \n があれば改行にして、通るまで繰り返す。通ったときだけ返す
    (直す所が無い・直らない・node が無ければ None)。文字列の中の \n は、そのまま残す。
    """
    if "\\n" not in code or not _node():
        return None
    fixed = code
    for _ in range(limit):
        found = js_syntax_error(fixed, module)
        if found is None:
            return fixed if fixed != code else None
        lines = fixed.split("\n")
        i = found[0] - 1
        spots = _stray_newlines(lines[i]) if 0 <= i < len(lines) else []
        if not spots:
            return None
        line = lines[i]
        for at in reversed(spots):
            line = line[:at] + "\n" + line[at + 2:]
        lines[i] = line
        fixed = "\n".join(lines)
    return None


def repair_page_newlines(path: str, text: str) -> str | None:
    """ページ(HTML)か .js のファイルの、改行のつもりの \n(2文字)を本当の改行にしたもの(直す所が無ければ None)。

    JSON の中で \\n と2重に書いたため、<script> の中に `...;\n    document...` が入って、ブラウザーでは構文の誤りで
    動かなかった(qwen2.5-coder:7b で実際にあった)。JavaScript は node で構文が通ったときだけ直す。タグとタグの間の \n
    (画面にそのまま \n と出る)は、改行にする。
    """
    if "\\n" not in text:
        return None
    fixed = text
    for start, _, body, module in reversed(page_scripts(path, text)):
        mended = repair_js_newlines(body, module)
        if mended is not None:
            fixed = fixed[:start] + mended + fixed[start + len(body):]
    if not path.lower().endswith((".js", ".mjs")):
        parts, last = [], 0
        for m in _SCRIPT_BLOCK.finditer(fixed):  # <script> の外だけ
            parts.append(_TAG_GAP.sub(lambda g: "\n" * (len(g.group(1)) // 2), fixed[last:m.start()]))
            parts.append(fixed[m.start():m.end()])
            last = m.end()
        parts.append(_TAG_GAP.sub(lambda g: "\n" * (len(g.group(1)) // 2), fixed[last:]))
        fixed = "".join(parts)
    return fixed if fixed != text else None


def script_problem(path: str, text: str) -> tuple[int, str] | None:
    """ページ(HTML)か .js のファイルの、JavaScript の構文の誤り (ファイルの中の行, 説明)。無い・確かめられなければ None。"""
    for _, line, body, module in page_scripts(path, text):
        found = js_syntax_error(body, module)
        if found:
            return line + found[0] - 1, found[1]
    return None
