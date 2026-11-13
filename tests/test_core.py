import json
import tempfile
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.bench import check, extract_code, load_tasks, run_bench
from celestial.evolve import LearnedRouter, Store
from celestial.textsim import cosine, task_hash, vectorize

SMALL = ModelInfo("small", "mock", int(1e9))
BIG = ModelInfo("big", "mock", int(5e9))


def responder(model, messages, schema):
    user = messages[-1]["content"]
    props = (schema or {}).get("properties", {})
    if "role" in props:
        if "分解" in user and "complex" in props["role"]["enum"]:
            return json.dumps({"role": "complex"})
        return json.dumps({"role": "code" if "コード" in user else "translate" if "翻訳" in user else "general"})
    if "subtasks" in props:
        return json.dumps({"subtasks": [{"role": "code", "task": "コードを書く"}, {"role": "translate", "task": "翻訳する"}]})
    if "ok" in props:
        return json.dumps({"ok": "small" not in user, "issue": "wrong"})
    return f"{model}:{user}"


def make(models=(SMALL,), **cfg):
    backend = MockBackend("mock", responder, list(models))
    app = Celestial(Config(home=Path("unused"), **cfg), backends={"mock": backend}, store=Store())
    return app, backend


class TextSimTest(unittest.TestCase):
    def test_similarity(self):
        a, b, c = vectorize("Pythonでソート関数を書いて"), vectorize("Pythonでソートする関数を書いて"), vectorize("今日の天気は?")
        self.assertGreater(cosine(a, b), 0.7)
        self.assertLess(cosine(a, c), 0.2)
        self.assertEqual(task_hash("Hello  World"), task_hash(" hello world "))


class RouterTest(unittest.TestCase):
    def test_learns(self):
        router = LearnedRouter()
        self.assertEqual(router.predict("何か"), (None, 0.0))
        for i in range(8):
            router.add(f"Pythonで{i}番目の関数のコードを書いて", "code")
            router.add(f"この文章{i}を英語に翻訳して", "translate")
        role, conf = router.predict("Pythonでファイルを読むコードを書いて")
        self.assertEqual(role, "code")
        self.assertGreater(conf, 0.85)
        self.assertEqual(router.predict("次の文を英語に翻訳して")[0], "translate")


class ManagerTest(unittest.TestCase):
    def test_routes_and_caches(self):
        app, backend = make()
        first = app.run("コードを書いて: FizzBuzz")
        self.assertEqual((first.role, first.route_source, first.source), ("code", "manager", "llm"))
        calls = len(backend.calls)
        second = app.run("コードを書いて:  fizzbuzz")
        self.assertEqual((second.source, second.tokens, second.text), ("cache", 0, first.text))
        self.assertEqual(len(backend.calls), calls)  # LLMを一切呼んでいない

        app.feedback(second.id, good=False)  # キャッシュへの悪評価は元の回答を無効化する
        third = app.run("コードを書いて: FizzBuzz")
        self.assertEqual(third.source, "llm")

    def test_learned_routing_skips_manager(self):
        app, backend = make()
        for i in range(8):
            app.run(f"Pythonで{i}番目の関数のコードを書いて")
            app.run(f"この文章{i}を英語に翻訳して")
        backend.calls.clear()
        r = app.run("Pythonでファイルを読むコードを書いて")
        self.assertEqual((r.role, r.route_source), ("code", "learned"))
        self.assertEqual(len(backend.calls), 1)  # ワーカーの1回だけ

    def test_bandit_follows_feedback(self):
        app, _ = make(models=(SMALL, BIG), cache=False, explore=0.05)
        seen = set()
        for i in range(12):
            r = app.run(f"質問{i}", role="general")
            seen.add(r.model)
            app.feedback(r.id, good=(r.model == "big"))
        self.assertEqual(seen, {"small", "big"})
        self.assertEqual([app.run(f"追加{i}", role="general").model for i in range(3)], ["big"] * 3)

    def test_fewshot_from_good_feedback(self):
        app, backend = make(cache=False)
        r = app.run("コードを書いて: 配列を逆順にする")
        app.feedback(r.id, good=True)
        r2 = app.run("コードを書いて: 配列を逆順にして表示する")
        self.assertEqual(r2.examples_used, 1)
        self.assertIn("配列を逆順にする", [m["content"] for m in backend.calls[-1][1]][1])

    def test_swarm(self):
        app, _ = make(swarm_min_chars=1)
        r = app.run("分解して: コードを書いて翻訳もして")
        self.assertEqual((r.role, r.source), ("complex", "swarm"))
        self.assertEqual([s.role for s in r.subresults], ["code", "translate"])
        self.assertIn("コードを書く", r.text)
        self.assertEqual(len(app.store.top_level()), 1)

    def test_verify_escalates(self):
        app, _ = make(models=(SMALL, BIG))
        r = app.run("質問です", role="general", model=None, verify=True)
        self.assertEqual(r.model, "big")  # small の回答は不合格 → big で再回答
        self.assertEqual(app.store.model_stats("general")["small"][0], 1)

    def test_persistence_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "c.db"
            backend = MockBackend("mock", responder, [SMALL])
            app = Celestial(Config(home=Path(tmp)), backends={"mock": backend}, store=Store(db))
            r = app.run("コードを書いて: hello")
            app.feedback(r.id, good=True)
            app.store._db.close()

            app2 = Celestial(Config(home=Path(tmp)), backends={"mock": backend}, store=Store(db))
            self.assertEqual(app2.router.examples, 1)
            self.assertEqual(app2.run("コードを書いて: hello").source, "cache")
            out = Path(tmp) / "data.jsonl"
            self.assertEqual(app2.export(out), 1)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["quality"], "good")
            app2.store._db.close()


