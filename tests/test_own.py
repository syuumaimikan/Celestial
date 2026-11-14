import json
import tempfile
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo, NativeBackend
from celestial.backends.native import META_FILE
from celestial.evolve import Store
from celestial.study import answer_ok, pick_teacher, run_study, well_formed
from celestial.train import TrainError, train, training_examples

SMALL = ModelInfo("small", "mock", int(1e9))
BIG = ModelInfo("big", "mock", int(5e9))


def responder(model, messages, schema):
    props = (schema or {}).get("properties", {})
    if "tasks" in props:
        return json.dumps({"tasks": ["次を英語に翻訳して: 今日は良い天気です", "次を英語に翻訳して: 駅まで歩いて十分かかります",
                                      "次を英語に翻訳して: 猫が好きです", "短い"]}, ensure_ascii=False)
    if "role" in props:
        return json.dumps({"role": "general"})
    if "ok" in props:
        return json.dumps({"ok": True})
    return f"{model}の回答"


def make(models=(SMALL, BIG)):
    backend = MockBackend("mock", responder, list(models))
    return Celestial(Config(home=Path("unused")), backends={"mock": backend}, store=Store())


def log_bench(app, label, by_role):
    n = sum(c[1] for c in by_role.values())
    app.store.log_bench(label=label, suite="s", n=n, correct=sum(c[0] for c in by_role.values()), route_n=0,
                        route_correct=0, avg_tokens=100, avg_latency_s=1.0, by_role=json.dumps(by_role))


class StudyTest(unittest.TestCase):
    def test_study_collects_examples_and_teaches_router(self):
        app = make()
        result = run_study(app, per_role=2, roles=["translate"], seed=1)
        self.assertEqual((result["added"], result["teacher"]), (2, "big"))  # 計測が無ければ最大のモデルが教師
        rows = app.store.study_rows()
        # 計測の課題に似た問題(猫が好きです)と短すぎる問題は入らない
        self.assertEqual([r["task"] for r in rows], ["次を英語に翻訳して: 今日は良い天気です", "次を英語に翻訳して: 駅まで歩いて十分かかります"])
        self.assertEqual(rows[0]["answer"], "bigの回答")
        self.assertEqual(app.router.examples, 2)
        self.assertEqual(app.store.top_level(), [])  # 利用実績には混ざらない
        self.assertEqual(len(training_examples(app.store)), 2)

    def test_quality_filters(self):
        self.assertTrue(well_formed("translate", "次を英語に翻訳して: 来週の金曜日に会議があります"))
        self.assertFalse(well_formed("translate", "イタリア語のメニューを日本語に訳して。"))  # 本文が無い
        self.assertFalse(well_formed("summarize", "この文章を要約して: 短い"))  # 本文が短すぎる
        self.assertTrue(well_formed("summarize", "次の文章を30文字以内で要約して:" + "睡眠中に脳は記憶を整理している。" * 4))  # 全角コロン
        self.assertFalse(well_formed("summarize", "{task_id: 1} {request: 要約して: " + "あ" * 80))  # 壊れた出力
        self.assertFalse(well_formed("general", "最新のKindleの価格を教えて"))  # 外部の情報が要る
        self.assertTrue(well_formed("general", "光合成とは何かを説明して"))
        self.assertFalse(answer_ok("どのような文書でしょうか?翻訳したい文章を入力してください。"))  # 教師が聞き返している
        self.assertTrue(answer_ok("It will rain tomorrow."))

    def test_teacher_follows_bench(self):
        app = make()
        log_bench(app, "small", {"general": [5, 5, 1.0]})
        log_bench(app, "big", {"general": [2, 5, 1.0]})
        self.assertEqual(pick_teacher(app).name, "small")

    def test_training_needs_data(self):
        with self.assertRaises(TrainError):
            train(make())


class PriorTest(unittest.TestCase):
    def test_bench_results_steer_model_choice(self):
        app = make()
        self.assertEqual(app._pick_model("reason").name, "big")  # 事前知識だけなら中型が好まれる役割
        log_bench(app, "small", {"reason": [5, 5, 2.0], "general": [1, 5, 2.0]})
        log_bench(app, "big", {"reason": [1, 5, 2.0], "general": [5, 5, 2.0]})
        self.assertEqual(app._pick_model("reason").name, "small")
        self.assertEqual(app._pick_model("general").name, "big")
        # 実際の評価が積み重なれば、計測結果より優先される
        for i in range(12):
            r = app.run(f"計算{i}", role="reason", model="small" if i % 2 else "big", use_cache=False)
            app.feedback(r.id, good=(r.model == "big"))
        self.assertEqual(app._pick_model("reason").name, "big")


