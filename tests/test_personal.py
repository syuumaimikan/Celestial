"""利用者に合わせる機能(AIへの指示・覚えていること)の試験。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from celestial import Celestial, Config
from celestial.backends import MockBackend, ModelInfo
from celestial.evolve import Store
import datetime

from celestial import web
from celestial.manager import date_answer, date_facts, memory_command, news_topic, personal_prompt, time_context, time_sensitive

_FEED = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>AI - Google News</title>
<item><title>古いAIの話題 - 東西新聞</title><link>https://news.example/old</link><pubDate>Mon, 09 Nov 2026 12:00:00 GMT</pubDate>
<description>&lt;a href="https://news.example/old"&gt;古いAIの話題&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;東西新聞&lt;/font&gt;</description>
<source url="https://touzai.example">東西新聞</source></item>
<item><title>新しいAIの規制案 - Reuters</title><link>https://news.example/new</link><pubDate>Thu, 12 Nov 2026 12:00:00 GMT</pubDate>
<description>&lt;a href="https://news.example/new"&gt;新しいAIの規制案&lt;/a&gt;</description><source url="https://reuters.example">Reuters</source></item>
<item><title>新しいAIの規制案 - Reuters</title><link>https://news.example/dup</link><pubDate>Thu, 12 Nov 2026 12:00:00 GMT</pubDate>
<source url="https://reuters.example">Reuters</source></item>
</channel></rss>"""
_EMPTY_FEED = '<?xml version="1.0"?><rss version="2.0"><channel><title>none</title></channel></rss>'


class NewsTest(unittest.TestCase):
    def test_topic_of_news_requests(self):
        cases = {
            "aiに関する最新ニュースをたくさん取得し、要約して": "ai",  # 実際にスマホから来た依頼
            "生成AIについての最近のニュースをまとめて": "生成AI",
            "最新のAIニュースを教えて": "AI",
            "大谷翔平選手の最新ニュース": "大谷翔平選手",
            "AIや半導体のニュースを3つ": "AI OR 半導体",
            "日本の経済ニュースを要約して": "経済",
            "今日のニュースを3つ教えて": "",
            "最新のニュースを要約して": "",
            "今日の主なニュースは?": "",
            "ニュースを教えて": "",
            "Give me the latest AI news": "AI",
            "What's the latest news about Tesla?": "Tesla",
            "Summarize today's top news": "",
            "latest tech news please": "tech",
        }
        for text, topic in cases.items():
            self.assertEqual(news_topic(text), topic, text)
        # ニュースレターやニュースリリースの文面づくりは、最新のニュースの依頼ではない
        self.assertFalse(time_sensitive("ニュースレターの文面を書いて"))
        self.assertFalse(time_sensitive("Write a news release for our product"))

    def test_feed_is_chosen_by_topic(self):
        self.assertEqual(web.news_feed("ja", ""), web.NEWS_FEEDS["ja"])  # 話題なし → 主なニュース
        self.assertEqual(web.news_feed("ja", "経済"), ("NHK 経済", "https://www.nhk.or.jp/rss/news/cat5.xml"))  # 分野 → その分野
        self.assertEqual(web.news_feed("ja", "海外")[0], "NHK 国際")
        self.assertEqual(web.news_feed("en", "Tech")[0], "BBC Technology")
        source, url = web.news_feed("ja", "AI")  # ほかの話題 → 直近1週間を検索
        self.assertEqual(source, "Google ニュース")
        self.assertTrue(url.startswith(web.NEWS_SEARCH) and "q=AI+when%3A7d" in url and "hl=ja" in url, url)

    def test_search_results_are_picked_then_newest_first(self):
        from unittest import mock

        with mock.patch("celestial.web._get", return_value=(_FEED.encode(), "application/xml")):
            source, items = web.news("ja", topic="AI")
        self.assertEqual(source, "Google ニュース")
        self.assertEqual([i["title"] for i in items], ["新しいAIの規制案", "古いAIの話題"])  # 新しい順・同じ見出しは1つ
        self.assertEqual((items[0]["outlet"], items[0]["summary"]), ("Reuters", ""))  # 媒体名は分け、見出しの繰り返しは省く
        # 直近1週間に見つからなければ、期間を限らずに探し直す
        with mock.patch("celestial.web._get", side_effect=[(_EMPTY_FEED.encode(), "application/xml"),
                                                           (_FEED.encode(), "application/xml")]) as get:
            _, items = web.news("ja", topic="めずらしい話題")
        self.assertEqual(len(items), 2)
        self.assertIn("when%3A7d", get.call_args_list[0][0][0])
        self.assertNotIn("when", get.call_args_list[1][0][0])

    def test_spam_ads_and_floods_are_left_out(self):
        from unittest import mock

        # 実際の「AI」の検索では、見えない文字と {SxC-00} の残った迷惑な記事が、1つの媒体から99件中75件来た
        new, old = "Fri, 13 Nov 2026 12:00:00 GMT", "Thu, 12 Nov 2026 12:00:00 GMT"
        rows = [("別の媒体の記事", "別の媒体", old), ("‍‍AI購入ガイド {SxC-00}", "迷惑", new), ("AIの新製品(PR)", "広告", new),
                ("iシェアーズ AI グローバル・イノベーション アクティブ【408A】：株価・株式情報", "Yahoo!ファイナンス", new)]
        rows += [(f"同じ媒体の記事{n}", "同じ媒体", new) for n in range(5)]
        feed = "".join(f"<item><title>{t} - {o}</title><link>https://news.example/{n}</link><pubDate>{d}</pubDate>"
                       f"<source url=\"https://{n}.example\">{o}</source></item>" for n, (t, o, d) in enumerate(rows))
        feed = f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>{feed}</channel></rss>'
        with mock.patch("celestial.web._get", return_value=(feed.encode(), "application/xml")):
            _, items = web.news("ja", topic="AI")
        self.assertEqual([i["title"] for i in items], ["同じ媒体の記事0", "同じ媒体の記事1", "同じ媒体の記事2", "別の媒体の記事"])