class BenchTest(unittest.TestCase):
    def test_checkers(self):
        self.assertTrue(check("17×23 = 300 + 91\n**答え: 391**", {"number": 391}))
        self.assertTrue(check("合計は1,050円です。", {"number": 1050}))
        self.assertFalse(check("391 ではなく 381 です", {"number": 391, "none": ["ではなく"]}))
        self.assertFalse(check("途中で391が出ます\n答えは 400", {"number": 391}))
        self.assertTrue(check("H₂O です", {"any": ["h2o"]}))
        self.assertTrue(check('```json\n{"name": "田中太郎", "age": "34"}\n```', {"json": {"name": "田中太郎", "age": 34}}))
        self.assertFalse(check("長い" * 30, {"max_chars": 20}))
        self.assertIsNone(check("x", {"tests": "assert True"}))

    def test_code_check(self):
        answer = "こうです:\n```python\ndef add(a, b):\n    return a + b\n```\n使用例:\n```python\nprint(add(1, 2))\n```"
        self.assertEqual(len(extract_code(answer)), 2)
        self.assertTrue(check(answer, {"tests": "assert add(2, 3) == 5"}, run_code=True))
        self.assertFalse(check(answer, {"tests": "assert add(2, 3) == 6"}, run_code=True))

    def test_builtin_tasks_are_wellformed(self):
        tasks = load_tasks()
        self.assertEqual(len({t["id"] for t in tasks}), len(tasks))
        for t in tasks:
            check("dummy", t["check"])  # 例外が出ないこと

    def test_run_bench_does_not_learn(self):
        app, backend = make()
        tasks = [
            {"id": "a", "role": "code", "prompt": "コードを書いて", "check": {"all": ["small:コード"]}},
            {"id": "b", "role": "translate", "prompt": "こんにちは", "check": {"all": ["存在しない語"]}},
            {"id": "c", "role": "code", "prompt": "コード", "check": {"tests": "assert True"}},
        ]
        report = run_bench(app, tasks)
        self.assertEqual((report.n, report.correct, report.skipped), (2, 1, 1))
        self.assertEqual((len(report.routed), report.route_correct), (2, 1))
        self.assertEqual(app.store.top_level(), [])  # 履歴を汚さない
        self.assertEqual(app.router.examples, 0)
        self.assertEqual(len(app.store.bench_history()), 1)


if __name__ == "__main__":
    unittest.main()