class NativeTest(unittest.TestCase):
    def test_lists_own_models_without_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "celestial-v1"
            model_dir.mkdir()
            (model_dir / META_FILE).write_text(json.dumps({"name": "celestial-v1", "base": "x", "size_bytes": 10}), encoding="utf-8")
            native = NativeBackend("native", models_dir=tmp)
            models = native.list_models()
            self.assertEqual([(m.name, m.tier, m.can_manage) for m in models], [("celestial-v1", "small", False)])

            mock = MockBackend("mock", responder, [SMALL])
            app = Celestial(Config(home=Path(tmp)), backends={"mock": mock, "native": native}, store=Store())
            self.assertEqual({m.name for m in app.models()}, {"small", "celestial-v1"})
            self.assertEqual(app.manager_model().name, "small")  # 自作AIは最小でも管理AIにはしない


class StudyAndTrainUpgradeTest(unittest.TestCase):
    def test_clean_and_verify_answer(self):
        from celestial.study import clean_answer, verify_answer

        self.assertEqual(
            clean_answer("summarize", "はい、承知いたしました。\n\n要約: これは要点です。"),
            "これは要点です。",
        )
        # 通常の文頭「はい、…」や文章作成(write)は過剰に削らない
        self.assertEqual(clean_answer("general", "はい、クジラは哺乳類です。"), "はい、クジラは哺乳類です。")
        self.assertEqual(clean_answer("write", "はい、承知いたしました。\nメール文面です。"), "はい、承知いたしました。\nメール文面です。")

        self.assertEqual(verify_answer("summarize", "30字以内で要約して: 本文", "あ" * 31), (False, False))
        self.assertEqual(verify_answer("summarize", "30字以内で要約して: 本文", "あ" * 20), (True, True))
        self.assertEqual(verify_answer("extract", "JSON形式で抜き出して: 本文", '{"name": "A"}'), (True, True))
        self.assertEqual(verify_answer("extract", "JSON形式で抜き出して: 本文", "nameはAです"), (False, False))
        self.assertEqual(verify_answer("translate", "次を英語に翻訳して: おはよう", "Good morning."), (True, True))
        self.assertEqual(verify_answer("translate", "次を英語に翻訳して: おはよう", "おはようございます。"), (False, False))
        self.assertEqual(
            verify_answer("code", "Pythonで関数を書いて", "```python\ndef add(a, b):\n    return a + b\n```"),
            (True, True),
        )
        self.assertEqual(verify_answer("code", "Pythonで関数を書いて", "```python\ndef add(:\n```"), (False, False))

    def test_curriculum_zero_bench_leak_and_weights(self):
        from celestial.bench_tasks import BUILTIN
        from celestial.study import generate_curriculum_examples
        from celestial.textsim import cosine, vectorize
        from celestial.train import training_examples

        app = make()
        res = generate_curriculum_examples(app, count=24, seed=42)
        self.assertEqual(res["added"], 24)
        rows = app.store.study_rows()
        self.assertEqual(len(rows), 24)
        bench_vecs = [vectorize(t["prompt"]) for t in BUILTIN]
        for row in rows:
            vec = vectorize(row["task"])
            self.assertLess(max(cosine(vec, b) for b in bench_vecs), 0.72)
        exs = training_examples(app.store)
        self.assertTrue(all(ex["weight"] >= 1.25 for ex in exs))

    def test_per_role_teacher_weak_roles_and_preferences(self):
        from celestial.study import weak_roles

        app = make()
        log_bench(app, "small", {"code": [5, 5, 1.0], "general": [1, 5, 1.0]})
        log_bench(app, "big", {"code": [2, 5, 1.0], "general": [5, 5, 1.0]})
        self.assertEqual(pick_teacher(app, role="code").name, "small")
        self.assertEqual(pick_teacher(app, role="general").name, "big")

        log_bench(app, "celestial-v1:latest", {"reason": [1, 5, 1.0], "code": [4, 5, 1.0], "general": [5, 5, 1.0]})
        self.assertEqual(weak_roles(app)[0], "reason")

        r = app.run("1+1は?", role="reason", model="small", use_cache=False)
        app.feedback(r.id, good=False)
        app.store.add_study(task="1+1は?", role="reason", model="big", answer="2です。", verified=True)
        pairs = app.store.interaction_preferences()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["chosen"], "2です。")

    def test_review_study_cleans_and_drops_near_bench(self):
        from celestial.bench_tasks import BUILTIN
        from celestial.study import review_study

        mock = MockBackend("mock", lambda _m, _msgs, _s=None: '{"ok": true}', [SMALL])
        with tempfile.TemporaryDirectory() as tmp:
            app = Celestial(Config(home=Path(tmp)), backends={"mock": mock}, store=Store())
            app.store.add_study(task="光合成の仕組みを教えて", role="general", model="small",
                                answer="はい、承知いたしました。\n\n植物が光を使って栄養を作る仕組みです。")
            app.store.add_study(task=BUILTIN[0]["prompt"], role=BUILTIN[0].get("role", "general"),
                                model="small", answer="テスト回答")
            res = review_study(app, teacher="small")
            self.assertEqual(res["removed"], 1)
            self.assertEqual(res["cleaned"], 1)
            kept = app.store.study_rows()
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["answer"], "植物が光を使って栄養を作る仕組みです。")

    def test_tiny_native_train_logprobs_and_loop_cut(self):
        try:
            import torch
            from transformers import AutoTokenizer, Qwen2Config, Qwen2ForCausalLM
        except ImportError:
            self.skipTest("torch/transformers がありません")
        try:
            tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct", local_files_only=True)
        except Exception:
            self.skipTest("Qwen2.5 tokenizer キャッシュがありません")

        from celestial.ollama_export import _CHATML
        from celestial.train import TrainConfig

        self.assertNotIn("\r\n", _CHATML)
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "tiny-qwen"
            base_dir.mkdir()
            cfg_qwen = Qwen2Config(
                vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64,
                num_hidden_layers=2, num_attention_heads=2, num_key_value_heads=1,
                max_position_embeddings=256, bos_token_id=tokenizer.bos_token_id or 151643,
                eos_token_id=tokenizer.eos_token_id or 151645,
            )
            Qwen2ForCausalLM(cfg_qwen).save_pretrained(base_dir)
            tokenizer.save_pretrained(base_dir)

            native = NativeBackend("native", models_dir=str(Path(tmp) / "models"), max_new_tokens=16)
            app = Celestial(Config(home=Path(tmp)), backends={"native": native}, store=Store())
            for i in range(10):
                app.store.add_study(task=f"計算{i}: {i}+1は?", role="reason", model="t", answer=f"{i+1}です", verified=True)
            app.store.add_preference(task="計算0: 0+1は?", role="reason", chosen="1です", rejected="999です")

            meta = train(app, TrainConfig(base=str(base_dir), epochs=1, batch_tokens=256, grad_accum=2, rank=4, kl_beta=0.05))
            self.assertEqual(meta["name"], "celestial-v1")
            self.assertGreaterEqual(meta["preference_steps"], 1)

            lps, top_lps = [], []
            gen = native.generate("celestial-v1", [{"role": "user", "content": "計算0: 0+1は?"}],
                                  max_tokens=6, logprobs=lps, top_logprobs=top_lps)
            self.assertGreater(len(lps), 0)
            self.assertEqual(len(lps), len(top_lps))
            self.assertTrue(all(isinstance(d, dict) and len(d) > 0 for d in top_lps))
            self.assertIn("celestial-v1", native.loaded_models())

            # 構造化 JSON 制約 (_JsonConstraint / json_schema) が valid JSON を返すこと
            j_gen = native.generate(
                "celestial-v1",
                [{"role": "user", "content": "role と ok を JSON で返して"}],
                max_tokens=32,
                json_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
            )
            self.assertIsInstance(json.loads(j_gen.text), dict)

            # LoRA+ と CPU オフロード AdamW でも学習が完結すること
            meta2 = train(app, TrainConfig(
                base=str(base_dir), epochs=1, batch_tokens=256, grad_accum=2, rank=4,
                kl_beta=0.05, loraplus_ratio=4.0, offload_optimizer=True,
            ))
            self.assertEqual(meta2["name"], "celestial-v2")

    def test_wiki_facts_and_expanded_curriculum(self):
        from unittest import mock as _mock
        from celestial.study import generate_curriculum_examples, generate_wiki_facts

        mock_resp = json.dumps({"items": [
            {"q": "ヘリウムの原子番号はいくつですか？", "a": "ヘリウムの原子番号は2です。"},
            {"q": "ネオンは周期表の何族に属する元素ですか？", "a": "ネオンは第18族の希ガスに属します。"},
        ]})
        backend = MockBackend("mock", lambda _m, _msgs, _s=None: mock_resp, [SMALL])
        app = Celestial(Config(home=Path("unused")), backends={"mock": backend}, store=Store())

        with _mock.patch("celestial.web.wikipedia", return_value="ヘリウムは原子番号2の元素。ネオンは第18族。"):
            w = generate_wiki_facts(app, count=4, teacher="small", seed=1)
        self.assertGreaterEqual(w["added"], 2)

        cur = generate_curriculum_examples(app, count=32, seed=7)
        self.assertEqual(cur["added"], 32)
        self.assertGreater(cur["by_role"]["translate"], 0)
        self.assertGreater(cur["by_role"]["summarize"], 0)
        self.assertGreater(cur["by_role"]["code"], 0)
        self.assertGreater(cur["by_role"]["agent"], 0)
        self.assertGreater(cur["by_role"]["judge"], 0)


