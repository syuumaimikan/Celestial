from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections import Counter

from .backends import BackendError
from .bench import Report, load_tasks, run_bench
from .config import load_config
from .manager import Celestial, Result
from .study import review_study, run_study, weak_roles
from .train import (MODEL_PRESETS, OWN_PROFILES, TrainConfig, TrainError, config_for_profile, delete_own_model,
                    evolve_own_ai, finish_model, resolve_base, train, training_examples)
from .workers import ROLES


def _footer(r: Result) -> str:
    bits = [f"#{r.id}", r.role, r.model, f"振り分け={r.route_source}", f"{r.tokens} tok", f"{r.latency_s:.2f}s"]
    if r.source == "cache":
        bits.append("キャッシュ")
    if r.examples_used:
        bits.append(f"手本{r.examples_used}件")
    if r.tool_calls:
        bits.append("道具: " + ", ".join(c["label"] for c in r.tool_calls))
    if r.subresults:
        bits.append("分担: " + ", ".join(f"{s.role}@{s.model}" for s in r.subresults))
    return "  ".join(bits)


def _confirm(request: dict) -> bool:
    """影響のある操作の前に、端末で確認を取る。"""
    print(f"\n\033[33m確認: {request['label']}  {request['summary']}\033[0m")
    if request.get("preview"):
        print("\n".join("  | " + line for line in request["preview"].splitlines()[:30]))
    try:
        return input("実行しますか? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _show_stage(stage: str, info: dict) -> None:
    if stage == "agent_step":
        print(f"\033[2m[{info['n']}] {info['label']}  {info['detail']}\033[0m", flush=True)
    elif stage == "agent_result" and not info["ok"]:
        print(f"\033[2m    → {info['output'].splitlines()[0][:100]}\033[0m", flush=True)


def _run_once(app: Celestial, task: str, args, history=None) -> Result:
    images = [open(path, "rb").read() for path in (args.image or [])]
    result = app.run(
        task, role=args.role, model=args.model, use_cache=not args.no_cache, verify=args.verify,
        swarm=True if args.swarm else False if args.no_swarm else None,
        on_token=lambda s: print(s, end="", flush=True), history=history, agent=args.agent or args.tools,
        approve=(lambda request: True) if args.yes else _confirm, images=images or None, on_stage=_show_stage,
    )
    print(f"\n\n\033[2m{_footer(result)}\033[0m")
    return result


def cmd_run(app: Celestial, args) -> int:
    task = " ".join(args.task) if args.task else sys.stdin.read()
    _run_once(app, task, args)
    return 0


def cmd_chat(app: Celestial, args) -> int:
    print("Celestial 対話モード  /good /bad = 直前の回答を評価, /new = 文脈を消去, /exit = 終了")
    history: list[tuple[str, str]] = []
    last: Result | None = None
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/new":
            history.clear()
            print("文脈を消去しました。")
            continue
        if line in ("/good", "/bad"):
            if last is None:
                print("評価する回答がまだありません。")
            else:
                app.feedback(last.id, line == "/good")
                print(f"#{last.id} を{'良い' if line == '/good' else '悪い'}と記録しました。")
            continue
        try:
            last = _run_once(app, line, args, history=history[-3:] or None)
        except BackendError as e:
            print(f"エラー: {e}")
            continue
        history.append((line, last.text))


def cmd_feedback(app: Celestial, args) -> int:
    app.feedback(args.id, args.rating == "good")
    print(f"#{args.id} を {args.rating} と記録しました。")
    return 0


def cmd_models(app: Celestial, args) -> int:
    manager = app.manager_model().name
    for m in sorted(app.models(), key=lambda m: m.size_bytes):
        mark = "  ← 管理AI" if m.name == manager else ""
        print(f"{m.name:32} {m.backend:10} {m.size_bytes / 1e9:5.1f} GB  {m.tier}{mark}")
    return 0


def cmd_stats(app: Celestial, args) -> int:
    rows = app.store.top_level()
    if not rows:
        print("まだ履歴がありません。")
        return 0

    def summary(chunk) -> str:
        n = len(chunk)
        tokens = sum(r["prompt_tokens"] + r["completion_tokens"] + r["overhead_tokens"] for r in chunk) / n
        latency = sum(r["latency_s"] for r in chunk) / n
        cache = sum(r["source"] == "cache" for r in chunk) / n
        routed = [r for r in chunk if r["route_source"] in ("learned", "manager", "heuristic")]
        learned = sum(r["route_source"] == "learned" for r in routed) / len(routed) if routed else 0.0
        return f"{n:5}件  平均 {tokens:7.0f} tok  {latency:6.2f}s  キャッシュ {cache:4.0%}  学習済み振り分け {learned:4.0%}"

    print(f"全体      {summary(rows)}")
    if len(rows) >= 10:  # 進化の度合い: 前半と後半を比べる
        half = len(rows) // 2
        print(f"前半      {summary(rows[:half])}")
        print(f"後半      {summary(rows[half:])}")
    rated = Counter(r["feedback"] for r in rows)
    print(f"評価      良い {rated[1]} / 悪い {rated[-1]} / 未評価 {rated[None]}")
    print(f"学習状態  振り分けの教師例 {app.router.examples}件")
    print("\n役割        モデル                           回数  報酬   平均秒  平均tok")
    for r in app.store.role_model_table():
        print(f"{r['role']:10}  {r['model']:30} {r['n']:5}  {r['reward']:.2f}  {r['latency']:6.2f}  {r['tokens']:7.0f}")
    return 0


def cmd_export(app: Celestial, args) -> int:
    n = app.export(args.path)
    print(f"{n}件を {args.path} に書き出しました。")
    return 0


def _print_report(report: Report) -> None:
    print(f"\n[{report.label}]  正解 {report.correct}/{report.n} ({report.accuracy:.0%})"
          f"  振り分け正解 {report.route_correct}/{len(report.routed)}"
          f"  平均 {report.avg_tokens:.0f} tok  {report.avg_latency:.2f}s")
    print("  " + "  ".join(f"{role} {c}/{n}" for role, (c, n, *_) in sorted(report.by_role().items())))
    if report.skipped:
        print(f"  ※コード実行が必要な {report.skipped} 件は対象外です(--run-code で採点)")


def cmd_agent_bench(app: Celestial, args) -> int:
    from .agentbench import run_agent_bench

    targets = [m.name for m in sorted(app.models(), key=lambda m: m.size_bytes) if m.can_manage] if args.all_models else [args.model]
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    reports = []
    for target in targets:
        print(f"\n=== エージェント: {target or '自動(計測で選ぶ)'}")

        def progress(o, i, total):
            mark = "o" if o.ok else "x"
            print(f"  {mark} [{i:2}/{total}] {o.id:14} {o.model:22} {o.latency_s:6.1f}s  {o.steps:2} 手  {o.error[:60]}", flush=True)

        reports.append(run_agent_bench(app, model=target, only=only, save=not args.no_save, on_outcome=progress))
    print()
    for r in reports:
        print(f"[agent:{r.model}]  成功 {r.correct}/{r.n} ({r.accuracy:.0%})  平均 {r.avg_latency:.1f}s")
    if not args.no_save:
        print(f"\nエージェントのAIの自動選択: {app._pick_agent_model('code').name}")
    return 0


def cmd_publish_app(app: Celestial, args) -> int:
    """作ったスマホのアプリを、PC から配れる場所に置く(スマホのアプリの「更新する」と「スマホ連携」のページから受け取れる)。"""
    from pathlib import Path

    project = Path(__file__).resolve().parents[2] / "app"
    source = Path(args.apk) if args.apk else project / "build" / "app" / "outputs" / "flutter-apk" / "app-arm64-v8a-release.apk"
    pubspec = project / "pubspec.yaml"
    if not source.is_file():
        raise ValueError(f"APK がありません: {source}(先に app で flutter build apk --release --split-per-abi "
                         "--target-platform android-arm64 を実行してください)")
    found = re.search(r"^version:\s*([\w.\-]+)\+(\d+)", pubspec.read_text("utf-8"), re.M) if pubspec.is_file() else None
    if not found:
        raise ValueError("app/pubspec.yaml に版(version: 0.3.0+3 の形)がありません")
    if not args.apk and pubspec.stat().st_mtime > source.stat().st_mtime:
        raise ValueError("pubspec.yaml が APK より新しいため、版が合わない可能性があります。APK を作り直してから置いてください")
    folder = app.cfg.home / "app"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, folder / "celestial.apk")
    meta = {"version": found.group(1), "build": int(found.group(2)), "size": source.stat().st_size, "published": time.time()}
    (folder / "celestial.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"スマホのアプリ {meta['version']}(build {meta['build']}、{meta['size'] / 1e6:.1f}MB)を置きました: {folder / 'celestial.apk'}")
    print("スマホのアプリの「設定 → このアプリについて → 更新する」で入れ替えられます。")
    return 0


def cmd_bench(app: Celestial, args) -> int:
    if args.agent:
        return cmd_agent_bench(app, args)
    if args.history:
        rows = app.store.bench_history()
        if not rows:
            print("まだ計測結果がありません。")
            return 0
        print("日時              ラベル                    タスク集     正解率   振り分け  平均tok  平均秒")
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            route = f"{r['route_correct']}/{r['route_n']}"
            print(f"{when}  {r['label']:24}  {r['suite']}  {r['correct']:3}/{r['n']:<3} {r['correct'] / r['n']:4.0%}"
                  f"  {route:>7}  {r['avg_tokens']:7.0f}  {r['avg_latency_s']:6.2f}")
        return 0

    tasks = load_tasks(args.tasks)
    if args.role:
        tasks = [t for t in tasks if t.get("role") == args.role]
    if not tasks:
        raise ValueError("該当するタスクがありません")
    targets = [m.name for m in sorted(app.models(), key=lambda m: m.size_bytes)] + [None] if args.all_models else [args.model]

    def progress(o, i, total):
        mark = "o" if o.ok else "x"
        route = "" if not o.role or o.role == o.routed_role else f"  (振り分け誤り: {o.routed_role})"
        print(f"  {mark} [{i:2}/{total}] {o.id:20} {o.model:22} {o.latency_s:6.2f}s{route}", flush=True)

    reports = []
    for target in targets:
        print(f"\n=== {target or '自動(管理AIが選択)'}")
        reports.append(run_bench(app, tasks, model=target, verify=args.verify, run_code=args.run_code,
                                 label=args.label if len(targets) == 1 else None, save=not args.no_save, on_outcome=progress))
    for report in reports:
        _print_report(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in reports], f, ensure_ascii=False, indent=2)
        print(f"\n詳細を {args.json} に書き出しました。")
    return 0


