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


if __name__ == "__main__":
    unittest.main()