class MemoryCommandTest(unittest.TestCase):
    def test_remember(self):
        for text, fact in (("覚えて: 私の名前は佐藤です", "私の名前は佐藤です"),
                           ("覚えておいてください、締め切りは毎月25日", "締め切りは毎月25日"),
                           ("私の誕生日は5月3日だと覚えておいて", "私の誕生日は5月3日"),
                           ("猫を2匹飼っていることを覚えてね。", "猫を2匹飼っている"),
                           ("Remember that I prefer metric units", "I prefer metric units")):
            self.assertEqual(memory_command(text), ("remember", fact), text)

    def test_forget_and_recall(self):
        self.assertEqual(memory_command("忘れて: 締め切り"), ("forget", "締め切り"))
        self.assertEqual(memory_command("誕生日のことは忘れて"), ("forget", "誕生日"))
        self.assertEqual(memory_command("何を覚えてる?"), ("recall", ""))
        self.assertEqual(memory_command("覚えていることを教えて"), ("recall", ""))

    def test_ordinary_requests_are_not_commands(self):
        for text in ("英単語を効率よく覚える方法は?", "この手順を覚えておいて", "それを覚えて",
                     "次の文章を覚えて、要約して", "覚えておくと便利な Excel の関数を教えて", "忘れ物を防ぐコツは?"):
            self.assertIsNone(memory_command(text), text)

    def test_personal_prompt(self):
        self.assertEqual(personal_prompt("", []), "")
        text = personal_prompt("敬語を使わず簡潔に", ["私の名前は佐藤"])
        self.assertIn("敬語を使わず簡潔に", text)
        self.assertIn("- 私の名前は佐藤", text)


