"""プロジェクトを作る(Codex や Claude Code のように): 計画 → ファイルごとに書く → 確かめる → 直す。

小さいローカルのAIは、1回の応答で多数のファイルを正しく書き分けられない(道具の呼び出しの JSON に長いコードを
入れると崩れやすい)。そこで、次の手順に分ける(分解と、道具による検証で直す推論の手順):

1) 計画: 作るファイルの一覧と役割、動かし方・確かめ方を JSON で決める
2) 生成: ファイルを1つずつ、計画とそれまでに書いたファイルの中身を見せて、中身だけを書かせる
3) 確認: 構文を機械的に確かめる(Python・JSON・JavaScript)。コマンドの実行が許可されていれば、確かめ方のコマンドも動かす
4) 修正: 問題のあったファイルを、エラーを見せて書き直させる(2回まで)

書く場所は作業フォルダの中の新しいフォルダだけ。書く前に、計画を見せて1回だけ許可を取る(ファイルごとには聞かない)。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .tools import ToolContext, ToolError, execute

_PROJECT_REQUEST = re.compile(
    r"(アプリ|プロジェクト|ツール|ゲーム|サイト|ウェブページ|webページ|ホームページ|プログラム|スクリプト|ライブラリ|\bAPI\b|ボット|\bbot\b|\bCLI\b|"
    r"\b(app|application|project|tool|game|website|script|library)\b)"
    r"[^。\n]{0,24}?(を|の)?\s*(作って|作成して|作りたい|開発して|実装して|作ってください|作成してください)"
    r"|\b(make|create|build|write|develop)\b[^.\n]{0,40}?\b(app|application|project|tool|game|website|web page|script|library|cli|bot|api)\b",
    re.I)
_EDIT_REQUEST = re.compile(r"直して|修正|変更して|書き換え|追加して|改善|リファクタ|バグ|このフォルダ|この(プロジェクト|アプリ|コード)|既存|"
                           r"\b(fix|modify|refactor|existing)\b", re.I)
MAX_FILES = 10
MAX_CONTEXT = 14000

PLAN_SCHEMA = {
    "type": "object", "required": ["folder", "title", "files", "run", "test"],
    "properties": {
        "folder": {"type": "string"}, "title": {"type": "string"}, "language": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "required": ["path", "purpose"],
                                               "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}}}},
        "run": {"type": "string"}, "test": {"type": "string"},
    },
}
PLAN_SYSTEM = """You plan a small software project for a local AI to write file by file. Output JSON only.
- folder: a short lowercase ASCII folder name with hyphens (e.g. "todo-cli").
- title: a short title in the user's language.
- files: the files to create, in the order to write them (dependencies first), at most 8. Include README.md last.
  Use only the standard library unless the request needs something else. Keep it simple and complete.
  For Python, put the code in a few modules and include tests in tests/test_*.py that use unittest and need no network.
