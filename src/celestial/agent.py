"""AIエージェント: 目標を受け取り、道具を使いながら自分で段取りして進め、最後に報告する(Codex や Claude Code のように)。

進め方:
1) 計画: 目標を 1〜6 の具体的な手順に分ける(画面にチェックリストとして出し、進み具合を示す)
2) 実行: 1手ごとに「考え・今の手順の番号・使う道具・引数」または「最終報告」を JSON で出させる(構造は文法で強制する)。
   道具の実行はこの側で行い、結果を次の手の材料として渡す。長いファイルは write_code で、書く役のAIに中身だけを書かせる。
3) 確かめ: コードを変えたら動かす・テストする(失敗したら直す)。終わったら、起動したサーバーなどを止める。

小さいモデルでも迷わないように:
- プログラムの失敗(終了コードが 0 以外)は「失敗」として扱い、直すよう促す(成功と取り違えない)
- よくある取り違え(ファイルを run_python で import しようとする など)には、正しい道具を示す
- 同じ呼び出しで同じ結果が続いたら、行き詰まりとみなして打ち切る(テストを直しながら何度も動かすのは続けられる)
- 何も変えずに、成功したことと同じ呼び出しを繰り返したら、動かさずに「もう終わっている」と知らせる(確かめ直しの繰り返しを防ぐ)
- 打ち切ったときの報告は、作業の記録だけを材料に、普通の文章で書かせる

安全の決まりは tools.py と guard.py にある(作業フォルダの外に出ない・危ないコマンドは止める・確かめる)。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from . import knowhow, pagerun
from .backends import BackendError, Message, ModelInfo
from .tools import (CODE_PROBLEM, ENDED_EARLY, ERROR_PAGE, MARKUP_IN_PYTHON, NO_PAGE, NOT_OPENED, OPENED_PAGE, OUTSIDE_DOWN, SLOW_MARK, STALE_SERVER, STOPPED, is_server_file,
                    running_server, serving, PAGE_PROBLEM, code_hint, server_problems, PORT_BUSY, PORT_IN_USE, TOOLS, ToolContext, ToolError, available_tools, execute, fact_check,
                    file_text, NO_CHANGE,
                    old_hint, step_schema, stop_processes, summarize_call, unavailable_notes)
from .workers import ROLES

_KEEP_FULL = 6        # 直近この回数ぶんの道具の結果は全文のまま渡す
_OLD_RESULT = 400     # それより古い結果は、この文字数に縮める(文脈があふれないように)
_MAX_FAILURES = 4     # 続けてこの回数失敗したら打ち切る
_RULES = """You are Celestial, an autonomous AI agent working on the user's own PC (like Codex or Claude Code).
Work step by step toward the user's goal with tools, then report.

Reply with exactly one JSON object each turn:
- To act: "thought" (one short sentence), "step" (the number of the plan step you are working on), "tool" (a tool name) and "args".
- To finish: "tool" = "answer" and "answer" = the final report, written in the user's language.

Rules:
- Work on your own until the goal is reached. Do not ask the user questions or for permission (the system asks the user
  itself when an action needs it). Choose sensible names, file layouts and defaults yourself.
- Use exactly the names and values the user gave (file and folder names, port numbers, texts, numbers).
- Single files go in the workspace folder itself. A new app with several files goes in one new folder whose name says
  what it is (e.g. weather-signage). If the user names a folder, use exactly that name. Never copy names from examples.
  "This folder" (このフォルダ) and "here" mean the workspace folder itself: work there, do not make a new folder for it.
- Never guess file contents, command output or what is on the screen: use the tools. Use calc for arithmetic.
- Look before you change: list, tree or read files first. edit_file needs the exact existing text.
- Folders: make_folder. Code, HTML and other files longer than about 15 lines: write_code with precise instructions.
  Short files: write_file. Changes to existing files: edit_file.
