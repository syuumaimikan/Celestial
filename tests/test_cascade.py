"""確からしさで答え直す仕組み(カスケード)・言語の取り違えの検出・資料での裏付けの試験。"""

import json
import unittest
from pathlib import Path
from unittest import mock

from celestial import Celestial, Config, ground
from celestial.backends import MockBackend, ModelInfo
from celestial.backends.base import loop_cut
from celestial.bench import run_bench
from celestial.evolve import Store
from celestial.manager import DEFAULT_CASCADE_THRESHOLD, learn_threshold, routing_text, wants_japanese, wrong_language
from celestial.workers import plausible_role

SMALL = ModelInfo("small", "mock", int(1e9))
BIG = ModelInfo("big", "mock", int(5e9))
CHINESE = "让我们逐步分析这个问题：设花子的年龄为x岁，太郎的年龄为x加3岁，所以两人的年龄和是25岁。"


def make(answers=None, logprob=None, models=(SMALL, BIG), **cfg):
    """answers: {モデル名: 回答}。logprob: {モデル名: 各トークンの対数確率}。"""
    answers = answers or {}
    levels = logprob or {}

    def responder(model, messages, schema):
        if schema and "role" in schema.get("properties", {}):
            return json.dumps({"role": "general"})
        return answers.get(model, f"{model} の回答です。")

    backend = MockBackend("mock", responder, list(models), logprob=lambda model, text: levels.get(model, -0.01))
    app = Celestial(Config(home=Path("unused"), **cfg), backends={"mock": backend}, store=Store())
    # 計測の実績: 一般の質問はどちらも全問正解だが、全体では big のほうが正確(small は速い)
    app.store.log_bench(label="small", suite="s", n=10, correct=6, route_n=0, route_correct=0, avg_tokens=10, avg_latency_s=0.2,
                        by_role=json.dumps({"general": [5, 5, 1.0], "reason": [1, 5, 1.0]}))
    app.store.log_bench(label="big", suite="s", n=10, correct=10, route_n=0, route_correct=0, avg_tokens=10, avg_latency_s=1.0,
                        by_role=json.dumps({"general": [5, 5, 5.0], "reason": [5, 5, 5.0]}))
    return app, backend


class LanguageTest(unittest.TestCase):
    def test_detects_chinese_and_english(self):
        self.assertTrue(wrong_language(CHINESE, "reason"))
        self.assertTrue(wrong_language("要計算的是以時速60公里的速度行駛2小時30分鐘的距離。", "reason"))
        self.assertTrue(wrong_language("Let's break this down step by step: first we compute the rate of A.", "reason"))
        self.assertFalse(wrong_language("日本で一番長い川は信濃川です。全長は367kmあります。", "general"))
        self.assertFalse(wrong_language("東京都、大阪府、京都府、北海道、神奈川県、愛知県、福岡県", "general"))  # 漢字の列挙は日本語
        self.assertFalse(wrong_language("def add(a, b):\n    return a + b  # adds two numbers together nicely", "code"))

    def test_wants_japanese(self):
        self.assertTrue(wants_japanese("太郎は花子より3歳年上です。", "reason"))
        self.assertFalse(wants_japanese("次の文を英語にして: 猫が好き", "general"))
        self.assertFalse(wants_japanese("猫が好き", "translate"))
        self.assertFalse(wants_japanese("What is 2+2?", "reason"))