def _progress_line(done: int, total: int, message: str) -> None:
    print(f"\r[{done}/{total}] {message:<60}", end="", flush=True)


def cmd_study(app: Celestial, args) -> int:
    if args.review:
        r = review_study(app, teacher=args.teacher, on_progress=_progress_line)
        print(f"\n{r['checked']} 件を見直し、不要な {r['removed']} 件を削除・{r['cleaned']} 件を整形/検証しました。")
        print(f"学習に使えるデータは合計 {len(training_examples(app.store))} 件です。")
        return 0
    roles = [args.role] if args.role else (weak_roles(app)[:4] if args.focus_weak else None)
    result = run_study(
        app, per_role=args.per_role, teacher=args.teacher, roles=roles,
        synthetic=args.synthetic, wiki=args.wiki, verify_llm=args.verify, contrastive=args.contrastive,
        on_progress=_progress_line,
    )
    print(f"\n教師 {', '.join(result.get('teachers') or [result['teacher']])} で {result['added']} 件"
          f"(+プログラム生成 {result['synthetic_added']} 件 / Wikipedia {result.get('wiki_added', 0)} 件 / 選好ペア {result['preferences_added']} 件)を保存しました。")
    print("  " + "  ".join(f"{role} {n}" for role, n in result["by_role"].items()))
    print(f"学習に使えるデータは合計 {len(training_examples(app.store))} 件です。")
    return 0