- To run a script you wrote, use run_file with its path (it runs in the script's own folder). run_python only runs code
  you put in args.code; it can not run or import your files. Tool names are not file names.
- Commands that finish (tests, builds, git, pip install): run_command. Servers and other long-running programs:
  start_process (run_file also starts a server script in the background), then check_url or process_output to test them.
  Leave them running when you finish: Celestial stops them when the request ends. Running it again restarts it, so use
  stop_process only when the user asks to stop one (after it, its URL does not answer any more).
  Check web pages and APIs you made with check_url, not screenshot (the screen shows the user's desktop, not your page).
- After creating or changing code, run it or its tests. If it fails, read the error, fix the code, and run it again.
- If a tool fails, read the error and try a different approach. Do not repeat the same call, and do not redo steps that
  already succeeded.
- PC control: open the app, wait for it, check its window title with list_windows, then type or press keys with the
  window argument set to that title (so input never goes to the wrong app). Check the result with screenshot when unsure.
- Never stop to get an API key, an account or a sign-up. Use a service that needs no key (see the useful facts when given).
  If a key is truly needed, read it from an environment variable in the code and tell the user in the answer how to set it.
- When the user asks to continue earlier work, keep working on the same folder and files as before (look at them first);
  do not start over somewhere else.
- delegate gives one self-contained piece of work (translate, summarize, write, analyse a given text) to a specialist AI.
  The worker can not see your files, program output, the screen or the internet: put all needed text in the task.
- Do only what the user asked. Stop as soon as the goal is reached.
- The final answer: what you created or changed (with paths), what you ran and the result, and how to use it. Do not paste whole files."""

_EXAMPLE = {
    "en": [("Write the script.", "write_file", {"path": "greet.py", "content": "print('hello')\n"}),
           ("Run it to check that it works.", "run_file", {"path": "greet.py"}),
           ("It printed hello, so the goal is reached.", "answer", "I created greet.py and ran it. Output: hello")],
    "ja": [("スクリプトを書く。", "write_file", {"path": "greet.py", "content": "print('hello')\n"}),
           ("動くか実行して確かめる。", "run_file", {"path": "greet.py"}),
           ("hello と表示されたので完了。", "answer", "greet.py を作って実行しました。出力: hello")],
}

PLAN_SCHEMA = {"type": "object", "required": ["steps"],
               "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}}}}
_PLAN_RULES = """You plan the work of an AI agent on the user's PC. Break the user's goal into 1 to 6 short, concrete steps that
the agent can do with its tools. Each step is one short sentence of at most 40 characters, in the user's language.
- Never put code, commands or file contents in a step.
- Creating a file together with all of its content is one step (never split one file into several steps).
- Single files go in the workspace folder itself. A new app with several files gets one folder whose name says what it is.
  If the user names a folder, use exactly that name. "This folder" (このフォルダ) is the workspace folder itself.
- Cover everything the user asked for (each thing to show or do); do not drop any of it.
- Use exactly the names the user gave. Do not add steps the user did not ask for.
- Never add a step to get an API key or an account: use services that need no key.
- Never add a step that looks up data with a command (a city's coordinates, today's weather): the program you write gets
  its data when it runs, and values that are known (such as the coordinates of a city) are written in the code.
- When the user asks to continue earlier work, plan only what is left, in the same folder and files as before.
- The last step is usually running or checking the result (run the program, or read the file back; not opening it in an app).
Output JSON only.

Example for a different goal ("calc.py に足し算の関数を作って、テストして"):
{"steps": ["calc.py に足し算の関数を書く", "test_calc.py にテストを書く", "テストを実行して、失敗したら直す"]}"""

_CODE_LINE = re.compile(r"^\s*(from|import|def|class|return|print|const|let|var|function|public|#include)\b|^\s*[<{\[]")
# 同じ呼び出しでも、時間で結果が変わる道具・PC の操作(何度使っても「もう終わっている」とはみなさない)
_VOLATILE = {"screenshot", "wait", "list_windows", "list_processes", "press_keys", "click", "scroll", "volume", "media",
             "system_info", "process_output", "check_url", "type_text", "clipboard_read", "clipboard_write", "focus_window",
             "open", "close_window", "start_process"}
# 様子を見るだけの道具(同じものを続けて何度も見ても、新しいことは分からない)
_OBSERVE = {"screenshot", "check_url", "process_output", "list_windows", "list_processes", "system_info", "clipboard_read"}


def _looks_allowed(tool: str, before: str) -> int:
    """何も変えないまま、同じ呼び出しで様子を見てよい回数。成功したページ(check_url)は1回: 開き直しても同じ。
    ただし、外のサービスが答えなかったときは、戻ったかを見るために2回まで。"""
    return 1 if tool == "check_url" and OUTSIDE_DOWN not in before else 2


_EXIT = re.compile(r"\[終了コード (-?\d+)\]\s*$")
_QUICK_EXIT = re.compile(r"すぐに終了しました\(終了コード (-?\d+)\)")
_RUNS = ("run_command", "run_python", "run_file")
_CHECKS = ("run_file", "check_url", "start_process")  # 作ったものを動かす・開く(成功すれば、確かめたことになる)
# 考えの最後が「完了しました」(作業ぜんぶが終わったという言い方)。「〜を作成する。」のような、これからの手は含まない
_DONE_THOUGHT = re.compile(r"(?:完了|完成|終了|終わり)(?:しました|しています|です)?[。.!！]?\s*$|"
                           r"\b(?:is|are|all|now|everything)\s+(?:done|finished|complete|completed)\.?\s*$", re.I)
# 「正しく動いていることを確認しました」(確かめて、うまくいったという言い方)。「エラーを確認しました」は含まない
_WORKS_THOUGHT = re.compile(r"(?:正しく|正常に|問題なく|きちんと)[^。]*(?:確認しました|確認できました|動作しています|表示されています|"
                            r"動いています)[。.!！]?\s*$|\b(?:it|the (?:page|app|server|program|script|site|signage))\s+(?:works|is working|runs|"
                            r"displays correctly|shows correctly)(?:\s+(?:now|correctly|fine|well))?\.?\s*$", re.I)
_CODE_WRITES = ("write_file", "write_code", "edit_file", "append_file")
# 止める理由が、このあとも作業を続けるため(直す・変える・起動し直す)の考え。依頼が止めることを頼んでいるかどうか
_MORE_WORK = re.compile(r"直|修正|変更|変え|追加|足す|再起動|起動し直|立ち上げ直|もう一度起動|ポート|番号|"
                        r"\b(?:fix|change|add|update|restart|relaunch|port)", re.I)
_ASKS_TO_STOP = re.compile(r"止めて|止める|停止|終了させ|落として|\b(?:stop|kill|shut ?down)\b", re.I)
_STATUS_LINE = re.compile(r"(?:\([^\n]*\)\n)?HTTP (\d{3})\b")  # check_url の最初の行(待った注意の次)
_PEEK_STATUS = re.compile(r"を開くと HTTP (\d{3})\b")  # 裏で起動したときに、ページを開いてみた結果


def _compact(messages: list[Message]) -> list[Message]:
    """古い道具の結果を縮めた写しを返す。最初の指示と依頼、直近のやり取りはそのまま残す。"""
    results = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("[")]
    old = set(results[:-_KEEP_FULL]) if len(results) > _KEEP_FULL else set()
    return [{**m, "content": m["content"][:_OLD_RESULT] + "\n...(古い結果のため省略)"} if i in old and len(m["content"]) > _OLD_RESULT else m
            for i, m in enumerate(messages)]


def _clean_step(text: str) -> str:
    """計画の1項目を短い1文にする(小さいモデルが入れがちなコードや中身は切り落とす)。"""
    text = str(text).split("```")[0].split("\n")[0]
    text = re.sub(r"\s+", " ", text).strip().rstrip(":：、, ")
    return text[:60]


def make_plan(*, task: str, model: ModelInfo, tools_text: str, generate: Callable[..., object], japanese: bool,
              facts: str = "") -> tuple[list[str], object]:
    """目標を手順に分ける。うまくいかなければ空の一覧(計画なしで進める)。戻り値は (手順, 生成の結果)。"""
    system = _PLAN_RULES + "\n\nThe agent's tools:\n" + tools_text + facts
    if japanese:
        system += "\n\nThe user writes in Japanese. Write the steps in Japanese."
    try:
        gen = generate(model, [{"role": "system", "content": system}, {"role": "user", "content": task}],
                       temperature=0.0, max_tokens=400, json_schema=PLAN_SCHEMA)
        data = json.loads(gen.text)
    except (BackendError, ValueError):
        return [], None
    steps = [s for s in (_clean_step(x) for x in (data.get("steps") or []) if str(x).strip()) if s and not _code_like(s)]
    return merge_file_steps(steps[:6], japanese), gen


_STEP_FILE = re.compile(r"[\w.-]+\.(?:py|pyw|js|mjs|ts|jsx|tsx|html?|css|json|md|txt|toml|ya?ml|csv|sh|bat|ps1|docx|xlsx|pptx)\b",
                        re.I)
_RUN_STEP = re.compile(r"実行|起動|確か|確認|テスト|試す|試し|動か|開い|開く|\b(?:run|start|test|check|verify|open|launch)\b", re.I)
_CODE_FILE = re.compile(r"\.(?:py|pyw|js|mjs|ts|jsx|tsx|html?)\b", re.I)
_PAGE_CODE = re.compile(r"JavaScript|\bJS\b|CSS|スタイル|\bstyles?\b", re.I)  # ページの中に書く部分(ファイル名の無い手順)


def merge_file_steps(steps: list[str], japanese: bool) -> list[str]:
    """同じファイルの中身を、いくつもの手順に分けた計画を、1つの手順にまとめる(1つのファイルは1手で書く)。

    小さいモデルは「app.py を作る」「app.py に天気の関数を作る」「app.py にルートを作る」と分けがちで、エージェントは
    1手で書き終えたファイルを手順ごとに作り直して、ページとかみ合っていた中身を壊していた(実際にあった)。
    実行・確かめの手順はまとめない。コードを作るのに、動かす手順が無ければ最後に足す。
    """
    merged: list[str] = []
    extra: dict[int, list[str]] = {}
    owner: dict[str, int] = {}  # ファイル名 → それを書く手順の位置
    for step in steps:
        files = list(dict.fromkeys(f.lower() for f in _STEP_FILE.findall(step)))
        pages = [f for f in owner if f.endswith((".html", ".htm"))]
        if (not files and _PAGE_CODE.search(step) and not _RUN_STEP.search(step) and len(pages) == 1
                and not any(f.endswith((".js", ".mjs", ".css")) for f in owner)):
            # 「JavaScript を追加して、時計を更新する」のような、ページの中身の手順: ページを書く手順で、あわせて書く
            # (別の手順にすると、<script> を何度も足して、同じ名前の関数が3つできていた。実際にあった)
            extra.setdefault(owner[pages[0]], []).append(step)
            continue
        if files and not _RUN_STEP.search(step):
            if all(f in owner for f in files):  # 前の手順で書くファイルに、中身を足すだけ
                first = owner[files[0]]
                rest = re.sub(rf"^\s*{re.escape(files[0])}\s*(?:に|へ|の|で|を)?\s*", "", step, flags=re.I) if japanese else step
                extra.setdefault(first, []).append(rest or step)
                continue
            for f in files:
                owner.setdefault(f, len(merged))
        merged.append(step)
    for i, more in extra.items():
        # どのファイルの中かを名前で書く(「同じファイルに」だけだと、ほかのファイルの指示にも「HTMLとCSSを同じファイルに
        # 書きます」と移り、app.py に HTML のページが書かれた。実際にあった)
        name = next((f for f, at in owner.items() if at == i), "")
        merged[i] += (f"(この {name} の中に、あわせて: {' / '.join(more)})" if japanese
                      else f" (in this same file, {name}, also: {'; '.join(more)})")
    if extra and owner and any(_CODE_FILE.search(f) for f in owner) and not any(_RUN_STEP.search(s) for s in merged):
        merged.append("作ったものを動かして確かめる" if japanese else "Run it and check the result")
    return merged


_CONTINUE = re.compile(r"続き|続けて|再開|仕上げて|完成させて|やり直して|もう一度|さっきの|前回の|"
                       r"\b(continue|resume|finish it|try again|go on)\b", re.I)
_BARE = re.compile(r"(作成|作って|作る|実行|やって|お願い|頼む|進めて|はい|ok|yes|go|do it)[^\n]{0,8}", re.I)


def is_continuation(task: str) -> bool:
    """前の依頼の続きを頼む言い方か(「続きから作成して」「作成して」「お願い」など)。"""
    return bool(_CONTINUE.search(task) or _BARE.fullmatch(task.strip()))


def with_earlier_requests(task: str, history: list[tuple[str, str]] | None) -> str:
    """続きの依頼なら、この会話のこれまでの依頼と前回の報告を目標に添える(計画や書く役のAIが、元の目的を忘れないように)。"""
    if not history or not is_continuation(task):
        return task
    asks = [u.strip()[:400] for u, _ in history[-4:] if u.strip() and not is_continuation(u)]
    lines = [f"- これまでの依頼: {a}" for a in asks]
    last = " ".join(history[-1][1].split())
    if last:
        lines.append("- 前回の報告(抜粋): " + last[:500])
    return task + "\n\n(続きの依頼です。この会話の流れ:\n" + "\n".join(lines) + ")"


def _code_like(step: str) -> bool:
    """計画の項目が、手順ではなくコードの1行になっているか(小さいモデルは、計画にコードを書いてしまうことがある)。"""
    if _CODE_LINE.match(step):
        return True
    return not re.search(r"[぀-ヿ一-鿿]", step) and bool(re.search(r"\w\(.*\)|[{};]|\s=\s", step))


def _example(japanese: bool, with_step: bool) -> str:
    lines = []
    for i, (thought, tool, args) in enumerate(_EXAMPLE["ja" if japanese else "en"], 1):
        item: dict = {"thought": thought}
        if tool == "answer":
            item.update(tool="answer", answer=args)
        else:
            if with_step:
                item["step"] = i
            item.update(tool=tool, args=args)
        lines.append(json.dumps(item, ensure_ascii=False))
    goal = "hello と表示する greet.py を作って実行して" if japanese else "make greet.py that prints hello and run it"
    return f"Example for a different goal (\"{goal}\"); use the names of the user's own goal:\n" + "\n".join(lines)


_MAKE = re.compile(r"作って|作成|作る|実装|書いて|追加して|\b(build|create|make|implement|write|add)\b", re.I)
_WRITES = ("write_file", "write_code", "edit_file", "append_file", "make_word", "make_excel", "make_slides")
REVIEW_SCHEMA = {"type": "object", "required": ["missing"],
                 "properties": {"missing": {"type": "array", "maxItems": 4, "items": {"type": "string"}}}}
_REVIEW_RULES = """You check whether an AI agent made everything the user asked for, before it reports.
Compare the user's request with the files as they are now. List only things the user explicitly asked for that are clearly
missing from the files (for example a value to show, a feature, a file). Do not list improvements, tests, style, or anything
the user did not ask for, and do not list things that are already in the files. Output JSON {"missing": [...]} with short
items in the user's language, or {"missing": []} when everything asked for is there."""


def review_missing(*, task: str, calls: list[dict], ctx: ToolContext, model: ModelInfo,
                   generate: Callable[..., object]) -> tuple[list[str], object | None]:
    """最終報告の前に、頼まれたのにまだ作っていないものを挙げさせる(小さいモデルは、一部だけ作って終えがち)。

    作る依頼で、ファイルを書いたときだけ行う。材料は依頼と、今のファイルの中身(作業の記録の言い分ではなく実物)。
    """
    if not _MAKE.search(task):
        return [], None
    paths = list(dict.fromkeys(c["detail"] for c in calls if c["name"] in _WRITES and c["ok"] and not c.get("skipped")))
    files = []
    for path in paths[-5:]:
        text = file_text(ctx, path, 2500)
        if text:
            files.append(f"=== {path} ===\n{text}")
    if not files:
        return [], None
    user = f"User's request:\n{task}\n\nFiles now:\n" + "\n\n".join(files)
    try:
        gen = generate(model, [{"role": "system", "content": _REVIEW_RULES}, {"role": "user", "content": user[:12000]}],
                       temperature=0.0, max_tokens=200, json_schema=REVIEW_SCHEMA)
        data = json.loads(gen.text)
    except (BackendError, ValueError):
        return [], None
    missing = [" ".join(str(m).split())[:80] for m in (data.get("missing") or []) if str(m).strip()]
    return [m for m in missing if grounded(m, task) and not _names_existing_file(m, ctx)][:4], gen


_FILE_ITEM = re.compile(r"[\w./\\-]+\.\w{1,5}")
_FILE_WORDS = re.compile(r"(?:の|を)?(?:作成|作る|作って|ファイル|書く|保存)|\b(?:create|file|write|save|the)\b|[\s、。:：()()「」]", re.I)


def _names_existing_file(item: str, ctx: ToolContext) -> bool:
    """やり残しとして挙がったものが、もうあるファイルの名前だけか(「average.py」と挙げていたが、作って動かしてあった)。"""
    names = _FILE_ITEM.findall(item)
    if not names or _FILE_WORDS.sub("", _FILE_ITEM.sub("", item)):
        return False
    try:
        return all((ctx.workspace / name).is_file() for name in names) if ctx.workspace is not None else False
    except OSError:
        return False


_CJK_RUN = re.compile(r"[一-鿿々]{2,}|[ァ-ヿ]{2,}")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
# どの依頼にも出てくる、それだけでは何を頼んだか分からない言葉(「風速の表示」の「表示」で、依頼にあると見なさない)
_GENERIC_JA = ("表示", "作成", "機能", "情報", "画面", "追加", "実装", "更新", "取得", "使用", "処理", "設定", "確認", "対応",
               "利用", "管理", "出力", "入力", "個人用", "個人", "詳細", "データ", "ページ", "ファイル", "アプリ", "ボタン",
               "リアルタイム", "デジタル", "サイネージ", "プログラム", "コード", "サーバー")
_GENERIC_EN = {"the", "and", "for", "with", "that", "this", "add", "make", "create", "show", "display", "page", "file", "app",
               "data", "info", "information", "feature", "function", "update", "button", "screen", "code", "server", "use"}


def _content_words(text: str) -> list[str]:
    for word in _GENERIC_JA:
        text = text.replace(word, " ")
    return _CJK_RUN.findall(text) + [w.lower() for w in _LATIN_WORD.findall(text) if w.lower() not in _GENERIC_EN]


def grounded(item: str, task: str) -> bool:
    """やり残しとして挙がったものが、依頼の文に出てくるものか(頼まれていない「風速」を足させていた。実際にあった)。

    一般的な言葉を除いて、依頼の言葉と2文字以上重なるか、3文字以下で依頼の1語に文字がすべて入っていれば(温度 と 温湿度)
    依頼にあると見なす。判断できる言葉が無いものは残す。
    """
    words = _content_words(item)
    if not words:
        return True
    asked = _content_words(task)
    for word in words:
        for have in asked:
            if word == have:
                return True
            if word.isascii() or have.isascii():  # 英語の言葉: 同じ語か、語の形が変わっただけ(delete と deleting)
                if word.isascii() and have.isascii() and min(len(word), len(have)) >= 4 and word[:5] == have[:5]:
                    return True
                continue
            if any(word[i:i + 2] in have for i in range(len(word) - 1)):
                return True
            if len(word) <= 3 and set(word) <= set(have):
                return True
    return False


_NO_RUN = re.compile(r"(起動|実行|動か)(は)?(しない|せず|しなくて|さない|さず)|\b(do not|don't|without) (run|start)", re.I)


def _told_not_to_run(task: str) -> bool:
    """「起動しないで」のように、動かさないよう頼まれているか。"""
    return bool(_NO_RUN.search(task))


_PAGE_FILE = re.compile(r"\.(?:html?|js|css)$", re.I)
_RESTARTS = ("stop_process", "start_process")  # 直すたびに繰り返すのが正しい道具(再起動)
_SERVE_COMMAND = re.compile(r"flask\s+run|uvicorn|gunicorn|waitress|http\.server", re.I)


def _unchecked_server(ctx: ToolContext, written_at: dict[str, int], ran: list[tuple[int, str]], always: bool = False) -> str:
    """作った Web サーバーのうち最後に書いたもの。ただし、最後に起動したあとにサーバーかページのファイルを変えたとき
    (または一度も起動していないとき)だけ。確かめが要らなければ空。always なら、起動したあとでも返す。"""
    if not ctx.allow_run:
        return ""
    servers = [path for path in written_at if is_server_file(ctx, path)]
    if not servers:
        return ""
    names = {Path(path).name for path in servers}
    last_run = max((n for n, text in ran if _SERVE_COMMAND.search(text) or any(name in text for name in names)), default=0)
    changed = max(at for path, at in written_at.items() if path in servers or _PAGE_FILE.search(path))
    return max(servers, key=written_at.__getitem__) if always or last_run < changed else ""


def _problem_line(output: str) -> str:
    """確かめた結果の、問題を表す1行(画面と報告の注意に出す)。"""
    lines = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("FAILED:")]
    error = next((line for line in reversed(lines) if re.match(r"[\w.]+(?:Error|Exception|Exit)\b", line)), "")
    for found in [next((line for line in lines if "エラー:" in line), ""), error] + [
            next((line for line in lines if mark in line), "") for mark in ("を開くと", "注意", "終了しました")]:
        if found:
            return found[:300]
    return lines[0][:300] if lines else ""


_CRASHED = re.compile(r"Traceback \(most recent call last\)|^\w+(?:Error|Exception): ", re.M)


def _crash_hint(output: str) -> str:
    """よくある、起動してすぐ止まるエラーの直し方(当てはまらなければ空)。"""
    m = re.search(r"overwriting an existing endpoint function: ([\w.]+)", output)
    if m:
        return (f" Two functions in the file are named {m.group(1)}: in Flask each function with @app.route needs its own "
                "name (the name becomes the endpoint). Delete the extra copy, or rename one of them.")
    return ""


_PAGE_RAN = pagerun.PAGE_RAN
_OUTSIDE_GUIDE = ("\nNOTE: the outside service (the weather API, etc.) did not answer in time, so the server answered with its "
                  "error message. This is not a mistake in the code, and editing the code will not fix it: do not change the "
                  "code for it. If the rest of the work is done, finish now with the answer tool, and tell the user that this "
                  "part shows 取得できませんでした until the service answers again.")


def judge_result(tool: str, output: str) -> tuple[bool, str]:
    """道具は動いたが、中のプログラムが失敗したか(終了コードが 0 以外)。失敗なら、直すよう促す文を先頭に付ける。

    外のサービスが答えなかっただけと分かったときは、コードを変えても直らないことを添える(「コードの誤りではありません」と
    伝えても、エラーの扱いを「改善」する編集を続けていた。実際にあった)。
    """
    ok, judged = _judge(tool, output)
    if ok and OUTSIDE_DOWN in output and (tool in ("check_url", "start_process") or tool in _RUNS):
        judged += _OUTSIDE_GUIDE
    return ok, judged


def _judge(tool: str, output: str) -> tuple[bool, str]:
    m = (_EXIT.search(output) or _QUICK_EXIT.search(output)) if tool in _RUNS else _QUICK_EXIT.search(output) if tool == "start_process" else None
    if tool == "open" and OPENED_PAGE in output:  # この PC の中のページを開いた: 中身を取得した結果で判定する
        checked = output.split(OPENED_PAGE, 1)[1].lstrip("\n")
        if checked.startswith("ERROR:"):
            return False, ("FAILED: the browser was opened, but the page itself could not be loaded (see below). Start the "
                           "server first, or fix the URL, then check it with check_url.\n" + output)
        ok, judged = judge_result("check_url", checked)
        if ok and NOT_OPENED in output:  # 開かずに中身だけ確かめた: 開き直しても同じなので、先へ進ませる
            return True, (output + "\n(Celestial stops this server when the request ends, so the page is not opened in a "
                          "browser; its content was checked above and it works. Do not open it again: if the goal is "
                          "reached, finish now with the answer tool.)")
        return (True, output) if ok else (False, judged + "\n(" + output.split(OPENED_PAGE, 1)[0].strip() + ")")
    if tool in ("make_excel", "make_word", "make_slides") and not output.startswith("ERROR"):  # 中身は結果のとおり
        return True, (output + "\n(This is the content of the file as it is now, with the formulas computed: the file is "
                      "made and checked. Do not open or check it again: if the goal is reached, finish now with the answer "
                      "tool.)")
    if tool == "check_url" and STALE_SERVER in output:  # 答えたのは、書き直す前のコードのサーバー
        return False, ("FAILED: this server was started before you changed its code (or its template), so it still runs the "
                       "old code: this result does not show your change. Run it again (run_file or start_process restarts it), "
                       "then check again.\n" + output)
    if tool in _CODE_WRITES and MARKUP_IN_PYTHON in output:  # Python のファイルに、HTML のページを書いた
        return False, ("FAILED: the file was saved, but it is not Python code: it is an HTML page (or CSS). edit_file cannot "
                       "fix this. Write the file again with write_code, as Python code only (for Flask: the routes return the "
                       "page with render_template('index.html'), and the page itself goes in templates/index.html).\n" + output)
    if tool in _CODE_WRITES and CODE_PROBLEM in output:  # 保存はしたが、動かす前から誤っている
        return False, ("FAILED: the file was saved, but its code has a mistake that stops it from running (see below). "
                       "Fix this file now (edit_file with a line that is in the file as old), then run it.\n" + output)
    if (tool in ("check_url", "start_process") or tool in _RUNS) and PAGE_PROBLEM in output and _PAGE_RAN in output:
        # ページの JavaScript を動かすと止まる・番号や undefined が出る(開いただけでは分からない)
        return False, ("FAILED: the page opened, but when its JavaScript runs it stops with an error or shows a wrong value "
                       "(see the problems below: the line in the page and what the page shows are given). Fix the page (the "
                       "id and class names the JavaScript looks for must be in the HTML, and the page must show text the user "
                       "can read), run the server again (it restarts), and check again.\n" + output)
    if (tool in ("check_url", "start_process") or tool in _RUNS) and PAGE_PROBLEM in output:  # ページとサーバーがかみ合わない
        return False, ("FAILED: the page opened, but it does not work with the server (see the problems below). Fix the page or "
                       "the server so they match, run the server again (it restarts), and check again.\n" + output)
    if (tool in _RUNS or tool in ("start_process", "check_url")) and ERROR_PAGE in output:  # 200 でも、中身はエラーの文だけ
        return False, ("FAILED: the server is running, but its page shows only a short error message instead of the page (see "
                       "below): the code catches an exception and returns just that text. Find the real error (the server's "
                       "output often shows it), fix the code, then run it again (it restarts) and check again. If the error "
                       "names an .html file, the template was not found: render_template takes the name inside the templates "
                       "folder ('index.html', not 'templates/index.html'), and the file must be in a folder named templates "
                       "next to the Python file. If the data comes from an outside service that cannot be reached, still return "
                       "the page and show 取得できませんでした only for that part.\n" + output)
    if (tool in _RUNS or tool == "start_process") and SLOW_MARK in output:  # 起動はしたが、ページの答えが返らない
        return False, ("FAILED: the server is running, but its page did not answer (see below). The page probably waits for an "
                       "outside service (a weather API, etc.) that is slow or unreachable: give that call timeout=5, catch the "
                       "exception and still return the page, showing 取得できませんでした for that part (better: return the "
                       "page at once and let its JavaScript fetch the data from a separate /api/... route). Then run it again "
                       "(it restarts) and check again.\n" + output)
    if (tool in _RUNS or tool in ("start_process", "check_url")) and NO_PAGE in output:  # ページを書いたのに / が 404
        return False, ("FAILED: the server is running, but it has no page at / (HTTP 404), although the page file named below "
                       "exists. Add a route for '/' that returns that page (Flask: @app.route('/') then def index(): return "
                       "render_template('index.html'), with render_template imported from flask), then run it again (it "
                       "restarts) and check again.\n" + output)
    if tool == "check_url" and OUTSIDE_DOWN in output:  # 外のサービスが答えないだけ: コードは正しく答えている
        return True, output
    error = (_STATUS_LINE.match(output) if tool == "check_url" else
             _PEEK_STATUS.search(output) if tool in _RUNS or tool == "start_process" else None)
    if error and error.group(1).startswith("5"):  # 動いてはいるが、ページを開くとサーバーの中でエラーになる
        hint = ""
        called = re.search(r"UndefinedError: '(\w+)' is undefined", output)
        if called and re.search(rf"\b{re.escape(called.group(1))}\s*\(", output):
            # テンプレートが Python の関数を呼んでいる(テンプレートからは見えない。直し方が分からず、同じ編集を繰り返していた)
            name = called.group(1)
            hint = (f" The template calls {name}(...), but a template cannot see the functions in the Python file. Register "
                    f"it: put @app.template_global() on the line right above def {name}( (edit_file with old \"def {name}(\" and "
                    f"new \"@app.template_global()\\ndef {name}(\"; if there is no def {name} yet, write that function first), "
                    "or call it in Python and pass the result to render_template.")
        elif "UndefinedError" in output:
            hint = (" The template uses a name or a key that the Python code does not give it: pass it from the Python file "
                    "(for example render_template('index.html', now=now, weather=weather)) or make the template use only what is "
                    "passed. A clock is better done with JavaScript in the page.")
        elif "TemplateNotFound" in output:
            hint = " Put the HTML file in a folder named templates next to the Python file, or fix the name in render_template."
        elif re.search(r"ReadTimeout|ConnectTimeout|ConnectionError|timed out|URLError|Max retries|getaddrinfo|NameResolution",
                       output):
            hint = (" The error comes from calling an outside service that did not answer: catch the exception (try/except "
                    "around the call, with timeout=5) and still return the page, showing 取得できませんでした for that part.")
        return False, (f"FAILED: the server is running, but opening the page gives HTTP {error.group(1)} (an error inside the "
                       "server program; the error and the server's last output are below). Fix the code, then run it again "
                       f"(it restarts) and check again.{hint}\n" + output)
    if PORT_BUSY in output:  # ポートを、ほかのプログラムが使っている(コードの誤りではない)
        return False, ("FAILED: the port is already used by another program (see below). That program is not part of this work: "
                       "never stop it. If the user did not ask for this port number, change the port in the code to the free "
                       "number shown below and run it again. If the user asked for this exact number, keep it and tell the user "
                       "in the final answer.\n" + output)
    if tool in _RUNS and ENDED_EARLY in output:  # サーバーやウィンドウのアプリが、終了コード 0 でもすぐに終わった
        if _CRASHED.search(output):  # エラーで止まった(「動き続ける作りになっていない」のではない)
            return False, ("FAILED: this program should keep running (a server keeps waiting for requests), but it stopped at "
                           "once with an error (the traceback is below). Fix that error in the code, then run it again."
                           + _crash_hint(output) + "\n" + output)
        return False, ("FAILED: this program should keep running (a server keeps waiting for requests, a window app stays open), "
                       "but it ended right away. Fix the code so that it keeps running, then run it again.\n" + output)
    if not m or m.group(1) == "0":
        return True, output
    if tool in _RUNS and "nothing to commit, working tree clean" in output:  # git: 変えたものがない = すでにすべてコミット済み
        return True, output + "\nNOTE: git found nothing new to commit: everything is already committed. This is not an error."
    hint = _crash_hint(output)
    missing = re.search(r"cannot import name '(\w+)' from '([\w.]+)'", output)
    if hint:
        pass
    elif missing:  # 使う側が求める名前が、ファイルに無い(書き直しで消していた。実際にあった)
        name, module = missing.group(1), missing.group(2)
        hint = (f" The program imports {name} from {module}, but {module}.py has no {name} now. Read {module}.py and the "
                f"file that imports it, and keep every name other files import (if a rewrite removed {name}, add it back, "
                "or bring back the earlier content with restore_file).")
    elif PORT_IN_USE.search(output):
        hint = " The port is already used by another program: stop that program first (keep the port number the user asked for)."
    elif tool == "run_python" and re.search(r"ModuleNotFoundError|No module named|ImportError", output):
        hint = " To run a script file you wrote, use run_file with its path instead of importing it."
    elif re.search(r"No such file|can't open file|見つかりません|cannot find|is not recognized|認識されていません", output, re.I):
        hint = " Check the path or the command name (list_files shows what exists)."
    return False, f"FAILED: the program exited with code {m.group(1)}. Read the error, fix the cause, then try again.{hint}\n{output}"


def _environment(names: set[str]) -> str:
    """この PC のこと(小さなモデルが Unix の書き方を Windows で使って失敗しないように)。"""
    files = ("For files and folders, prefer the file tools (list_files, read_file, make_folder, move_file, copy_file, "
             "delete_file) over shell commands.")
    if "run_command" not in names:
        return ""
    if sys.platform == "win32":
        unix = "" if shutil.which("ls") else "ls, cat, mv, cp, rm and touch do not exist there: use dir, type, move, copy, del. "
        return ("\n\nThis PC runs Windows. run_command uses cmd.exe: quote with \"double quotes\" ('single quotes' do not "
                "work there). " + unix + files)
    return f"\n\nThis PC runs {'macOS' if sys.platform == 'darwin' else 'Linux'}. run_command uses /bin/sh. " + files


# 何も変えなかった呼び出しの結果(報告の材料にしない。old と new が同じ編集の失敗を見て、報告を書くAIが「URL が不完全で、
# 修正が必要です」と書いた。実際にあった)
_NO_EFFECT = ("ALREADY DONE:", "ALREADY SEEN:", "FAILED: this exact call already failed", "ERROR: old と new が同じ",
              "ERROR: " + NO_CHANGE[:20])


def _had_effect(call: dict) -> bool:
    """作業の記録として報告に使う呼び出しか(動かさなかった繰り返しや、何も変えなかった編集は除く)。"""
    return not call.get("skipped") and not str(call.get("output", "")).startswith(_NO_EFFECT)


_UNFINISHED_WORDS = (r"(?:未完了|未完成|未解決|やり残し|残っている(?:作業|点|こと)|まだ(?:終わって|できて)いない(?:点|こと)?|"
                     r"修正が必要な(?:点|所|箇所)|今後の課題|unfinished|not (?:yet )?(?:done|finished)|still to do|"
                     r"remaining (?:work|issues))")
# 見出し: 「未完了な点:」「**未完了な点:**」「### 未完了な点」(「未完了な点はありません。」のような文は見出しではない)
_UNFINISHED_HEAD = re.compile(rf"\s*(?:#{{1,4}}\s*)?(?:\*\*)?\s*{_UNFINISHED_WORDS}[^:：\n]{{0,12}}?(?:\*\*)?\s*[:：](?:\*\*)?"
                              rf"|\s*#{{1,4}}\s*(?:\*\*)?\s*{_UNFINISHED_WORDS}[^\n]*$", re.I)
_ITEM = re.compile(r"\s*(?:[-*•]\s+|・\s*|\d+[.)．]\s*)")
# 終わった時点の確かめ(サーバーを開き、ページの JavaScript を動かして表示を見る)で、確かめた事柄
_CHECKED_TOPIC = re.compile(r"API|URL|天気|気温|温度|湿度|時計|時刻|時間|日付|サーバー|ページ|画面|表示|エラー|取得|接続|"
                            r"JavaScript|HTML|CSS|Flask|コード|server|page|weather|temperature|humidity|clock", re.I)


def _drop_unfinished(text: str) -> str:
    """終わった時点の確かめで問題が無かったのに、報告が書いた「未完了な点」のうち、確かめた事柄(API・ページ・表示など)に
    ついての項目を除く(「天気APIのURLが不完全です。修正が必要です」と書いたが、ページは天気・気温・湿度・時計を正しく
    出していた。実際にあった)。確かめていない事柄の項目は残す。項目が残らなければ、見出しも除く。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        head = _UNFINISHED_HEAD.match(lines[i])
        if not head:
            out.append(lines[i])
            i += 1
            continue
        inline = lines[i][head.end():].strip()  # 「未完了な点: 天気APIの…」のように、見出しの行に中身があることも
        items, j = [], i + 1
        while j + 1 < len(lines) and not lines[j].strip() and _ITEM.match(lines[j + 1]):  # 見出しと項目のあいだの空行
            j += 1
        while j < len(lines) and (_ITEM.match(lines[j]) or (lines[j][:1] in (" ", "\t", "　") and lines[j].strip())):
            items.append(lines[j])
            j += 1
        below = ""
        if not inline and not items and j < len(lines) and lines[j].strip():  # 見出しのすぐ下の1行が中身(「ありません。」など)
            below = lines[j]
            j += 1
        kept = [item for item in items if not _CHECKED_TOPIC.search(item)]
        body = inline or below.strip()
        keep_body = bool(body) and not _CHECKED_TOPIC.search(body)
        if kept or keep_body:
            out.append(lines[i] if keep_body or not inline else lines[i][:head.end()].rstrip())
            if below:
                out.append(below)
            out += kept
        i = j
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _report_prompt(task: str, calls: list[dict], stopped: str, japanese: bool, now: str = "",
                   clean: bool = False) -> list[Message]:
    """打ち切ったときなどの報告: 作業の記録だけを材料に、普通の文章で書かせる(JSON の真似をさせない)。

    now は、終わった時点で Celestial が実際に確かめた今の状態(途中の失敗より、こちらが正しい)。clean は、その確かめで
    問題が無かったこと(未完了の点を書かせない)。
    """
    entries: list[list] = []  # [操作, 結果, 続けて行った回数]
    for c in calls:
        if not _had_effect(c):  # 動かさなかった繰り返しや、何も変えなかった編集は、作業ではない
            continue
        head = f"[{'OK' if c['ok'] else 'FAILED'}] {c['label']}: {c['detail']}"
        if entries and entries[-1][0] == head:  # 同じ確かめの繰り返しなどは1つにまとめる(報告が同じ行を3つ並べていた)
            entries[-1][2] += 1
            continue
        entries.append([head, c["output"][:300].strip(), 1])
    log = [f"{i}. {head}" + (f" (the same call {times} times in a row)" if times > 1 else "") + f"\n   result: {result}"
           for i, (head, result, times) in enumerate(entries, 1)]
    system = ("You write the final report of an AI agent's work for the user, as plain text (no JSON). Use only the facts in "
              "the work log and the state at the end. Say briefly what was made (with file paths), how to use it (for a "
              "server: the command that starts it and the URL to open), and what is still unfinished, only if the log or "
              "the state at the end shows that something is missing or broken. Summarise: do not list the steps of the log "
              "one by one. Something that failed first and then succeeded later is done: report the final state. Do not add "
              "problems, requirements or settings that the log and the state at the end do not show (for example, do not "
              "say that an API key is needed when the API answered with data). Do not mention the fixes that were made "
              "automatically while saving a file (newlines, indentation): they are not part of the result. Do not paste "
              "code, file contents or API responses (the user has the files), and do not give file sizes or character "
              "counts.")
    if clean:
        system += (" The checks at the end found nothing missing or broken: do not write any unfinished part, and do not say "
                   "that something is incomplete or must still be fixed.")
    if japanese:
        system += " Write in natural Japanese."
    user = f"Goal: {task}\n\nWork log:\n" + ("\n".join(log) or "(no actions)") + (f"\n\nNote: {stopped}" if stopped else "")
    if now:
        user += f"\n\nState at the end (checked just now by opening it; this is the truth, not the earlier failures):\n{now}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _without_code(text: str, longest: int = 4) -> str:
    """報告から、長いコードのかたまり(```で囲んだ、中身が longest 行を超えるもの)を除く(打ち切ったときの報告で、実際の
    ファイルとは違うコードを貼っていた)。短いもの(起動のコマンドなど)はそのまま残す。"""
    out: list[str] = []
    block: list[str] | None = None

    def drop(block: list[str]) -> None:
        """長いかたまりを除く。それを紹介する「…：」の行も除く(「以下の通りです：」だけが残っていた)。"""
        while out and not out[-1].strip():
            out.pop()
        introduced = bool(out) and out[-1].rstrip().endswith((":", "："))
        if introduced:
            out.pop()
        body = next((b.strip() for b in block[1:] if b.strip()), "")
        if not introduced and not body.startswith(("{", "[")):  # JSON(API の答えなど)は、ファイルにあるものではない
            out.append(block[0][:len(block[0]) - len(block[0].lstrip())] + "(コードはファイルを見てください)")
        out.append("")

    for line in text.split("\n"):
        if block is None:
            if line.strip().startswith("```"):
                block = [line]
            else:
                out.append(line)
            continue
        block.append(line)
        if line.strip() == "```":  # かたまりの終わり
            if len(block) - 2 <= longest:
                out += block
            else:
                drop(block)
            block = None
    if block is not None:  # 閉じていないかたまり
        if len(block) - 1 <= longest:
            out += block
        else:
            drop(block)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _plain_answer(text: str) -> str | None:
    """報告が JSON の形になってしまったときは、中の文章を取り出す。"""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return stripped or None
    try:
        data = json.loads(stripped)
    except ValueError:
        m = re.search(r'"answer"\s*:\s*"((?:\\.|[^"\\])*)"', stripped)
        try:
            return json.loads(f'"{m.group(1)}"').strip() or None if m else None
        except ValueError:
            return m.group(1).strip() or None
    if isinstance(data, dict):
        value = data.get("answer") or data.get("report") or data.get("text")
        return str(value).strip() if value else None
    return None


def _log_report(calls: list[dict], stopped: str) -> str:
    """AIが報告を書けなかったときの、記録からそのまま作る報告。"""
    done = [f"- {c['label']} {c['detail']}".rstrip() + ("" if c["ok"] else "(失敗)") for c in calls if _had_effect(c)]
    head = stopped or "作業の記録です。"
    return head + ("\n" + "\n".join(done) if done else "\n(道具は使われませんでした)")


def run_agent(*, task: str, role_name: str, model: ModelInfo, ctx: ToolContext, max_steps: int,
              generate: Callable[..., object], history: list[tuple[str, str]] | None = None,
              on_event: Callable[..., None] | None = None, on_token: Callable[[str], None] | None = None,
              personal: str = "", plan: bool = True, review: bool = True) -> dict:
    """エージェントを動かす。戻り値は {answer, calls, tokens, gen_latency, steps, stopped, plan}。

    generate(model, messages, **kw) は1回の生成を行う関数(管理AIが渡す)。on_event(種類, **内容) は進行の通知。
    personal は利用者の指示と覚えていること(指示文の最後に足す)。plan=False なら計画を立てずに進める。
    """
    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, **data)

    ctx.task = task  # 道具が依頼の文を見て決めること(頼まれていないのに、作ったファイルをアプリで開かない など)
    ctx.by_agent = True  # 読まずに書き直させない など、AIの誤りを防ぐ決まりを使う
    ctx.started = ctx.started or time.time()
    tools = available_tools(ctx)
    tools_text = "\n".join(f"- {t.name}: {t.description}" for t in tools)
    japanese = bool(re.search(r"[぀-ヿ一-鿿]", task))
    tokens, gen_latency = 0, 0.0

    facts = knowhow.hints(task)  # 天気など、キーのいらない公開 API の使い方(関係する話題のときだけ)
    steps: list[str] = []
    if plan:
        steps, gen = make_plan(task=task, model=model, tools_text=tools_text, generate=generate, japanese=japanese, facts=facts)
        if gen is not None:
            tokens += gen.tokens
            gen_latency += gen.latency_s
        if steps:
            emit("agent_plan", steps=steps)

    schema = step_schema(tools, len(steps))
    system = (_RULES + f"\n\nKind of task: {ROLES[role_name].description}\n\nTools:\n" + tools_text
              + "\n\nYou can NOT do these: " + "; ".join(unavailable_notes(ctx) or ["(nothing is restricted)"])
              + ". If the goal needs one of them, do not pretend: say in the answer that it was not done and why.")
    names = {t.name for t in tools}
    system += _environment(names) + facts
    if {"make_folder", "write_file", "run_file"} <= names:
        system += "\n\n" + _example(japanese, bool(steps))
    if steps:
        system += "\n\nPlan (set \"step\" to the number you are working on):\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    if ctx.workspace is not None:
        system += f"\n\nThe workspace folder is {ctx.workspace}. Paths in the tools are relative to it."
        if ctx.anywhere:
            system += " You have full access to this PC: absolute paths anywhere also work."
    if japanese:  # 小さいモデルは英語で報告しがちなので、言語を明示する
        system += "\n\nThe user writes in Japanese. Write the thoughts and the final answer in natural Japanese."
    system += personal
    messages: list[Message] = [{"role": "system", "content": system}]
    for user_text, reply in history or []:
        messages += [{"role": "user", "content": user_text[:1500]}, {"role": "assistant", "content": reply[:1500]}]
    messages.append({"role": "user", "content": task})

    calls: list[dict] = []
    same: dict[str, int] = {}
    done_at: dict[str, tuple[int, int, str]] = {}  # 呼び出し → (成功した手の番号, そのときの状態の番号, 結果)
    failed_at: dict[str, tuple[int, int]] = {}
    state = 0  # ファイルを変えたり、プログラムを動かしたりするたびに増える(間に変化があれば、同じ呼び出しでも結果が変わりうる)
    answer: str | None = None
    failures, redundant, stopped, ended = 0, 0, "", ""
    observed: dict[str, int] = {}  # 最後に何かを変えてから、様子を見るだけの同じ呼び出しをした回数
    reviewed = not review  # やり残しの確認は1回だけ
    servers_checked = False  # 動いているサーバーの確かめ(直させるのは1回だけ)
    final_checks = 0         # 報告の前に、作ったサーバーを Celestial が自分で起動して確かめた回数(2回まで)
    check_failed = ""        # その確かめで分かった問題(直す手数が残っていなかったときに、報告の先頭で伝える)
    verified_at = 0          # 作ったものを動かす・開くかして、最後に成功した手の番号
    last_check_ok = True     # 最後に動かす・開くかした結果(失敗のあとで止めるのは、直すための停止)
    claimed_done = False     # 1つ前の手の考えが「完了しました」だったか
    written_at: dict[str, int] = {}  # 書いた・直したファイル → 最後に書いた手の番号
    ran: list[tuple[int, str]] = []  # 動かした手の番号と、動かしたもの(ファイルの道のりかコマンド)

    def check_server(path: str, number: int) -> tuple[str, str]:
        """作ったサーバーを自分で起動し、ページを開いて確かめる。問題があれば (AIに渡す文, その元の結果)、なければ空。"""
        emit("agent_step", n=number, thought="報告の前に、作ったサーバーを起動してページを確かめます", tool="run_file",
             label=TOOLS["run_file"].label, detail=path, step=0)
        try:
            raw, ok = execute(ctx, "run_file", {"path": path}), True
        except ToolError as e:
            raw, ok = f"ERROR: {e}", False
        judged = raw
        if ok:
            ok, judged = judge_result("run_file", raw)
        port = re.search(r"ポート (\d+) で接続を受け付けています", raw) if ok else None
        if port:  # 開いて中身(読み込む JavaScript と、呼ぶ API)まで確かめる
            try:
                raw, ok = execute(ctx, "check_url", {"url": f"http://localhost:{port.group(1)}/"}), True
            except ToolError as e:
                raw, ok = f"ERROR: {e}", False
            judged = raw
            if ok:
                ok, judged = judge_result("check_url", raw)
        emit("agent_result", n=number, ok=ok, output=judged[:1500])
        calls.append({"name": "run_file", "label": TOOLS["run_file"].label, "detail": path, "ok": ok, "output": raw[:400],
                      "thought": "報告の前の確かめ(Celestial が自分で起動)", "step": 0})
        ran.append((number, path))
        return ("", "") if ok else (judged, raw)

    rescued = False          # 行き詰まったときの立て直し(1回だけ)

    def rescue(number: int) -> str:
        """同じ失敗が続いて打ち切る前に1回だけ: 作ったサーバーを起動して開き、今の本当の状態を見せて、そこから続けさせる
        (小さいAIは、うまくいかない編集を繰り返しがち。動かした結果は、何を直せばよいかをはっきり示す)。"""
        nonlocal final_checks
        if final_checks >= 2 or _told_not_to_run(task) or number >= max_steps - 3:
            return ""
        target = _unchecked_server(ctx, written_at, ran, always=True)
        if not target:
            return ""
        final_checks += 1
        problem, _ = check_server(target, number)
        if problem:
            return (f"STUCK: the same kind of failure kept repeating, so stop repeating it. Celestial started {target} and "
                    f"opened its page to show the real state now:\n{problem[:2500]}\nFix what this shows (read the file first "
                    "to get the exact text), run it again and check it.")
        return (f"STUCK: the same kind of failure kept repeating, so stop repeating it. Celestial started {target} and opened "
                "its page: it works now (the page, its JavaScript and the API it calls). If the goal is reached, finish now "
                "with the answer tool; otherwise do the next unfinished step in a different way.")

    broken: list[str] = []   # 報告の時点で、ページを開くとエラーになるサーバー
    now = ""                 # 繰り返しや打ち切りで終わったときの、今の状態(報告を書くAIに渡す)
    halted: list[tuple[str, str]] = []  # 依頼の終わりに止めたサーバーの、動かし直すための (コマンド, 言い方)
    clean = False            # その確かめで、問題が無かった(報告に「未完了」を書かせない)

    def before_report(number: int) -> str:
        """報告の前の確かめ。まだ報告させないときは、AIに渡す続けさせる文。報告してよければ空。

        1) 作ったサーバーを、最後に変えたあと起動していなければ、自分で起動してページを開く
        2) 動いているサーバーのページを開く  3) 頼まれたのにまだ無いもの(AIの確認と、表示画面の決まった確かめ)
        """
        nonlocal final_checks, servers_checked, check_failed, broken, reviewed, tokens, gen_latency
        target = "" if final_checks >= 2 or _told_not_to_run(task) else _unchecked_server(ctx, written_at, ran)
        if target:  # 作ったサーバーを、最後に変えたあと(または一度も)起動していない: 自分で起動して確かめる
            final_checks += 1
            problem, raw = check_server(target, number)
            if problem and not servers_checked and number < max_steps - 2:
                servers_checked = True  # 直させるのは1回だけ(直したあとは、もう1回だけ確かめる)
                emit("agent_review", missing=[_problem_line(raw)], about="server")
                return (f"NOT FINISHED YET. Before your report, Celestial started {target} and opened its page, and it does "
                        f"not work yet:\n{problem[:2500]}\nFix it, run it again (it restarts) and check it with check_url, "
                        "then report.")
            check_failed = raw
        broken = server_problems(ctx)  # 「表示されました」と報告する前に、動いているサーバーを実際に開く
        if broken and not servers_checked and number < max_steps - 2:
            servers_checked = True
            emit("agent_review", missing=broken, about="server")
            return ("NOT FINISHED YET. A server you started does not work yet (its page gives an error, or the page's "
                    "JavaScript stops or shows a wrong value when it runs): " + "; ".join(broken) + ". Read the error (process_output shows the server's output and "
                    "traceback), fix the code, run it again (it restarts) and check it with check_url before you report.")
        if not reviewed and number < max_steps - 3:
            reviewed = True
            missing, rgen = review_missing(task=task, calls=calls, ctx=ctx, model=model, generate=generate)
            if rgen is not None:
                tokens += rgen.tokens
                gen_latency += rgen.latency_s
            # 表示画面で決まって要るもの(動く時計・天気の文字)は、AIに聞かずにファイルの中身で確かめる
            shown = {path: file_text(ctx, path, 30_000) for path in written_at}
            gaps = knowhow.display_gaps(task, shown)
            missing = [m for m in missing if not knowhow.already_shown(m, shown, task)]  # ページがもう出しているもの(天気など)は除く
            missing = (gaps + [m for m in missing if m not in gaps])[:4]
            if missing:  # 頼まれたのに、まだ無いものがある: 報告せずに続けさせる
                emit("agent_review", missing=missing)
                return ("NOT FINISHED YET. The user also asked for: " + "; ".join(missing) + ". Add these now with the tools "
                        "and check the result again. If one of them truly cannot be done, say why in the final answer.")
        return ""

    try:
        for number in range(1, max_steps + 1):
            gen = generate(model, _compact(messages), temperature=0.0, max_tokens=2500, json_schema=schema)
            tokens += gen.tokens
            gen_latency += gen.latency_s
            try:
                step = json.loads(gen.text)
            except ValueError:
                step = None
            if not isinstance(step, dict):
                answer = _plain_answer(gen.text)  # JSON にならなかった出力は、そのまま報告として扱う
                break
            tool = str(step.get("tool"))
            thought = str(step.get("thought") or "").strip()[:300]
            done_claim = tool != "answer" and bool(_DONE_THOUGHT.search(thought) or _WORKS_THOUGHT.search(thought))
            if done_claim and claimed_done and last_check_ok and verified_at > max(written_at.values(), default=0):
                # 確かめ終えたあとで、2手続けて「完了しました」と言いながら道具を使っている(小さいAIは、ここでファイルを
                # 動かして壊しがち): その手は行わず、報告の前の確かめをして、足りないものがなければここで終える
                nudge = before_report(number)
                if nudge:
                    messages += [{"role": "assistant", "content": gen.text}, {"role": "user", "content": nudge}]
                    claimed_done = False
                    continue
                ended = ("The agent said the work was finished and it had been checked, so the work was ended here "
                         "(more changes after the check could break the checked result).")
                break
            claimed_done = done_claim
            if tool == "answer":
                answer = str(step.get("answer") or "").strip() or None
                nudge = before_report(number)
                if nudge:
                    messages += [{"role": "assistant", "content": gen.text}, {"role": "user", "content": nudge}]
                    answer = None
                    continue
                break

            args = step.get("args") if isinstance(step.get("args"), dict) else {}
            label = TOOLS[tool].label if tool in TOOLS else tool
            detail = summarize_call(tool, args)
            try:
                plan_step = int(step.get("step") or 0)
            except (TypeError, ValueError):
                plan_step = 0
            plan_step = plan_step if 1 <= plan_step <= len(steps) else 0
            if (tool == "stop_process" and written_at and last_check_ok and verified_at > max(written_at.values())
                    and not _MORE_WORK.search(thought) and not _ASKS_TO_STOP.search(task)
                    and not (steps and 0 < plan_step < len(steps)) and running_server(ctx, str(args.get("name") or ""))):
                # 変えたあとで確かめて成功したサーバーを、続きの作業のためでもなく止めようとしている = 終わったつもりの
                # 後片付け(止めたあとでもう一度開いて失敗し、起動し直しては止める、を繰り返しがち。実際にあった)。
                # 止めずに報告の前の確かめをして、足りないものがなければ終える(依頼が終わると、Celestial が止める)
                nudge = before_report(number)
                if nudge:
                    messages += [{"role": "assistant", "content": gen.text}, {"role": "user", "content": nudge}]
                    continue
                ended = ("The work was checked after the last change and it worked. Then the agent began to stop the server "
                         "it had checked (Celestial stops it when the request ends), so the work was ended here.")
                break
            emit("agent_step", n=number, thought=thought, tool=tool, label=label, detail=detail, step=plan_step)

            key = json.dumps([tool, args], sort_keys=True, ensure_ascii=False)
            group = TOOLS[tool].group if tool in TOOLS else ""
            fixed = tool not in _VOLATILE  # 間に変化がなければ、同じ呼び出しは同じ結果になる道具
            skipped, saved = False, False
            if fixed and key in done_at and done_at[key][1] == state:
                # 成功したことの繰り返し(小さいモデルは、終わったあとも確かめ直しを続けがち): 動かさずに、終わっていると知らせる
                turn, _, before = done_at[key]
                again = f" Its result was:\n{before}" if number - turn > _KEEP_FULL else ""  # 古い結果は縮めて渡しているので、もう一度見せる
                output, ok, skipped = (f"ALREADY DONE: this exact call already succeeded at turn {turn}, and nothing has changed "
                                       "since, so it was not run again. Do not repeat finished work: if the goal is reached, finish "
                                       "now with the answer tool; otherwise do the next unfinished step." + again), True, True
            elif fixed and key in failed_at and failed_at[key][1] == state:
                output, ok, skipped = (f"FAILED: this exact call already failed at turn {failed_at[key][0]}, and nothing has changed "
                                       "since, so it would fail the same way. Fix the cause first, or try a different way."), False, True
                hint = code_hint(ctx, str(args.get("path") or "")) if tool in ("edit_file", "append_file", "run_file") else ""
                if hint:  # 直せないまま同じことを繰り返している: そのファイルの今の誤りと、そのまま使える直し方をもう一度見せる
                    output += "\nThe file still has this mistake (fix this first):\n" + hint
                elif tool == "edit_file":  # old が見つからないまま同じ編集を繰り返している: その関数の今の全体を見せる
                    shown = old_hint(ctx, str(args.get("path") or ""), str(args.get("old") or ""))
                    if shown:
                        output += "\n" + shown
            elif tool in _OBSERVE and observed.get(key, 0) >= _looks_allowed(tool, done_at.get(key, (0, 0, ""))[2]):
                # 何も変えないまま、同じものを何度も見ている(画面撮影の繰り返しなど): 新しいことは分からないので、先へ進ませる
                times = "once" if observed[key] == 1 else f"{observed[key]} times"
                output, ok, skipped = (f"ALREADY SEEN: you made this exact call {times} since the last change and saw "
                                       "the result. Looking again shows nothing new: do the next unfinished step, or finish now "
                                       "with the answer tool."), True, True
            else:
                try:
                    output, ok = execute(ctx, tool, args), True
                except ToolError as e:
                    output, ok = f"ERROR: {e}", False
                if ok:
                    ok, output = judge_result(tool, output)
                busy_port = re.search(r"ポート (\d+) は(?:、起動する前から)?" + re.escape(PORT_BUSY), output) if not ok else None
                if busy_port:  # 番号を頼まれたかどうかは依頼の文で分かる: どうするかを、はっきり伝える
                    n = busy_port.group(1)
                    output += (f"\nThe user asked for port {n} in the request: keep it, do not stop the other program, and tell the "
                               "user about it in the final answer." if re.search(rf"(?<!\d){n}(?!\d)", task) else
                               f"\nThe user did not ask for port {n}: change the port number in the code to the free one shown "
                               "above, then run it again.")
                if not ok and tool == "check_url" and STOPPED in output:  # 自分で止めたサーバーを開いた
                    output += ("\nYou stopped this server yourself with stop_process, so nothing answers now. "
                               + ("It was checked after your last change and it worked, so the work is done: finish now with "
                                  "the answer tool (do not start it again just to check it)."
                                  if last_check_ok and verified_at > max(written_at.values(), default=0) else
                                  "Run it again (it starts again), then check it."))
                if not ok and tool == "run_file" and args.get("path"):  # 動かすと止まった: 動かす前に分かる誤りも見せる
                    hint = code_hint(ctx, str(args["path"]))
                    if hint and hint not in output:
                        output += "\nThe file has this mistake (fix this first):\n" + hint
                saved = group == "write" and CODE_PROBLEM in output  # 誤りの注意つきでも、ファイルは変わっている
                if tool in _CODE_WRITES and (ok or saved) and args.get("path"):
                    written_at[str(args["path"])] = number
                if tool in _RUNS or tool == "start_process":
                    ran.append((number, str(args.get("path") or args.get("command") or args.get("code") or "")))
                if ok and tool in _CHECKS:
                    verified_at = number
                if tool not in _OBSERVE and (group == "run" or ((ok or saved) and group in ("write", "pc"))):
                    state += 1  # 変えたもの・動かしたものがある(失敗した実行も、途中まで何かを変えたかもしれない)
                    observed.clear()
                if ok:
                    done_at[key] = (number, state, output[:3000])
                    failed_at.pop(key, None)
                else:
                    failed_at[key] = (number, state)
            if tool in _CHECKS:
                last_check_ok = ok
            redundant = redundant + 1 if skipped and ok else 0
            if skipped and ok and tool == "check_url":
                # 成功したページを、何も変えないまま開き直そうとした = 確かめ終えている(小さいモデルは、終えたあとも開き直しを
                # 続けがち。実際にあった): 報告の前の確かめに進む
                redundant = max(redundant, 2)
            if tool in _OBSERVE and ok:
                observed[key] = observed.get(key, 0) + 1
            failures = 0 if ok or saved else failures + 1  # 誤りの注意つきでも、保存できたなら前へ進んでいる
            calls.append({"name": tool, "label": label, "detail": detail, "ok": ok, "output": output[:400], "thought": thought,
                          "step": plan_step, **({"skipped": True} if skipped else {})})
            emit("agent_result", n=number, ok=ok, output=output[:1500])
            messages += [{"role": "assistant", "content": gen.text},
                         {"role": "user", "content": f"[{tool} result]\n{output}\n\nContinue with the next step, or finish with the answer tool."}]
            # 同じ呼び出しで同じ結果が続く = 行き詰まり(直してから動かし直すのは、結果が変わるので続けられる)
            outcome = key + "\x00" + output[:2000] + (f"\x00{len(ran)}" if tool in _RESTARTS else "")
            same[outcome] = same.get(outcome, 0) + 1
            if failures >= _MAX_FAILURES or (same[outcome] >= 3 and not ok):
                fresh = "" if rescued else rescue(number)
                if fresh:  # 打ち切る前に1回だけ: 今の本当の状態を見せて、そこから続けさせる
                    rescued = True
                    failures = 0
                    same.clear()
                    messages[-1]["content"] += "\n\n" + fresh
                    continue
                stopped = "うまく進まない状態が続いたため、途中で打ち切りました。"
                break
            if (redundant >= 2 or same[outcome] >= 3) and not last_check_ok:
                # 書き込みなどの成功を繰り返していても、そのあとの実行・確かめは失敗したまま = 行き詰まり(終わったのではない。
                # 同じ書き直しと失敗を繰り返して「成功の繰り返し」として終え、報告が「テストが通りました」だった。実際にあった)
                fresh = "" if rescued else rescue(number)
                if fresh:
                    rescued = True
                    failures = redundant = 0
                    same.clear()
                    messages[-1]["content"] += "\n\n" + fresh
                    continue
                stopped = "うまく進まない状態が続いたため、途中で打ち切りました。"
                break
            if redundant >= 2 or same[outcome] >= 3:  # 成功したことを繰り返している = もう終わっている(失敗ではないので注意は付けない)
                nudge = before_report(number) if number < max_steps - 2 else ""
                if nudge:  # 終わったつもりでも、確かめると足りないものがある: 報告の前と同じく、続けさせる
                    messages[-1]["content"] += "\n\n" + nudge
                    redundant = 0
                    same.clear()
                    continue
                ended = "The agent was repeating work that had already succeeded, so the work was ended here."
                break
        else:
            stopped = f"手数の上限({max_steps} 手)に達したため、途中で打ち切りました。"
        if final_checks < 2 and not _told_not_to_run(task):  # 途中で終わったときも、作ったサーバーが動くかを確かめて伝える
            target = _unchecked_server(ctx, written_at, ran)
            if not target and answer is None and not serving(ctx):
                # 作ったサーバーを止めたあとで終わった(打ち切りなど): 報告に今の本当の状態を書けるよう、起動して確かめる
                target = _unchecked_server(ctx, written_at, ran, always=True)
            if target:
                final_checks += 1
                check_failed = check_server(target, len(calls) + 1)[1]
        if answer is None and ctx.processes:  # 繰り返しや打ち切りで終わった: 動いているサーバーの今の状態を確かめる
            seen: list[str] = []
            broken = server_problems(ctx, seen)
            clean = serving(ctx) and not broken and not check_failed and not stopped
            now = ("Problems found when the page was opened: " + " / ".join(broken) if broken else
                   "The server is running and its page opens without errors (including the JavaScript and the API it calls)."
                   + ("\nWhat was opened and what it returned:\n" + "\n".join(seen) if seen else "")
                   if serving(ctx) else "")
    finally:
        kept = stop_processes(ctx, halted)  # この依頼で起動したサーバーなどは、残すと言われたもの以外は止める

    streamed = False
    if answer is None:  # 打ち切り・空の報告: 作業の記録を踏まえて、普通の文章で報告させる
        try:
            gen = generate(model, _report_prompt(task, calls, stopped or ended, japanese, now, clean), temperature=0.2,
                           max_tokens=900)
            tokens += gen.tokens
            gen_latency += gen.latency_s
            answer = _plain_answer(gen.text)
            answer = _without_code(answer) if answer else answer
            if answer and clean:  # 確かめで問題が無かった: 確かめた事柄を「未完了」とする項目は、事実ではない
                answer = _drop_unfinished(answer) or answer
        except BackendError:
            answer = None
        if not answer:
            answer = _log_report(calls, stopped)
    if written_at:  # キーの要らない API を使うコードなのに「API キーを設定していない」と書いた報告を直す(実際にあった)
        answer = knowhow.drop_false_key_claims(answer, {p: file_text(ctx, p, 30_000) for p in written_at}) or answer

    prefix = [text for text in (fact_check(task, answer, calls, ctx), f"【注意】{stopped}" if stopped else None) if text]
    if broken:  # 報告の時点で、起動したサーバーのページがエラーだった(AIの報告より、実際に開いた結果を正とする)
        prefix.append("【注意】報告の時点で、起動したサーバーのページを開くとエラーでした: " + " / ".join(broken))
    if check_failed and not broken:  # 報告の前に自分で起動して確かめたところ、動かなかった(直す手数が残っていなかった)
        prefix.append("【注意】報告の前に、作ったサーバーを起動して確かめたところ、動きませんでした: " + _problem_line(check_failed))
    suffix = []
    restart = [hint for command, hint in halted if command not in answer]
    if restart:  # 報告は「開くと表示されます」と書くが、止めたので開けない(動かし直すコマンドも無かった。実際にあった)
        suffix.append(("作ったサーバーは、依頼が終わったので止めました。もう一度動かすには、" if japanese else
                       "The server was stopped when the request ended. To run it again: ") + " / ".join(restart[:2])
                      + ("。" if japanese else "."))
    if kept:
        suffix.append("動かし続けているプログラム:\n" + "\n".join(f"- {k}" for k in kept)
                      + "\n(止めるときは、エージェントに「止めて」と頼むか、画面の「動作中のプログラム」から止めてください)")
    if prefix:
        answer = "\n\n".join(prefix + [answer])
    if suffix:
        answer = "\n\n".join([answer] + suffix)
    if on_token and not streamed:
        on_token(answer)
    return {"answer": answer, "calls": calls, "tokens": tokens, "gen_latency": gen_latency, "steps": len(calls),
            "stopped": bool(stopped), "plan": steps}