- purpose: what the file contains (functions, classes, and how it relates to the other files), in the user's language.
- run: the command to run the project from its folder (e.g. "python main.py").
- test: a command that exits 0 only when the project works (e.g. "python -m unittest discover -s tests -v"), or "" if not possible."""
FILE_SYSTEM = ("You write exactly one file of a software project. Output only the complete content of that file: "
                "no explanations, no Markdown code fences. Make it consistent with the plan and with the files already written "
                "(use the same function and class names). Write comments, messages shown to the user, and README text "
                "in the language of the request.")


def looks_like_project_request(task: str) -> bool:
    """「〜アプリを作って」のような、新しいプロジェクトを作る依頼か(既存のファイルを直す依頼は除く)。"""
    return bool(_PROJECT_REQUEST.search(task)) and not _EDIT_REQUEST.search(task)


def _clean_path(path: str) -> str | None:
    path = path.strip().replace("\\", "/").lstrip("./")
    if not path or path.startswith("/") or ":" in path or ".." in path.split("/") or len(path) > 120 or path.count("/") > 3:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-./]+", path):
        return None
    return path


def clean_plan(data: dict) -> dict:
    """AIが作った計画を確かめる。使えない形なら ValueError。"""
    folder = re.sub(r"[^a-z0-9\-]+", "-", str(data.get("folder") or "").lower()).strip("-")[:40] or "project"
    files, seen = [], set()
    for item in data.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = _clean_path(str(item.get("path") or ""))
        if path and path.lower() not in seen:
            seen.add(path.lower())
            files.append({"path": path, "purpose": str(item.get("purpose") or "")[:400]})
    if not files:
        raise ValueError("作るファイルを決められませんでした。作りたいものを、もう少し具体的に書いてください")
    files = files[:MAX_FILES]
    if not any(f["path"].lower() == "readme.md" for f in files):
        files.append({"path": "README.md", "purpose": "概要・使い方・動かし方"})
    readme = [f for f in files if f["path"].lower() == "readme.md"]
    files = [f for f in files if f["path"].lower() != "readme.md"] + readme  # README は最後に(中身が決まってから書く)
    return {"folder": folder, "title": str(data.get("title") or folder)[:80], "language": str(data.get("language") or "")[:30],
            "files": files, "run": str(data.get("run") or "").strip()[:200], "test": str(data.get("test") or "").strip()[:200]}


def strip_fences(text: str) -> str:
    """AIがコードを ``` で囲んでしまったときは、中身だけを取り出す。"""
    text = text.strip("\n")
    fenced = re.match(r"^\s*```[^\n]*\n([\s\S]*?)\n?```\s*$", text)
    return (fenced.group(1) if fenced else text) + "\n"


def syntax_error(path: str, content: str) -> str | None:
    """構文の誤り(なければ None)。Python・JSON・JavaScript(node があれば)を確かめる。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            return f"{path}:{e.lineno}: SyntaxError: {e.msg}"
        return None
    if suffix == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return f"{path}: JSON の誤り: {e}"
        return None
    if suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
        try:
            proc = subprocess.run(["node", "--check", "-"], input=content.encode("utf-8"), capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return f"{path}: " + proc.stderr.decode("utf-8", "replace").strip()[:800]
    return None


def _written_context(written: dict[str, str], skip: str) -> str:
    parts, size = [], 0
    for path, content in written.items():
        if path == skip:
            continue
        chunk = content if len(content) <= 4000 else content[:4000] + "\n...(省略)"
        if size + len(chunk) > MAX_CONTEXT:
            parts.append(f"=== {path} === (長いため省略)")
            continue
        size += len(chunk)
        parts.append(f"=== {path} ===\n{chunk}")
    return "\n\n".join(parts) or "(まだありません)"


def _file_prompt(plan: dict, request: str, path: str, written: dict[str, str], problem: str = "", tests_wrong: bool = False) -> str:
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
    purpose = next((f["purpose"] for f in plan["files"] if f["path"] == path), "")
    text = (f"Project: {plan['title']}\nRequest: {request}\nRun: {plan['run'] or '-'}\nTest: {plan['test'] or '-'}\n"
            f"Files:\n{listing}\n\nFiles already written:\n{_written_context(written, path)}\n\n")
    if problem:
        if tests_wrong:  # 本体を直しても同じ食い違いが残った: テストの期待のほうを、計画と本体に合わせる
            text += (f"The tests in `{path}` do not match the implementation, and fixing the implementation did not help. "
                     "Rewrite this test file so that it checks the behaviour that the plan describes and the implementation provides.\n")
        elif _is_test(path):
            text += f"The current `{path}` has a problem. Fix it and write the whole corrected file.\n"
        else:
            text += (f"The current `{path}` has a problem. The tests are the specification: change this file so that the tests pass "
                     "(return exactly the values the tests expect). Write the whole corrected file.\n")
        text += f"Problem:\n{problem[:3000]}\n\n"
        if path in written:
            text += f"Current content of `{path}`:\n{written[path][:6000]}\n\n"
    if path.lower().endswith("readme.md"):
        text += ("Describe only the files listed above and the Run/Test commands. Do not invent repository URLs, "
                 "dependencies or files that are not in the list.\n")
    return text + f"Write the complete content of `{path}` ({purpose})."


def _gist(problem: str) -> str:
    """問題の要点の1行(AssertionError: … などを優先。見出しだけの行は使わない)。"""
    lines = [l.strip() for l in problem.splitlines() if l.strip() and not l.strip().startswith(("[", "---", "===", "Traceback"))]
    for line in reversed(lines):
        if re.search(r"(Error|Exception|FAIL|assert|失敗|誤り)", line):
            return line[:200]
    return (lines[-1] if lines else problem.strip())[:200]


_CODE = (".py", ".js", ".mjs", ".ts", ".go", ".rs", ".java", ".kt", ".dart", ".rb", ".php", ".c", ".cpp", ".cs")


def _is_test(path: str) -> bool:
    name = path.split("/")[-1]
    return name.startswith("test") or name.endswith(("_test.py", ".test.js", "_test.go")) or "/tests/" in f"/{path}"


def _blamed(output: str, plan: dict) -> list[str]:
    """テストの失敗の出力から、直すファイルを選ぶ。出力に出てくる本体(テスト以外)のファイルを優先し、
    テストのファイルしか出てこない(テストの中の確かめで落ちた)ときは、本体のファイルを直す(誤りはたいてい本体にある)。"""
    names = [f["path"] for f in plan["files"]]
    hits = [p for p in names if re.search(r"[\\/\"' ]" + re.escape(p.split("/")[-1]) + r"[\"', :]", " " + output)]
    sources = [p for p in hits if not _is_test(p)]
    code = [p for p in names if p.lower().endswith(_CODE) and not _is_test(p)]
    return sources or code[:3] or hits[:2]


def create_project(*, request: str, ctx: ToolContext, plan_fn: Callable[[str], dict], write_fn: Callable[[str], str],
                   on_stage: Callable[..., None] | None = None) -> dict:
    """プロジェクトを作る。plan_fn(依頼) → 計画の JSON、write_fn(指示) → ファイルの中身(AIの呼び出し)。
    戻り値は {folder, title, files, checks: [...], test: {command, ok, output}, run}。"""
    def emit(phase: str, **data) -> None:
        if on_stage:
            on_stage("project", {"phase": phase, **data})

    if ctx.workspace is None or not ctx.allow_write:
        raise ToolError("プロジェクトを作るには、作業フォルダの設定と「ファイルの変更を許可」が必要です")
    emit("plan")
    plan = clean_plan(plan_fn(request))
    root = ctx.workspace.resolve()
    folder, n = plan["folder"], 2
    while (root / folder).exists():
        folder, n = f"{plan['folder']}-{n}", n + 1
    plan["folder"] = folder
    can_test = bool(plan["test"]) and ctx.allow_run
    if ctx.confirm:  # 書く前に、計画をまとめて1回だけ確かめる
        if ctx.approve is None:
            raise ToolError("この操作には利用者の確認が必要ですが、確認を取る手段がありません")
        preview = "\n".join(f"{folder}/{f['path']}  {f['purpose'][:60]}" for f in plan["files"])
        if can_test:
            preview += f"\n\n確かめるために、{folder} で次のコマンドを実行します: {plan['test']}"
        if not ctx.approve({"tool": "create_project", "label": "プロジェクトを作成", "summary": f"{folder}/({len(plan['files'])} ファイル)",
                            "preview": preview}):
            raise ToolError("利用者がプロジェクトの作成を許可しませんでした")
    quiet = replace(ctx, confirm=False)
    written: dict[str, str] = {}
    total = len(plan["files"])
    for number, item in enumerate(plan["files"], 1):
        emit("file", path=item["path"], n=number, total=total)
        content = strip_fences(write_fn(_file_prompt(plan, request, item["path"], written)))
        written[item["path"]] = content
        execute(quiet, "write_file", {"path": f"{folder}/{item['path']}", "content": content})

    checks: list[str] = []
    test = {"command": plan["test"] if can_test else "", "ok": None, "output": ""}
    runner = replace(quiet, workspace=root / folder)
    last_failure = ""
    for attempt in range(3):  # 確かめて、問題があれば直す(2回まで)
        emit("check", attempt=attempt)
        problems = {p: e for p, c in written.items() if (e := syntax_error(p, c))}
        tests_wrong = False
        if not problems and can_test:
            try:
                output = execute(runner, "run_command", {"command": plan["test"]})
            except ToolError as e:
                output = f"{e}\n[終了コード 1]"
            test.update(ok="[終了コード" not in output, output=output[-4000:])
            if not test["ok"]:
                failure = "\n".join(l for l in output.splitlines() if re.search(r"(Error|FAIL|assert)", l))
                names = [f["path"] for f in plan["files"]]
                tests = [p for p in names if _is_test(p) and p.split("/")[-1] in output]
                if attempt > 0 and failure == last_failure and tests:
                    # 本体を直しても同じ食い違いが残った: テストの期待が計画と合っていない(どちらもAIが書いたもの)
                    problems, tests_wrong = {p: output[-3000:] for p in tests}, True
                else:
                    problems = {p: output[-3000:] for p in _blamed(output, plan)}
                last_failure = failure
        checks = [f"{p}: {_gist(e)}" for p, e in problems.items()]
        if not problems or attempt == 2:
            break
        for path, problem in problems.items():
            emit("fix", path=path, attempt=attempt + 1)
            content = strip_fences(write_fn(_file_prompt(plan, request, path, written, problem, tests_wrong)))
            written[path] = content
            execute(quiet, "write_file", {"path": f"{folder}/{path}", "content": content})
    return {"folder": folder, "title": plan["title"], "files": plan["files"], "checks": checks, "test": test, "run": plan["run"]}


def report(result: dict) -> str:
    """利用者への報告(作ったもの・確かめた結果・動かし方)。"""
    lines = [f"「{result['title']}」を作業フォルダの {result['folder']}/ に作りました。", "", "ファイル:"]
    lines += [f"- {f['path']} — {f['purpose']}" for f in result["files"]]
    lines.append("")
    test = result["test"]
    if result["checks"]:
        lines.append("確認: 直しきれなかった問題が残っています。")
        lines += [f"- {c}" for c in result["checks"]]
    elif test["command"]:
        lines.append(f"確認: 構文に問題はなく、テスト(`{test['command']}`)にも合格しました。" if test["ok"]
                     else f"確認: テスト(`{test['command']}`)が通りませんでした。")
    else:
        lines.append("確認: 構文に問題はありません(テストの実行は、設定で「コマンドと Python の実行を許可」すると行います)。")
    if result["run"]:
        lines += ["", "動かし方:", "```", f"cd {result['folder']}", result["run"], "```"]
    return "\n".join(lines)