def cmd_train(app: Celestial, args) -> int:
    base = resolve_base(args.preset or args.base) if (args.preset or args.base) else None
    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    config = config_for_profile(
        args.profile,
        base=base, epochs=args.epochs, lr=args.lr, batch_tokens=args.batch_tokens, grad_accum=args.grad_accum,
        rank=args.rank, kl_beta=args.kl_beta, neftune_alpha=args.neftune,
        loraplus_ratio=args.loraplus, offload_optimizer=True if args.offload else None,
        from_adapter=args.from_adapter, roles=roles, name=args.name, quantize=args.quantize,
    )
    meta = train(app, config, on_progress=_progress_line)
    eval_text = "" if meta["eval_loss"] is None else f"  検証損失 {meta['base_eval_loss']} → {meta['eval_loss']}"
    print(f"\n{meta['name']} を作成しました: 基礎={meta['base']}, {meta['examples']} 件で学習、{meta['seconds']} 秒{eval_text}")
    if not args.no_bench:
        done = finish_model(app, meta["name"], export=not args.no_export, run_code=args.run_code,
                            quantize=config.quantize, on_progress=_progress_line)
        print()
        if done["export_error"]:
            print(f"Ollama への書き出しはできませんでした({done['export_error']})。このまま内蔵の実行環境で使います。")
        if done["base_report"]:
            _print_report(done["base_report"])
        _print_report(done["report"])
    return 0