class CascadeTest(unittest.TestCase):
    def test_unsure_answer_is_redone_by_more_accurate_model(self):
        app, backend = make(logprob={"small": -0.6, "big": -0.01})
        shown = []
        result = app.run("ハワイの州都はどこ?", role="general", on_token=shown.append)
        self.assertEqual(result.model, "big")
        self.assertEqual(result.escalated["from"], "small")
        self.assertEqual(result.escalated["reason"], "unsure")
        self.assertAlmostEqual(result.confidence, -0.6)
        self.assertEqual("".join(shown), "big の回答です。")  # 自信のない下書きは画面に出さない
        self.assertEqual([c[0] for c in backend.calls], ["small", "big"])

    def test_confident_answer_is_kept(self):
        app, backend = make(logprob={"small": -0.05})
        shown = []
        result = app.run("ハワイの州都はどこ?", role="general", on_token=shown.append)
        self.assertEqual(result.model, "small")
        self.assertIsNone(result.escalated)
        self.assertEqual("".join(shown), "small の回答です。")

    def test_wrong_language_is_redone_and_recorded(self):
        app, backend = make(answers={"small": CHINESE}, logprob={"small": -0.01})
        result = app.run("太郎と花子の年齢の和は25歳です。花子は何歳?", role="general")
        self.assertEqual(result.model, "big")
        self.assertEqual(result.escalated["reason"], "lang")
        self.assertIn("日本語", backend.calls[0][1][0]["content"])  # 最初から日本語で答えるよう指示している
        rejected = [r for r in app.store._query("SELECT * FROM interactions WHERE source = 'rejected'")]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["model"], "small")

    def test_best_model_is_not_gated(self):
        app, backend = make(models=(BIG,), logprob={"big": -0.9})
        result = app.run("ハワイの州都はどこ?", role="general")
        self.assertEqual(result.model, "big")
        self.assertIsNone(result.escalated)

    def test_cascade_can_be_turned_off(self):
        app, backend = make(logprob={"small": -0.6}, cascade=False)
        self.assertEqual(app.run("ハワイの州都はどこ?", role="general").model, "small")

    def test_prefers_loaded_model_when_close(self):
        app, backend = make()
        app.store.log_bench(label="big", suite="s", n=10, correct=10, route_n=0, route_correct=0, avg_tokens=10, avg_latency_s=1.0,
                            by_role=json.dumps({"general": [5, 5, 1.0], "reason": [5, 5, 5.0]}))  # 一般の質問はどちらも同じ速さ
        backend.loaded = {"big"}
        self.assertEqual(app._pick_model("general").name, "big")  # 読み込み済みなら、追い出しを避けて使い続ける
        backend.loaded = {"small"}
        self.assertEqual(app._pick_model("general").name, "small")

    def test_forced_model_is_not_escalated(self):
        app, backend = make(logprob={"small": -0.6})
        self.assertEqual(app.run("ハワイの州都はどこ?", model="small").model, "small")


class ThresholdTest(unittest.TestCase):
    def test_learns_from_items(self):
        draft = {f"t{i}": {"ok": i >= 6, "confidence": -0.5 + i * 0.05, "latency_s": 0.2} for i in range(12)}
        fallback = {f"t{i}": {"ok": True, "confidence": -0.01, "latency_s": 1.0} for i in range(12)}
        t = learn_threshold(draft, fallback)
        # 誤答(i<6, 確からしさ -0.25 未満)はすべて答え直し、正解は答え直さない
        self.assertGreater(t, -0.5 + 5 * 0.05)
        self.assertLessEqual(t, -0.5 + 6 * 0.05 + 1e-6)
        self.assertIsNone(learn_threshold({"a": draft["t0"]}, fallback))

    def test_bench_records_items_and_threshold_is_learned(self):
        app, backend = make(logprob={"small": -0.3, "big": -0.01})
        tasks = [{"id": f"q{i}", "role": "general", "prompt": f"質問{i}は?", "check": {"any": ["回答"]}} for i in range(12)]
        self.assertEqual(app.cascade_threshold("small", "big"), DEFAULT_CASCADE_THRESHOLD)
        run_bench(app, tasks, model="small")
        run_bench(app, tasks, model="big")
        items = app.store.bench_items("small")
        self.assertEqual(len(items), 12)
        self.assertAlmostEqual(items["q0"]["confidence"], -0.3)
        # どちらも全問正解なら、答え直さないのが最善(境目は最も低い確からしさより下)
        self.assertLess(app.cascade_threshold("small", "big"), -0.3)