class SpecialistProfileTest(unittest.TestCase):
    def test_profiles_specialties_and_qwen3_export_template(self):
        from celestial.ollama_export import chat_template_for
        from celestial.study import collect_contrastive_preferences
        from celestial.train import OWN_PROFILES, TrainError, balance_examples, config_for_profile, training_examples

        for name in ("nano", "qwen3-nano", "fast", "qwen3", "pro", "coder", "reason", "lingual", "agent", "judge"):
            self.assertIn(name, OWN_PROFILES)
            cfg = config_for_profile(name)
            self.assertEqual(cfg.profile, name)
            self.assertTrue(cfg.name.startswith("celestial-"))
        with self.assertRaises(TrainError):
            config_for_profile("nonexistent-profile")

        q3 = chat_template_for("qwen3", thinking=False)
        q2 = chat_template_for("qwen2", thinking=False)
        self.assertIn("<think>\n\n</think>", q3)
        self.assertNotIn("<think>", q2)

        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp) / "models"
            for name, meta in [
                ("celestial-pro", {"name": "celestial-pro", "base": "b", "size_bytes": 3_000_000_000,
                                   "can_manage": True, "profile": "pro", "specialties": ["general", "reason"]}),
                ("celestial-coder", {"name": "celestial-coder", "base": "b", "size_bytes": 3_000_000_000,
                                     "can_manage": True, "profile": "coder", "specialties": ["code", "agent"]}),
                ("celestial-judge", {"name": "celestial-judge", "base": "b", "size_bytes": 1_500_000_000,
                                     "can_manage": True, "profile": "judge", "specialties": ["judge", "extract"]}),
            ]:
                d = mdir / name
                d.mkdir(parents=True)
                (d / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
            native = NativeBackend("native", models_dir=str(mdir))
            mock = MockBackend("mock", lambda _m, _msgs, _s=None: "wrong answer 999999 " + "x" * 280, [SMALL])
            app = Celestial(Config(home=Path(tmp)), backends={"native": native, "mock": mock}, store=Store())
            by_name = {m.name: m for m in app.models()}
            self.assertTrue(by_name["celestial-pro"].can_manage)
            self.assertEqual(by_name["celestial-coder"].specialties, ("code", "agent"))
            self.assertEqual(app._pick_agent_model("code").name, "celestial-coder")
            self.assertEqual(app._decision_model().name, "celestial-judge")

            # エージェント・判定教材と均等化・対比ペア収集
            app.store.add_study(task="計算: 5*5は?", role="reason", model="small", answer="5×5=25です。\n答え: 25", verified=True)
            app.store.add_study(task="JSONで計画して: 作業ステップ", role="agent", model="celestial-curriculum",
                                answer='{"steps": [{"title": "確認", "role": "files"}]}', verified=True)
            app.store.add_study(task="厳格判定 JSON: 2+2=4?", role="judge", model="celestial-curriculum",
                                answer='{"ok": true, "issue": ""}', verified=True)
            all_ex = training_examples(app.store)
            self.assertEqual({e["role"] for e in all_ex}, {"reason", "agent", "judge"})
            # 重み・偏り抑制
            many = [{"role": "reason", "task": f"t{i}", "answer": "a", "weight": 1.0} for i in range(15)] + [
                {"role": "code", "task": "c0", "answer": "a", "weight": 1.0}
            ]
            bal = balance_examples(many, max_per_role=4, specialties=["code"], seed=0)
            self.assertEqual(sum(1 for x in bal if x["role"] == "reason"), 4)
            self.assertAlmostEqual(next(x["weight"] for x in bal if x["role"] == "code"), 1.35)

            pref = collect_contrastive_preferences(app, student="small", limit=10)
            self.assertEqual(pref["student"], "small")
            self.assertGreaterEqual(pref["added"], 1)


if __name__ == "__main__":
    unittest.main()