def cmd_evolve(app: Celestial, args) -> int:
    base = resolve_base(args.base or args.preset) if (args.base or args.preset) else (None if args.profile else resolve_base("auto"))
    config = config_for_profile(
        args.profile,
        base=base, epochs=args.epochs, quantize=args.quantize, name=args.name,
        loraplus_ratio=args.loraplus, offload_optimizer=True if args.offload else None,
    )
    res = evolve_own_ai(
        app, config=config, profile=args.profile, study_per_role=args.study_per_role,
        synthetic=args.synthetic, wiki=args.wiki,
        review=not args.no_review, run_code=not args.no_code, on_progress=_progress_line,
    )
    print(f"\n進化完了: {res['model']} (基礎={res['base']}, 重点役割={', '.join(res['focus_roles'])})")
    if res["base_correct"] is not None:
        print(f"  基礎モデル: {res['base_correct']}/{res['base_n']} ({res['base_correct']/max(1, res['base_n']):.0%})")
    print(f"  自作AI:     {res['bench_correct']}/{res['bench_n']} ({res['bench_correct']/max(1, res['bench_n']):.0%})")
    return 0


def cmd_own_delete(app: Celestial, args) -> int:
    res = delete_own_model(app, args.name)
    print(f"削除しました: {res['deleted']}" + (f" (Ollama: {res['ollama']})" if res.get("ollama") else ""))
    return 0