class GroundTest(unittest.TestCase):
    def test_factual_detection_and_terms(self):
        self.assertTrue(ground.looks_factual("日本で一番長い川は?"))
        self.assertTrue(ground.looks_factual("ペニシリンを発見したのは誰?"))
        self.assertFalse(ground.looks_factual("17×23を計算して"))
        self.assertFalse(ground.looks_factual("夏休みの思い出を作文して書いてください"))
        self.assertFalse(ground.looks_factual("なぜ空は青いの?"))
        self.assertEqual(ground.search_terms("日本で一番長い川は?"), "日本 最長 川")
        self.assertEqual(ground.search_terms("日本の初代内閣総理大臣は誰ですか?"), "日本 初代 内閣総理大臣")
        self.assertEqual(ground.search_terms("太陽系で一番大きい惑星は?"), "太陽系 最大 惑星")

    def test_ranking_lists_say_which_is_first(self):
        # 「長さ順の日本の川一覧」の頭は名前の並びだけなので、1位と2位を書き添える
        text = "信濃川 利根川 石狩川 天塩川 長さ順の日本の川一覧（ながさじゅんのにほんのかわいちらん）は、日本の河川について長さ順に列挙した一覧である。"
        self.assertEqual(ground.ranking_note("長さ順の日本の川一覧", text), "(この一覧は長さの順。1位は信濃川、2位は利根川)")
        self.assertEqual(ground.ranking_note("長さ順の日本の川一覧", "長さ順の日本の川一覧（…）は、日本の河川について"), "")
        self.assertEqual(ground.ranking_note("川", "長く水量も多い支流は珍しくない。"), "")

    def test_unsure_fact_is_grounded(self):
        materials = [{"title": "信濃川", "url": "https://ja.wikipedia.org/wiki/信濃川", "text": "信濃川は日本最長の川。"}]
        app, backend = make(answers={"big": "日本で一番長い川は長良川です。"}, models=(BIG,), logprob={"big": -0.2}, tools_net=True)
        with mock.patch.object(ground, "lookup", return_value=materials) as lookup:
            result = app.run("日本で一番長い川は?", role="general")
        lookup.assert_called_once()
        self.assertEqual(result.sources, [{"title": "信濃川", "url": "https://ja.wikipedia.org/wiki/信濃川"}])
        self.assertIn("信濃川は日本最長の川", backend.calls[-1][1][0]["content"])

    def test_confident_fact_is_not_looked_up(self):
        app, backend = make(models=(BIG,), logprob={"big": -0.001}, tools_net=True)
        with mock.patch.object(ground, "lookup", return_value=[]) as lookup:
            result = app.run("日本で一番長い川は?", role="general")
        lookup.assert_not_called()
        self.assertEqual(result.sources, [])

    def test_no_lookup_without_network_permission(self):
        app, backend = make(models=(BIG,), logprob={"big": -0.5})
        with mock.patch.object(ground, "lookup", return_value=[]) as lookup:
            app.run("日本で一番長い川は?", role="general")
        lookup.assert_not_called()

    def test_draft_is_kept_when_nothing_found(self):
        app, backend = make(answers={"big": "日本で一番長い川は長良川です。"}, models=(BIG,), logprob={"big": -0.2}, tools_net=True)
        shown = []
        with mock.patch.object(ground, "lookup", return_value=[]):
            result = app.run("日本で一番長い川は?", role="general", on_token=shown.append)
        self.assertEqual(len(backend.calls), 1)  # 資料がなければ、書き終えた回答をそのまま使う
        self.assertEqual("".join(shown), result.text)


class RoutingGuardTest(unittest.TestCase):
    def test_implausible_role_falls_back(self):
        self.assertTrue(plausible_role("code", "Pythonで回文判定の関数を書いて"))
        self.assertTrue(plausible_role("code", "FizzBuzzを書いて"))
        self.assertFalse(plausible_role("code", "次の文からメールアドレスだけを抜き出して: a@example.com"))
        self.assertFalse(plausible_role("extract", "オーストラリアの首都は?"))
        self.assertTrue(plausible_role("general", "なんでも"))
        # 管理AIが手がかりのない担当を選んだら、キーワードでの振り分けに切り替える
        backend = MockBackend("mock", lambda model, messages, schema: json.dumps({"role": "code"}) if schema else "回答", [SMALL])
        app = Celestial(Config(home=Path("unused")), backends={"mock": backend}, store=Store())
        self.assertEqual(app.run("平安京に都が移されたのは西暦何年?").route_source, "heuristic")
        self.assertEqual(app.run("次の文からメールアドレスだけを抜き出して: a@example.com").role, "extract")
        self.assertEqual(app.run("Pythonで回文判定の関数を書いて").role, "code")

    def test_attachment_is_not_used_for_routing(self):
        task = "このメモの決定事項を箇条書きにして\n\n[添付ファイル: 会議メモ.txt]\n````\n- 公開日は12月1日\n```code```\n````"
        self.assertEqual(routing_text(task), "このメモの決定事項を箇条書きにして\n(文書ファイルが添付されている)")
        self.assertEqual(routing_text("添付なし"), "添付なし")


class LoopCutTest(unittest.TestCase):
    def test_cuts_repetition_at_sentence_end(self):
        looped = "答えは次のとおりです。" + "同じ文をずっと繰り返してしまう小さなモデルの出力の例です。" * 12
        self.assertEqual(loop_cut(looped), "答えは次のとおりです。同じ文をずっと繰り返してしまう小さなモデルの出力の例です。")
        self.assertEqual(loop_cut("あいう" * 200), "あいう")

    def test_leaves_normal_text(self):
        text = "正常な文章です。" + "".join(f"{i}番目の項目について、それぞれ違う内容を書いています。" for i in range(40))
        self.assertIsNone(loop_cut(text))
        self.assertIsNone(loop_cut("短い"))


if __name__ == "__main__":
    unittest.main()
