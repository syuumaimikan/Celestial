"""AIエージェント: 目標を受け取り、道具を使いながら自分で段取りして進め、最後に報告する。

1手ごとに「考え・使う道具・引数」または「最終報告」を JSON で出させる(構造は文法で強制する)。
道具の実行はこの側で行い、結果を次の手の材料として渡す。安全の決まりは tools.py 側にある。
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .backends import BackendError, Message, ModelInfo
from .tools import TOOLS, ToolContext, ToolError, available_tools, execute, fact_check, step_schema, summarize_call, unavailable_notes
from .workers import ROLES

_KEEP_FULL = 6        # 直近この回数ぶんの道具の結果は全文のまま渡す
_OLD_RESULT = 400     # それより古い結果は、この文字数に縮める(文脈があふれないように)
_RULES = """You are Celestial, an AI agent working on the user's own PC. Work step by step toward the user's goal with tools, then report.

Reply with exactly one JSON object each turn:
- To act: "thought" (one short sentence), "tool" (a tool name) and "args".
- To finish: "tool" = "answer" and "answer" = the final report, written in the user's language: what you did, the result, and anything you could not do.

Rules:
- Never guess file contents, command output or what is on the screen: use the tools. Use calc for arithmetic.
- Look before you act: list or read files before changing them. edit_file needs the exact existing text.
- After changing code, run it or its tests when a run tool is available, and fix what fails.
- If a tool fails, read the error and try a different approach. Do not repeat the same call.
- PC control: open the app, wait for it, check its window title with list_windows, then type or press keys with the
  window argument set to that title (so input never goes to the wrong app). Check the result with screenshot when unsure.
- delegate gives one self-contained piece of work (translate, summarize, write, analyse a given text) to a specialist AI. Put all needed text in the task.
- Do only what the user asked. Stop as soon as the goal is reached."""


def _compact(messages: list[Message]) -> list[Message]:
    """古い道具の結果を縮めた写しを返す。最初の指示と依頼、直近のやり取りはそのまま残す。"""
    results = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("[")]
    old = set(results[:-_KEEP_FULL]) if len(results) > _KEEP_FULL else set()
    return [{**m, "content": m["content"][:_OLD_RESULT] + "\n...(古い結果のため省略)"} if i in old and len(m["content"]) > _OLD_RESULT else m
            for i, m in enumerate(messages)]


def run_agent(*, task: str, role_name: str, model: ModelInfo, ctx: ToolContext, max_steps: int,
              generate: Callable[..., object], history: list[tuple[str, str]] | None = None,
              on_event: Callable[..., None] | None = None, on_token: Callable[[str], None] | None = None,
              personal: str = "") -> dict:
    """エージェントを動かす。戻り値は {answer, calls, tokens, gen_latency, steps, stopped}。

    generate(model, messages, **kw) は1回の生成を行う関数(管理AIが渡す)。on_event(種類, **内容) は進行の通知。
    personal は利用者の指示と覚えていること(指示文の最後に足す)。
    """
    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, **data)

    tools = available_tools(ctx)
    schema = step_schema(tools)
    system = (_RULES + f"\n\nKind of task: {ROLES[role_name].description}\n\nTools:\n"
              + "\n".join(f"- {t.name}: {t.description}" for t in tools)
              + "\n\nYou can NOT do these: " + "; ".join(unavailable_notes(ctx) or ["(nothing is restricted)"])
              + ". If the goal needs one of them, do not pretend: say in the answer that it was not done and why.")
    if re.search(r"[぀-ヿ一-鿿]", task):  # 小さいモデルは英語で報告しがちなので、言語を明示する
        system += "\n\nThe user writes in Japanese. Write the final answer in natural Japanese."
    system += personal
    messages: list[Message] = [{"role": "system", "content": system}]
    for user_text, reply in history or []:
        messages += [{"role": "user", "content": user_text[:1500]}, {"role": "assistant", "content": reply[:1500]}]
    messages.append({"role": "user", "content": task})

    calls: list[dict] = []
    seen: dict[str, int] = {}
    answer: str | None = None
    tokens, gen_latency, failures, stopped = 0, 0.0, 0, ""

    for number in range(1, max_steps + 1):
        gen = generate(model, _compact(messages), temperature=0.0, max_tokens=2500, json_schema=schema)
        tokens += gen.tokens
        gen_latency += gen.latency_s
        try:
            step = json.loads(gen.text)
        except ValueError:
            step = None
        if not isinstance(step, dict):
            answer = gen.text.strip() or None  # JSON にならなかった出力は、そのまま報告として扱う
            break
        tool = str(step.get("tool"))
        thought = str(step.get("thought") or "").strip()[:300]
        if tool == "answer":
            answer = str(step.get("answer") or "").strip() or None
            break

        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        label = TOOLS[tool].label if tool in TOOLS else tool
        detail = summarize_call(tool, args)
        emit("agent_step", n=number, thought=thought, tool=tool, label=label, detail=detail)

        key = json.dumps([tool, args], sort_keys=True, ensure_ascii=False)
        seen[key] = seen.get(key, 0) + 1
        repeatable = tool in ("screenshot", "wait", "list_windows", "press_keys", "click", "scroll", "volume", "media", "system_info")
        if seen[key] > 1 and not repeatable:
            output, ok = "ERROR: you already made exactly this call. Do something different, or finish with the answer tool.", False
        else:
            try:
                output, ok = execute(ctx, tool, args), True
            except ToolError as e:
                output, ok = f"ERROR: {e}", False
        failures = 0 if ok else failures + 1
        calls.append({"name": tool, "label": label, "detail": detail, "ok": ok, "output": output[:400]})
        emit("agent_result", n=number, ok=ok, output=output[:600])
        messages += [{"role": "assistant", "content": gen.text},
                     {"role": "user", "content": f"[{tool} result]\n{output}\n\nContinue with the next step, or finish with the answer tool."}]
        if failures >= 3 or seen[key] >= 3:
            stopped = "うまく進まない状態が続いたため、途中で打ち切りました。"
            break
    else:
        stopped = f"手数の上限({max_steps} 手)に達したため、途中で打ち切りました。"

    streamed = False
    if answer is None:  # 打ち切り・空の報告: ここまでの結果を踏まえて、普通の文章で報告させる
        messages.append({"role": "user", "content": "Stop using tools now. Write the final report in plain text in the user's language: "
                                                     "what was done, the result, and what is still unfinished."})
        try:
            gen = generate(model, _compact(messages), temperature=0.2, on_token=on_token)
            tokens += gen.tokens
            gen_latency += gen.latency_s
            answer, streamed = gen.text, on_token is not None
        except BackendError:
            answer = "報告を作成できませんでした。"

    prefix = [text for text in (fact_check(task, answer, calls, ctx), f"【注意】{stopped}" if stopped else None) if text]
    if prefix:
        answer = "\n\n".join(prefix + [answer])
    if on_token:
        if not streamed:
            on_token(answer)
        elif prefix:
            on_token("\n\n" + "\n\n".join(prefix))
    return {"answer": answer, "calls": calls, "tokens": tokens, "gen_latency": gen_latency, "steps": len(calls), "stopped": bool(stopped)}