def cmd_kb(app: Celestial, args) -> int:
    sub = args.kb_cmd
    if sub == "list":
        st = app.kb.stats()
        print(f"知識ベース: {st['docs']} 件の資料 / {st['chunks']} チャンク / 埋め込み済み {st['embedded']} 件")
        for d in app.kb.list_docs():
            print(f"  #{d['id']} [{d['kind']}] {d['title']} ({d['chunks']} chunks)  {d['source']}")
        return 0
    if sub == "add":
        if args.note:
            title = args.title or "メモ"
            res = app.kb.add_note(title, args.target, tags=args.tags or "")
            print(f"登録しました: #{res['id']} {res['title']} ({res['chunks']} チャンク)")
        elif args.url or args.target.startswith(("http://", "https://")):
            res = app.kb.add_url(args.target, title=args.title or "", tags=args.tags or "")
            print(f"URL を登録しました: #{res['id']} {res['title']} ({res['chunks']} チャンク)")
        elif args.folder:
            res = app.kb.add_folder(args.target, tags=args.tags or "", limit=args.limit)
            print(f"フォルダを取り込みました: {res['indexed']} ファイル登録 (スキップ {res['skipped']} 件)")
        else:
            from pathlib import Path
            p = Path(args.target).expanduser()
            if p.is_dir():
                res = app.kb.add_folder(p, tags=args.tags or "", limit=args.limit)
                print(f"フォルダを取り込みました: {res['indexed']} ファイル登録 (スキップ {res['skipped']} 件)")
            else:
                res = app.kb.add_file(p, tags=args.tags or "")
                print(f"ファイルを登録しました: #{res['id']} {res['title']} ({res['chunks']} チャンク)")
        return 0
    if sub == "search":
        hits = app.kb.search(args.query, top_k=args.top_k)
        if not hits:
            print("該当する資料は見つかりませんでした。")
            return 0
        for h_item in hits:
            print(f"[{h_item['score']:.2f}] {h_item['title']} ({h_item['source']}):\n  {h_item['text'][:240]}\n")
        return 0
    if sub == "rm":
        try:
            key: int | str = int(args.target)
        except ValueError:
            key = args.target
        if not app.kb.remove(key):
            raise KeyError(f"資料が見つかりません: {args.target}")
        print(f"削除しました: {args.target}")
        return 0
    raise ValueError(f"不明なサブコマンドです: {sub}")