class TimeContextTest(unittest.TestCase):
    def test_only_time_related_requests_get_the_date(self):
        for text in ("今日は何曜日?", "私の誕生日まであと何日?", "来週の会議の議事録を作って", "What day is it today?", "締め切りまでの段取りを考えて"):
            self.assertTrue(time_sensitive(text), text)
        for text in ("挨拶の例を1つ", "日本一長い川は?", "17 * 23 は?", "Write a poem about the moon", "I know the answer"):
            self.assertFalse(time_sensitive(text), text)
        now = time.mktime((2026, 11, 13, 17, 5, 0, 0, 0, -1))
        self.assertIn("2026年11月13日(金曜日)", time_context("今日は何曜日?", now))
        self.assertIn("17時05分", time_context("今何時?", now))
        self.assertIn("Friday, November 13, 2026", time_context("What day is it today?", now))
        self.assertEqual(time_context("挨拶の例を1つ", now), "")

    def test_date_facts_are_computed_not_guessed(self):
        # 小さいAIは日付の引き算を誤る(「大晦日まで260日」など)ので、計算した値を添える
        today = datetime.date(2026, 11, 13)  # 金曜日
        self.assertEqual(date_facts("今年の大晦日まであと何日?", [], today), ["大晦日(12月31日(木))まで あと48日"])
        self.assertEqual(date_facts("私の誕生日まであと何日?", ["私の名前は佐藤", "誕生日は5月3日"], today),
                         ["誕生日(2027年5月3日(月))まで あと171日"])  # 覚えていることの日付も使う
        self.assertEqual(date_facts("今日から100日後は何日?", [], today), ["100日後は 2027年2月21日(日)"])
        self.assertEqual(date_facts("3か月後の日付は?", [], today), ["3か月後は 2027年2月13日(土)"])
        self.assertEqual(date_facts("2020年1月1日から何日経った?", [], today), ["2020年1月1日(水)から今日まで 2508日"])
        self.assertEqual(date_facts("1か月後は?", [], datetime.date(2026, 1, 31)), ["1か月後は 2月28日(土)"])  # 月末はそろえる
        self.assertEqual(date_facts("2月30日まであと何日?", [], today), [])  # ない日付は計算しない
        now = time.mktime((2026, 11, 13, 9, 0, 0, 0, 0, -1))
        self.assertIn("計算済みの値", time_context("大晦日まであと何日?", now))

    def test_simple_date_questions_are_answered_by_calculation(self):
        today = datetime.date(2026, 11, 13)
        self.assertEqual(date_answer("今日は何曜日?", [], today), "今日は 2026年11月13日(金曜日)です。")
        self.assertEqual(date_answer("5月3日は何曜日?", [], today), "2027年5月3日は月曜日です。")
        self.assertEqual(date_answer("3か月後の日付は?", [], today), "3か月後は 2027年2月13日(土)です。")
        self.assertEqual(date_answer("私の誕生日まであと何日?", ["誕生日は5月3日"], today), "誕生日(2027年5月3日(月))まで あと171日です。")
        # 条件のつく質問・答えが1つに決まらない質問・日付の質問ではない依頼は、AIに任せる
        for text in ("大晦日まで営業日で何日?", "5月3日と12月25日まで何日?", "来週の会議の議事録を作って", "今何時?"):
            self.assertIsNone(date_answer(text, [], today), text)


class PersonalAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []

        def respond(model, messages, schema):
            if schema and "role" in schema.get("properties", {}):
                return json.dumps({"role": "general"})
            self.seen.append(messages)
            return f"回答{len(self.seen)}"

        backend = MockBackend("mock", respond, [ModelInfo("small", "mock", int(1e9))])
        self.app = Celestial(Config(home=Path(self.tmp.name)), backends={"mock": backend}, store=Store())

    def tearDown(self):
        self.tmp.cleanup()

    def test_instructions_and_memories_reach_the_model(self):
        self.app.set_setting("instructions", "  敬語を使わず、短く答える  ")
        self.assertEqual(self.app.cfg.instructions, "敬語を使わず、短く答える")
        reply = self.app.run("覚えて: 私の名前は佐藤")
        self.assertEqual((reply.model, reply.source), ("メモリ", "memory"))
        self.assertIn("覚えました", reply.text)
        self.assertEqual(self.seen, [])  # 覚えるだけならAIを呼ばない
        self.app.run("自己紹介の文を書いて")
        system = self.seen[-1][0]["content"]
        self.assertIn("敬語を使わず、短く答える", system)
        self.assertIn("- 私の名前は佐藤", system)
        self.assertIn("私の名前は佐藤", self.app.run("何を覚えてる?").text)
        self.assertIn("忘れました", self.app.run("名前のことは忘れて").text)
        self.assertEqual(self.app.cfg.memories, [])
        # 計測(learn=False)では混ぜない
        self.app.run("自己紹介の文を書いて", learn=False, use_cache=False)
        self.assertNotIn("敬語を使わず", self.seen[-1][0]["content"])

    def test_cache_is_not_reused_after_changing_instructions(self):
        first = self.app.run("挨拶の例を1つ")
        self.assertEqual(self.app.run("挨拶の例を1つ").source, "cache")
        time.sleep(0.01)
        self.app.set_setting("instructions", "英語で答える")
        again = self.app.run("挨拶の例を1つ")
        self.assertNotEqual(again.source, "cache")  # 古い指示で作った回答は返さない
        self.assertNotEqual(again.text, first.text)
        self.assertEqual(self.app.run("挨拶の例を1つ").source, "cache")  # 新しい指示で作った回答は使える

    def test_dates_reach_the_model_and_skip_the_cache(self):
        direct = self.app.run("今日は何曜日?")
        self.assertEqual((direct.model, direct.source), ("計算", "calc"))  # 短い日付の質問はAIを呼ばずに計算で答える
        self.assertEqual(self.seen, [])
        self.app.run("今日の日付を入れた挨拶文を書いて")
        self.assertIn("今日は ", self.seen[-1][0]["content"])  # 今日の日付を指示文で渡す
        again = self.app.run("今日の日付を入れた挨拶文を書いて")
        self.assertNotEqual(again.source, "cache")  # 日付で答えが変わるので、キャッシュは使わない
        self.app.run("挨拶の例を1つ")
        self.assertNotIn("今日は ", self.seen[-1][0]["content"])  # 日付と関係のない依頼の指示文は変えない

    def test_latest_news_is_fetched_or_admitted(self):
        from unittest import mock

        # ネットが使えないときは、最新の情報は取得できないと最初に伝えさせる(作り話のニュースを防ぐ)
        self.app.run("最新のニュースを要約して")
        self.assertIn("インターネットに接続できず", self.seen[-1][0]["content"])
        # 使えるときは、報道機関の公開の見出し(RSS)を材料にして、出典を付ける
        self.app.cfg.tools_net = True
        items = [{"title": "接続障害が発生", "url": "https://news.example/1", "summary": "広い範囲で影響", "date": ""}]
        with mock.patch("celestial.web.news", return_value=("NHK", items)):
            result = self.app.run("今日のニュースを3つ教えて")
        self.assertIn("接続障害が発生: 広い範囲で影響", self.seen[-1][0]["content"])
        self.assertEqual(result.sources, [{"title": "接続障害が発生", "url": "https://news.example/1"}])
        self.assertNotEqual(self.app.run("今日のニュースを3つ教えて").source, "cache")  # 最新の情報はキャッシュから返さない

    def test_news_about_a_topic_is_searched(self):
        from unittest import mock

        self.app.cfg.tools_net = True
        items = [{"title": "新しいAIの規制案", "url": "https://news.example/new", "summary": "",
                  "date": "Thu, 12 Nov 2026 12:00:00 GMT", "outlet": "Reuters"}]
        with mock.patch("celestial.web.news", return_value=("Google ニュース", items)) as news:
            result = self.app.run("AIに関する最新ニュースをたくさん取得し、要約して")
        self.assertEqual(news.call_args.kwargs["topic"], "AI")
        system = self.seen[-1][0]["content"]
        self.assertIn("「AI」に関する最新のニュース(Google ニュース", system)
        self.assertIn("- 新しいAIの規制案(Reuters、11/12)", system)  # 媒体と日付を添える
        self.assertEqual(result.sources, [{"title": "新しいAIの規制案(Reuters)", "url": "https://news.example/new"}])
        # 話題のニュースが見つからなければ、そう伝えさせる(作り話を防ぐ)
        with mock.patch("celestial.web.news", return_value=("Google ニュース", [])):
            self.app.run("量子コンピューターのニュースを教えて")
        self.assertIn("「量子コンピューター」に関する最近のニュースは、ニュースの検索で見つかりませんでした", self.seen[-1][0]["content"])

    def test_settings_are_validated_and_kept(self):
        with self.assertRaises(ValueError):
            self.app.set_setting("instructions", "あ" * 1001)
        with self.assertRaises(ValueError):
            self.app.set_setting("memories", [1, 2])
        self.app.set_setting("memories", [" 猫を飼っている ", "猫を飼っている", ""])
        self.assertEqual(self.app.cfg.memories, ["猫を飼っている"])  # 空白の整理と重複の除去
        again = Celestial(Config(home=Path(self.tmp.name)), backends=self.app.backends, store=self.app.store)
        self.assertEqual(again.cfg.memories, ["猫を飼っている"])  # 次回起動時も残る


if __name__ == "__main__":
    unittest.main()
