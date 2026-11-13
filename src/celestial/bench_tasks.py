"""組み込みの計測タスク集。答えが機械的に採点できるものだけを集めている。

check のキー(指定したものすべてを満たせば正解):
  number    最後に数字が出てくる行に、この数値が含まれる
  all/any   これらの語をすべて/どれか含む   none  どれも含まない
  regex     正規表現に一致する
  json      回答中のJSONが、これらのキーと値を持つ
  max_chars 回答がこの文字数以内
  tests     回答のPythonコードに続けて実行し、例外が出なければ正解(--run-code 指定時のみ)
"""

_S1 = ("小型のAIを複数組み合わせ、管理AIが仕事を振り分ける手法が注目されている。大型モデルを1つ動かすよりも少ない計算資源で"
       "済み、得意分野ごとに担当を分けることで精度も保ちやすい。一方で、振り分けを誤ると品質が落ちるため、管理AIの判断の質が全体の鍵になる。")
_S2 = ("この図書館は来月から開館時間を変更する。平日はこれまでより1時間長く午後9時まで開館し、仕事帰りの利用者を増やす狙いだ。"
       "一方、利用の少ない日曜日は午後5時に閉館する。貸出期間の2週間は変わらない。")

BUILTIN: list[dict] = [
    # ---- reason ----
    {"id": "reason-mul", "role": "reason", "prompt": "17×23を計算して", "check": {"number": 391}},
    {"id": "reason-apple", "role": "reason", "prompt": "りんごが3個で450円のとき、7個ではいくら?", "check": {"number": 1050}},
    {"id": "reason-sum", "role": "reason", "prompt": "1から100までの整数の合計は?", "check": {"number": 5050}},
    {"id": "reason-speed", "role": "reason", "prompt": "時速60kmで2時間30分走ると何km進む?", "check": {"number": 150}},
    {"id": "reason-eq", "role": "reason", "prompt": "ある数に5を足してから3倍すると36になる。ある数はいくつ?", "check": {"number": 7}},
    {"id": "reason-pow", "role": "reason", "prompt": "2の10乗はいくつ?", "check": {"number": 1024}},
    {"id": "reason-age", "role": "reason", "prompt": "太郎は花子より3歳年上で、2人の年齢の合計は25歳。花子は何歳?", "check": {"number": 11}},
    {"id": "reason-tax", "role": "reason", "prompt": "1000円の商品を20%引きにし、そこに消費税10%を加えると支払額は何円?", "check": {"number": 880}},
    # ---- general ----
    {"id": "general-fuji", "role": "general", "prompt": "日本で一番高い山は?", "check": {"any": ["富士"]}},
    {"id": "general-water", "role": "general", "prompt": "水の化学式は?", "check": {"any": ["h2o"]}},
    {"id": "general-paris", "role": "general", "prompt": "フランスの首都は?", "check": {"any": ["パリ", "paris"]}},
    {"id": "general-rgb", "role": "general", "prompt": "光の三原色を挙げて", "check": {"all": ["赤", "緑", "青"]}},
    {"id": "general-jupiter", "role": "general", "prompt": "太陽系で一番大きい惑星は?", "check": {"any": ["木星", "jupiter"]}},
    {"id": "general-soseki", "role": "general", "prompt": "『吾輩は猫である』の作者は?", "check": {"any": ["漱石"]}},
    # ---- translate ----
    {"id": "translate-cat", "role": "translate", "prompt": "次を英語に翻訳して: 猫が好きです", "check": {"any": ["like cats", "love cats"]}},
    {"id": "translate-rain", "role": "translate", "prompt": "次を英語に翻訳して: 明日は雨が降りそうです", "check": {"all": ["rain", "tomorrow"]}},
    {"id": "translate-thanks", "role": "translate", "prompt": "次を日本語に翻訳して: Thank you very much.", "check": {"any": ["ありがとう"]}},
    {"id": "translate-library", "role": "translate", "prompt": "次を日本語に翻訳して: The library is closed on Mondays.", "check": {"all": ["図書館", "月曜"]}},
    {"id": "translate-station", "role": "translate", "prompt": "次を英語に翻訳して: 駅はどこですか?", "check": {"all": ["where", "station"]}},
    # ---- extract ----
    {"id": "extract-person", "role": "extract",
     "prompt": "次の文から名前と年齢を抽出し、キーが name と age のJSONだけを出力して: 田中太郎さんは34歳の会社員です。",
     "check": {"json": {"name": "田中太郎", "age": 34}}},
    {"id": "extract-email", "role": "extract",
     "prompt": "次の文からメールアドレスだけを抜き出して: お問い合わせは support@example.com までお願いします。",
     "check": {"all": ["support@example.com"], "max_chars": 40}},
    {"id": "extract-sentiment", "role": "extract",
     "prompt": "次のレビューを positive か negative のどちらか1語で分類して: この商品は最悪でした。二度と買いません。",
     "check": {"all": ["negative"], "none": ["positive"]}},
    {"id": "extract-date", "role": "extract",
     "prompt": "次の文の日付を YYYY-MM-DD 形式で、日付だけ出力して: 会議は2025年3月14日に開かれます。",
     "check": {"all": ["2025-03-14"], "max_chars": 30}},
    {"id": "extract-csv", "role": "extract",
     "prompt": "次の3つをカンマ区切りの1行に直して、その1行だけ出力して: りんご、みかん、ぶどう",
     "check": {"regex": r"りんご\s*,\s*みかん\s*,\s*ぶどう", "max_chars": 30}},
    # ---- summarize ----
    {"id": "summarize-swarm", "role": "summarize", "prompt": f"次の文章を60字以内で要約して: {_S1}",
     "check": {"all": ["管理ai"], "any": ["小型", "複数"], "max_chars": 90}},
    {"id": "summarize-library", "role": "summarize", "prompt": f"次の文章を60字以内で要約して: {_S2}",
     "check": {"all": ["図書館"], "any": ["開館", "時間"], "max_chars": 90}},
    # ---- 事実(小さいモデルが作り話をしやすい問題) ----
    {"id": "fact-river", "role": "general", "prompt": "日本で一番長い川は?", "check": {"any": ["信濃川"]}},
    {"id": "fact-lake", "role": "general", "prompt": "日本で一番大きい湖は?", "check": {"any": ["琵琶湖"]}},
    {"id": "fact-pref-area", "role": "general", "prompt": "日本で一番面積が大きい都道府県は?", "check": {"any": ["北海道"]}},
    {"id": "fact-everest", "role": "general", "prompt": "世界で一番高い山は?", "check": {"any": ["エベレスト", "チョモランマ", "everest"]}},
    {"id": "fact-bones", "role": "general", "prompt": "大人の人間の骨はおよそ何本?", "check": {"any": ["206"]}},
    {"id": "fact-fe", "role": "general", "prompt": "元素記号 Fe はどの元素?", "check": {"any": ["鉄", "iron"]}},
    {"id": "fact-prefectures", "role": "general", "prompt": "日本の都道府県はいくつある?", "check": {"any": ["47"]}},
    {"id": "fact-genji", "role": "general", "prompt": "『源氏物語』の作者は?", "check": {"any": ["紫式部"]}},
    {"id": "fact-first-pm", "role": "general", "prompt": "日本の初代内閣総理大臣は?", "check": {"any": ["伊藤博文"]}},
    {"id": "fact-mercury", "role": "general", "prompt": "太陽系で太陽に一番近い惑星は?", "check": {"any": ["水星", "mercury"]}},
    {"id": "fact-fuji-height", "role": "general", "prompt": "富士山の標高は約何メートル?", "check": {"any": ["3776", "3,776", "3775", "3,775"]}},
    {"id": "fact-capital-au", "role": "general", "prompt": "オーストラリアの首都は?", "check": {"any": ["キャンベラ", "canberra"]}},
    {"id": "fact-penicillin", "role": "general", "prompt": "ペニシリンを発見した人物は?", "check": {"any": ["フレミング", "fleming"]}},
    {"id": "fact-largest-ocean", "role": "general", "prompt": "世界で一番広い海洋は?", "check": {"any": ["太平洋", "pacific"]}},
    {"id": "fact-heian", "role": "general", "prompt": "平安京に都が移されたのは西暦何年?", "check": {"any": ["794"]}},
    {"id": "fact-dna", "role": "general", "prompt": "DNAの二重らせん構造を発表した2人の科学者は?", "check": {"all": ["ワトソン", "クリック"]}},
    # ---- 計算(複数の手順が要る問題) ----
    {"id": "reason-day-seconds", "role": "reason", "prompt": "1日は何秒?", "check": {"number": 86400}},
    {"id": "reason-lcm", "role": "reason", "prompt": "12と18の最小公倍数は?", "check": {"number": 36}},
    {"id": "reason-kmh", "role": "reason", "prompt": "時速72kmは秒速何メートル?", "check": {"number": 20}},
    {"id": "reason-discount", "role": "reason", "prompt": "1個240円のパンを3割引きで5個買うと、合計いくら?", "check": {"number": 840}},
    {"id": "reason-linear", "role": "reason", "prompt": "ある数を3倍して7を引くと20になる。ある数はいくつ?", "check": {"number": 9}},
    {"id": "reason-average", "role": "reason", "prompt": "72点、85点、91点、64点の平均点は?", "check": {"number": 78}},
    {"id": "reason-work", "role": "reason", "prompt": "Aさん1人だと6時間、Bさん1人だと3時間かかる仕事を2人でやると何時間かかる?", "check": {"number": 2}},
    {"id": "reason-clock", "role": "reason", "prompt": "午前9時45分から3時間50分後は何時何分?", "check": {"regex": r"(午後\s*1\s*時\s*35\s*分|13\s*[:時]\s*35)"}},
    # ---- code(--run-code 指定時のみ採点) ----
    {"id": "code-sum-even", "role": "code",
     "prompt": "Pythonで、整数のリストを受け取り偶数だけの合計を返す関数 sum_even(nums) を書いて",
     "check": {"tests": "assert sum_even([1, 2, 3, 4]) == 6\nassert sum_even([]) == 0\nassert sum_even([-2, 5]) == -2"}},
    {"id": "code-palindrome", "role": "code",
     "prompt": "Pythonで、英数字以外を無視し大文字小文字を区別せずに回文かどうかを返す関数 is_palindrome(s) を書いて",
     "check": {"tests": "assert is_palindrome('A man, a plan, a canal: Panama') is True\nassert is_palindrome('hello') is False\nassert is_palindrome('') is True"}},
    {"id": "code-fib", "role": "code",
     "prompt": "Pythonで、n番目のフィボナッチ数を返す関数 fib(n) を書いて。fib(0)=0, fib(1)=1 とする",
     "check": {"tests": "assert [fib(i) for i in range(8)] == [0, 1, 1, 2, 3, 5, 8, 13]\nassert fib(30) == 832040"}},
    {"id": "code-count-words", "role": "code",
     "prompt": "Pythonで、空白区切りの単語の出現回数を辞書で返す関数 count_words(text) を書いて",
     "check": {"tests": "assert dict(count_words('a b a')) == {'a': 2, 'b': 1}\nassert dict(count_words('')) == {}"}},
    {"id": "code-flatten", "role": "code",
     "prompt": "Pythonで、任意の深さにネストしたリストを平坦なリストにして返す関数 flatten(lst) を書いて",
     "check": {"tests": "assert list(flatten([1, [2, [3, 4]], 5])) == [1, 2, 3, 4, 5]\nassert list(flatten([])) == []"}},
]