def cmd_ui(app: Celestial, args) -> int:
    from .server import serve

    if getattr(args, "no_remote", False):
        app.cfg.remote_enabled = False  # この起動の間だけ(保存はしない)
    return serve(app, port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_open", False),
                 keep_running=getattr(args, "keep_running", False))


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--role", choices=list(ROLES), help="振り分けを固定する")
    p.add_argument("--model", help="モデルを固定する")
    p.add_argument("--verify", action="store_true", help="管理AIが回答を検証し、不合格なら再回答させる(高精度・低速)")
    p.add_argument("--swarm", action="store_true", help="必ずサブタスクに分解する")
    p.add_argument("--no-swarm", action="store_true", help="分解しない")
    p.add_argument("--no-cache", action="store_true", help="キャッシュを使わない")
    p.add_argument("--agent", action="store_true", help="AIエージェントとして動かす: 設定で許可した道具を使い、自分で段取りして進める")
    p.add_argument("--tools", action="store_true", help="--agent と同じ")
    p.add_argument("--yes", action="store_true", help="エージェントの操作を確認なしで実行する")
    p.add_argument("--image", action="append", help="添付する画像ファイル(複数可)")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="celestial", description="小型AI群と管理AIによる、使うほど進化するローカルAI")
    parser.add_argument("--config", help="celestial.toml のパス")
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_ui)  # コマンド省略時は画面を開く

    p = sub.add_parser("ui", help="画面を開く(コマンド省略時の既定)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    p.add_argument("--no-remote", action="store_true", help="この起動の間、スマホからの接続を受け付けない")
    p.add_argument("--keep-running", action="store_true", help="ウィンドウを閉じても動き続ける")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("run", help="タスクを1件実行する(タスク省略時は標準入力から読む)")
    p.add_argument("task", nargs="*")
    _add_run_options(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("chat", help="対話モード")
    _add_run_options(p)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("feedback", help="回答を評価して学習させる")
    p.add_argument("id", type=int)
    p.add_argument("rating", choices=["good", "bad"])
    p.set_defaults(func=cmd_feedback)

    sub.add_parser("models", help="利用可能なモデル一覧").set_defaults(func=cmd_models)
    sub.add_parser("stats", help="利用実績と進化の度合い").set_defaults(func=cmd_stats)

    p = sub.add_parser("bench", help="精度を計測する(学習状態は変えない)")
    p.add_argument("--tasks", help="独自のタスク集(JSONL)。省略時は組み込み")
    p.add_argument("--role", choices=list(ROLES), help="この役割のタスクだけ計測する")
    p.add_argument("--model", help="全ワーカーをこのモデルに固定して計測する")
    p.add_argument("--all-models", action="store_true", help="各モデル固定と自動選択をすべて計測して比べる")
    p.add_argument("--verify", action="store_true", help="検証つきで計測する")
    p.add_argument("--run-code", action="store_true", help="コードのタスクも採点する(モデルが書いたコードをこのPC上で実行する)")
    p.add_argument("--label", help="履歴に残す名前")
    p.add_argument("--no-save", action="store_true", help="履歴に残さない")
    p.add_argument("--json", help="回答を含む詳細をJSONで書き出す")
    p.add_argument("--history", action="store_true", help="これまでの計測結果を表示する")
    p.add_argument("--agent", action="store_true", help="エージェントの作業(ファイル・フォルダ・コマンド)を計測する")
    p.add_argument("--only", help="--agent で、この課題だけを計測する(カンマ区切りの ID)")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("study", help="自習: 教師役のAIに練習問題を作らせて解かせ、学習データを貯める")
    p.add_argument("--per-role", type=int, default=15, help="担当ごとの問題数")
    p.add_argument("--role", choices=list(ROLES), help="この担当だけ自習する")
    p.add_argument("--teacher", help="教師役のモデル(省略時は役割ごとに計測で最も正確なモデル)")
    p.add_argument("--review", action="store_true", help="保存済みの自習データを見直し、不良・非自己完結を取り除き整形する")
    p.add_argument("--focus-weak", action="store_true", help="自作AIの苦手分野(直近計測の正答率が低い役割)を重点的に自習する")
    p.add_argument("--synthetic", type=int, default=0, help="検算済みのプログラム生成問題(推論・抽出・翻訳・要約)を追加する件数")
    p.add_argument("--wiki", type=int, default=0, help="Wikipedia 根拠の一般知識 Q&A を追加する件数")
    p.add_argument("--verify", action="store_true", help="教師AIを使った計算式・単体テストによる自動検算を有効にする")
    p.add_argument("--contrastive", action="store_true", help="既存自作AIと教師を比較して DPO 選好ペアを収集する")
    p.set_defaults(func=cmd_study)

    p = sub.add_parser("train", help="自作AIを学習する(履歴の良い回答 + 自習の成果)")
    p.add_argument("--profile", choices=list(OWN_PROFILES.keys()), help="自作AIの種類プロファイル(nano/qwen3-nano/fast/qwen3/pro/coder/reason/lingual/agent/judge)")
    p.add_argument("--preset", choices=["auto", *MODEL_PRESETS.keys()], help="モデル構成プリセット(auto は VRAM から自動選択)")
    p.add_argument("--base", default=None, help="基礎モデル(Hugging Face の名前)")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--batch-tokens", type=int, default=TrainConfig.batch_tokens, help="1バッチの最大トークン数(GPU メモリに合わせて下げる)")
    p.add_argument("--grad-accum", type=int, default=TrainConfig.grad_accum, help="勾配累積ステップ数")
    p.add_argument("--rank", type=int, default=TrainConfig.rank, help="LoRA のランク")
    p.add_argument("--kl-beta", type=float, default=None, help="基礎モデルとの KL 正則化係数(0 で無効)")
    p.add_argument("--neftune", type=float, default=TrainConfig.neftune_alpha, help="NEFTune 埋め込みノイズ係数")
    p.add_argument("--loraplus", type=float, default=TrainConfig.loraplus_ratio, help="LoRA+ 学習率倍率(lora_B / lora_A)")
    p.add_argument("--offload", action="store_true", help="最適化状態を CPU に逃がして GPU メモリを節約する")
    p.add_argument("--from-adapter", help="継続学習元にする既存自作AI名(例: celestial-v1)")
    p.add_argument("--roles", help="学習対象の役割をカンマ区切りで指定")
    p.add_argument("--name", help="作成する自作AI名(省略時はプロファイル名または celestial-vN)")
    p.add_argument("--quantize", default=None, choices=["q8_0", "q4_k_m", "f16"], help="Ollama 書き出し量子化")
    p.add_argument("--no-bench", action="store_true", help="学習後の書き出しと計測を省く")
    p.add_argument("--no-export", action="store_true", help="Ollama へ書き出さず、内蔵の実行環境で使う")
    p.add_argument("--run-code", action="store_true", help="学習後の計測でコードの課題も採点する")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("evolve", help="診断 → 見直し → 重点自習 → 学習 → 書き出し・計測を一気に行う")
    p.add_argument("--profile", choices=list(OWN_PROFILES.keys()), help="作成する自作AIプロファイル(pro/coder/reason/lingual/agent/judge 等)")
    p.add_argument("--preset", default=None, choices=["auto", *MODEL_PRESETS.keys()], help="基礎モデルプリセット")
    p.add_argument("--base", help="基礎モデルを直接指定する場合に指定")
    p.add_argument("--name", help="出力モデル名を上書きする場合に指定")
    p.add_argument("--study-per-role", type=int, default=8, help="重点役割ごとの追加問題数(0 で追加自習なし)")
    p.add_argument("--synthetic", type=int, default=40, help="プログラム生成の検算済み問題数")
    p.add_argument("--wiki", type=int, default=24, help="Wikipedia 根拠の一般知識 Q&A 問題数")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--quantize", default=None, choices=["q8_0", "q4_k_m", "f16"])
    p.add_argument("--loraplus", type=float, default=TrainConfig.loraplus_ratio, help="LoRA+ 学習率倍率")
    p.add_argument("--offload", action="store_true", help="最適化状態を CPU にオフロード")
    p.add_argument("--no-review", action="store_true", help="既存自習データの見直しを省く")
    p.add_argument("--no-code", action="store_true", help="コード実行採点を省く")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("own-delete", help="作成した自作AIを削除する")
    p.add_argument("name", help="削除する自作AI名(例: celestial-v1)")
    p.set_defaults(func=cmd_own_delete)

    kb_p = sub.add_parser("kb", help="ローカル知識ベース(RAG)を管理・検索する")
    kb_sub = kb_p.add_subparsers(dest="kb_cmd", required=True)
    kb_sub.add_parser("list", help="登録済みの資料一覧").set_defaults(func=cmd_kb)
    kb_add = kb_sub.add_parser("add", help="ファイル・フォルダ・URL・メモを登録する")
    kb_add.add_argument("target", help="パス、URL、または --note 時は本文")
    kb_add.add_argument("--title", help="タイトル")
    kb_add.add_argument("--tags", help="タグ")
    kb_add.add_argument("--note", action="store_true", help="文字列をメモとして登録")
    kb_add.add_argument("--folder", action="store_true", help="フォルダを一括取り込み")
    kb_add.add_argument("--url", action="store_true", help="URL を取得して登録")
    kb_add.add_argument("--limit", type=int, default=80, help="フォルダ取り込み時の最大ファイル数")
    kb_add.set_defaults(func=cmd_kb)
    kb_s = kb_sub.add_parser("search", help="知識ベースを検索する")
    kb_s.add_argument("query", help="検索キーワード・質問")
    kb_s.add_argument("-k", "--top-k", type=int, default=5, help="返す件数")
    kb_s.set_defaults(func=cmd_kb)
    kb_r = kb_sub.add_parser("rm", help="登録した資料を削除する")
    kb_r.add_argument("target", help="資料 ID またはソース名")
    kb_r.set_defaults(func=cmd_kb)

    p = sub.add_parser("publish-app", help="作ったスマホのアプリ(APK)を、PC から配れる場所に置く")
    p.add_argument("--apk", help="置く APK(省略時は app の arm64 向けのリリース版)")
    p.set_defaults(func=cmd_publish_app)

    p = sub.add_parser("export", help="自作モデル学習用のJSONLを書き出す")
    p.add_argument("path")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    try:
        app = Celestial(load_config(args.config))
        return args.func(app, args)
    except (BackendError, TrainError, ValueError, KeyError, OSError) as e:
        print(f"エラー: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
