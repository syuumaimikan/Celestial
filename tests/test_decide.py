"""判断(Jev のような System 1)と、記憶の関門の試験。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from celestial import Celestial, Config, decide
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
from celestial.workers import ROLES


def make_app(top, responder=None, home=None):
    backend = MockBackend("mock", responder or (lambda model, messages, schema: json.dumps({"role": "general"}) if schema else "了解"),
                          [ModelInfo("small", "mock", int(1e9)), ModelInfo("big", "mock", int(4e9))])
    backend.top = top
    app = Celestial(Config(home=Path(home or tempfile.mkdtemp())), backends={"mock": backend}, store=Store())
    return app, backend


class DistributionTest(unittest.TestCase):
    def test_letters_and_words_are_counted(self):
        dist = decide.option_distribution({"A": -0.1, " B": -2.4, "はい": -5.0, "**": -1.0}, ["はい", "いいえ"])
        self.assertAlmostEqual(sum(dist), 1.0)
        self.assertGreater(dist[0], 0.9)  # A と「はい」は1つ目、" B" は2つ目
        self.assertIsNone(decide.option_distribution({"**": -0.1}, ["はい", "いいえ"]))  # 選択肢に当たらない
        # 書き出しが2つの選択肢に当てはまる語は数えない
        self.assertEqual(decide.option_distribution({"至": -0.1, "C": -3.0}, ["至急", "至って普通", "低い"]), [0.0, 0.0, 1.0])
        self.assertEqual(decide.parse_answer(" B", ["はい", "いいえ"]), 1)
        self.assertEqual(decide.parse_answer("いいえです", ["はい", "いいえ"]), 1)

    def test_temperature_fixes_overconfidence(self):
        # いつも 99% と言うが、半分しか当たらない → 温度を上げて確率をならす
        samples = [([0.99, 0.01], 0), ([0.99, 0.01], 1)] * 10
        t = decide.fit_temperature(samples)
        self.assertGreater(t, 3)
        self.assertLess(decide.calibration_error(samples, t), decide.calibration_error(samples, 1.0))
        self.assertEqual(decide.calibrate([0.7, 0.3], 1.0), [0.7, 0.3])


class DecideTest(unittest.TestCase):
    def test_decide_returns_probabilities(self):
        app, backend = make_app(lambda model, messages: {"A": -0.05, "B": -3.1})
        d = app.decide("このメールは返信が必要ですか?", context="明日の会議の時間を教えてください。")
        self.assertEqual((d.answer, d.index), ("はい", 0))  # 選択肢を省くと はい/いいえ
        self.assertAlmostEqual(d.probability, 1 / (1 + 2.718281828 ** -3.05), places=3)
        self.assertTrue(d.calibrated)
        self.assertIn("答え(記号1文字)", backend.calls[-1][1][-1]["content"])
        with self.assertRaises(ValueError):
            app.decide("", ["a", "b"])
        with self.assertRaises(ValueError):
            app.decide("どれ?", ["一つだけ"])
        # 確率の分布を返さないAIでも、生成された記号から選ぶ
        backend.top = None
        backend.responder = lambda model, messages, schema: "B"
        d = app.decide("緊急度は?", ["至急", "普通"], "急ぎません")
        self.assertEqual((d.answer, d.probability, d.calibrated), ("普通", 1.0, False))

    def test_calibration_picks_the_most_accurate_model(self):
        from unittest import mock

        tasks = [("返信は必要?", None, "会議の時間を教えて", 0), ("返信は必要?", None, "返信は不要です", 1),
                 ("緊急度は?", ["至急", "普通"], "サーバーが止まった", 0)]
        truth = {ctx: ans for _, _, ctx, ans in tasks}

        def top(model, messages):
            text = messages[-1]["content"]
            answer = next((ans for ctx, ans in truth.items() if ctx in text), 0)
            if model == "small":  # 小さいほうが正しく答える(大きいほど良いとは限らない)
                return {decide.LETTERS[answer]: -0.1, decide.LETTERS[1 - answer]: -2.5}
            return {"A": -0.01, "B": -5.0}  # いつも A と答える

        app, _ = make_app(top)
        with mock.patch("celestial.decide.DECISION_TASKS", tasks), mock.patch("celestial.bench.load_tasks", lambda: []):
            results = {r["model"]: r for r in app.calibrate_decisions()}
        self.assertEqual(results["small"]["accuracy"], 1.0)
        self.assertLess(results["big"]["accuracy"], 1.0)
        self.assertEqual(app._decision_model().name, "small")
        self.assertIn("temperature", app.decision_calibration()["small"])
        self.assertEqual(results["small"]["n"], 3)


class MemoryGateTest(unittest.TestCase):
    def test_personal_facts_are_suggested(self):
        def respond(model, messages, schema):
            if schema:
                return json.dumps({"role": "general"})
            if messages[0]["content"].startswith("Extract the lasting personal fact"):
                return "私はコーヒーをブラックで飲む"
            return "了解しました"

        app, backend = make_app(lambda model, messages: {"A": -0.02, "B": -4.0}, respond)
        self.assertEqual(app.suggest_memory("私はコーヒーはいつもブラックで飲みます。おすすめの豆は?"), "私はコーヒーをブラックで飲む")
        self.assertIsNone(app.suggest_memory("17×23はいくつ?"))  # 自分のことを話していない発言は、判断にかけない
        self.assertIsNone(app.suggest_memory("覚えて: 私は猫が好き"))  # 「覚えて」は別の決まりで覚える
        backend.top = lambda model, messages: {"A": -3.0, "B": -0.05}  # 関門が「含まれていない」と判断
        self.assertIsNone(app.suggest_memory("私の質問に答えて"))


if __name__ == "__main__":
    unittest.main()
