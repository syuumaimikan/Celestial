"""よく使う、鍵(APIキー)のいらない公開 API の使い方。

小さいモデルは「天気を取るには API キーが要る」と思い込み、キーを取りに行こうとして同じ所で止まってしまう。
依頼に関係する話題のときだけ、使い方(URL と、返ってくるデータの形)を指示文に足す。
どれも登録なしで使え、2026年11月に実際に呼んで形を確かめたもの。
"""
from __future__ import annotations

import re

_WEATHER = (
    "Weather: use Open-Meteo. It needs NO API key and no sign-up, so never ask for a key or write a placeholder key. "
    "GET https://api.open-meteo.com/v1/forecast?latitude=35.68&longitude=139.76"
    "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=Asia%2FTokyo&forecast_days=3 returns JSON like "
    '{"current": {"time": "2026-11-14T13:00", "temperature_2m": 11.6, "relative_humidity_2m": 73, "weather_code": 1, '
    '"wind_speed_10m": 4.9}, "daily": {"time": ["2026-11-14", ...], "weather_code": [3, ...], "temperature_2m_max": [18.4, ...], '
    '"temperature_2m_min": [11.4, ...], "precipitation_probability_max": [2, ...]}} '
    "(units: °C, %, km/h). weather_code is a WMO code: 0 clear 快晴, 1 mainly clear 晴れ, 2 partly cloudy 晴れ時々曇り, "
    "3 overcast 曇り, 45/48 fog 霧, 51-57 drizzle 霧雨, 61-67 rain 雨, 71-77 snow 雪, 80-82 rain showers にわか雨, "
    "85/86 snow showers にわか雪, 95-99 thunderstorm 雷雨. "
    "Coordinates of cities in Japan (write them directly in the code as constants; there is no need to look them up): "
    "東京 Tokyo 35.68, 139.76 / 横浜 Yokohama 35.43, 139.65 / さいたま Saitama 35.91, 139.66 / 千葉 Chiba 35.60, 140.12 / "
    "大阪 Osaka 34.69, 135.50 / 京都 Kyoto 35.02, 135.75 / 神戸 Kobe 34.69, 135.18 / 名古屋 Nagoya 35.18, 136.91 / "
    "札幌 Sapporo 43.07, 141.35 / 仙台 Sendai 38.27, 140.87 / 新潟 Niigata 37.92, 139.04 / 金沢 Kanazawa 36.60, 136.62 / "
    "静岡 Shizuoka 34.98, 138.38 / 広島 Hiroshima 34.40, 132.45 / 岡山 Okayama 34.65, 133.93 / 福岡 Fukuoka 33.60, 130.42 / "
    "熊本 Kumamoto 32.81, 130.69 / 鹿児島 Kagoshima 31.57, 130.55 / 那覇 Naha 26.21, 127.68. "
    "For another place, the program itself looks up the coordinates from the place name when it runs (in the code, not "
    "as a separate step of the work; also no key; use the English name): "
    "https://geocoding-api.open-meteo.com/v1/search?name=Tokyo&count=1&language=ja returns "
    '{"results": [{"name": "東京都", "latitude": 35.69, "longitude": 139.69, "timezone": "Asia/Tokyo"}]}. '
    "Use Tokyo (35.68, 139.76) when the user gives no place, and keep the place easy to change (constants at the top). "
    "If the user mentions no sensor, show the outdoor temperature and humidity from this API."
)
_AIR = ("Air quality (no key): https://air-quality-api.open-meteo.com/v1/air-quality?latitude=35.68&longitude=139.76"
        '&current=pm2_5,pm10,us_aqi returns {"current": {"pm2_5": 8.1, "pm10": 12.0, "us_aqi": 34}}.')
_HOLIDAYS = ("Japanese public holidays (no key): https://holidays-jp.github.io/api/v1/date.json returns "
             '{"2026-01-01": "元日", "2026-01-12": "成人の日", ...} for last year, this year and next year.')
_RATES = ("Exchange rates (no key): https://open.er-api.com/v6/latest/USD returns "
          '{"result": "success", "base_code": "USD", "rates": {"JPY": 150.1, "EUR": 0.86, ...}} (updated daily).')
_QUAKES = ("Earthquakes in Japan (no key): https://api.p2pquake.net/v2/history?codes=551&limit=5 returns a list of "
           '{"earthquake": {"time": "2026/11/14 00:03:00", "maxScale": 20, "hypocenter": {"name": "島根県西部", '
           '"magnitude": 3.5, "depth": 10}}} (maxScale 10 = 震度1, 20 = 2, 30 = 3, 40 = 4, 45 = 5弱, 50 = 5強, '
           "55 = 6弱, 60 = 6強, 70 = 7).")
_CODE = ("In Python, call these with urllib.request (standard library) or requests, always with timeout=5 "
         "(these services are sometimes slow or unreachable), and when the call fails show a short message such as "
         "取得できませんでした instead of crashing or waiting forever.")
_SCREEN = ("A display screen (signage, dashboard, clock) must keep itself current without reloading: update the clock every "
           "second with its own setInterval(updateClock, 1000) in JavaScript (new Date()), separate from the data. Let the server call the external API and return a small JSON made for the page, "
           'e.g. GET /api/weather → {"weather": "晴れ", "temperature": 11.6, "humidity": 73, "max": 18.4, "min": 11.4}, and make '
           "the page's JavaScript read exactly those keys; fetch it again every 10 minutes. Put the JavaScript and CSS inside "
           "the HTML file itself (no separate static files), and use a full-screen layout with large text sized to the screen "
           "(for example font-size: 12vw for the clock and 5vw for the weather and the temperature), not small fixed sizes "
           "such as 24px.")

_FLASK = ("With Flask, write the whole server in one app.py: @app.route('/') returns render_template('index.html') "
          "(the page is templates/index.html, in a folder named templates next to app.py; static/ is only for extra CSS or "
          "JavaScript files), and its JSON routes (such as /api/...) are in the same file. Use port 5000 "
          "(app.run(host='127.0.0.1', port=5000)) unless the user names a port: other servers on this PC often use 8080.")
# スマホなど、ほかの機器から開く依頼のときだけ 0.0.0.0(Windows では、0.0.0.0 の名前の逆引きで起動に十数秒かかることがある)
_FLASK_SHARED = _FLASK.replace("app.run(host='127.0.0.1', port=5000)", "app.run(host='0.0.0.0', port=5000), so other devices "
                               "such as a phone can open it at http://<this PC's address>:5000/")
_OTHER_DEVICES = re.compile(r"スマホ|スマートフォン|携帯|タブレット|iPad|(?:ほか|他|別)の(?:機器|端末|PC|パソコン|デバイス)|LAN|"
                            r"ネットワーク|tailscale|\b(?:phones?|tablets?|other devices?|lan|network)\b", re.I)

_HTTP_SERVER = ("With only the Python standard library, a web server that shows your own text or page subclasses "
                "http.server.BaseHTTPRequestHandler and writes the answer in do_GET: self.send_response(200), "
                "self.send_header('Content-Type', 'text/html; charset=utf-8'), self.end_headers(), "
                "self.wfile.write('hello'.encode('utf-8')); then HTTPServer(('127.0.0.1', PORT), Handler).serve_forever(). "
                "SimpleHTTPRequestHandler does not show your text: it only lists and sends the files in the folder.")
_WEB_SERVER = re.compile(r"(?:web|ウェブ|http)\s*サーバ|\bweb\s*server\b|http\.server|\bhttp\s*server\b", re.I)
_FRAMEWORK = re.compile(r"flask|django|fastapi|express|node", re.I)

_TOPICS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"天気|天候|気温|湿度|温湿度|降水|予報|気象|\bweather\b|\bforecast\b|\bhumidity\b", re.I), _WEATHER),
    (re.compile(r"大気|空気の質|PM2\.?5|air quality|\baqi\b", re.I), _AIR),
    (re.compile(r"祝日|休日|祭日|holiday", re.I), _HOLIDAYS),
    (re.compile(r"為替|(?<!テンプ)レート|円相場|ドル円|通貨|exchange rate|currenc", re.I), _RATES),
    (re.compile(r"地震|震度|earthquake", re.I), _QUAKES),
]
_DISPLAY = re.compile(r"サイネージ|ダッシュボード|掲示|時計|signage|dashboard|\bclock\b|kiosk", re.I)


_CODING = re.compile(r"コード|プログラム|スクリプト|アプリ|API|関数|実装|python|javascript|flask|django|html|"
                     r"\b(code|script|program|function|app)\b", re.I)


# Open-Meteo の天気の変数(コードが読む名前のうち、これらだけを URL と照らし合わせる)
_OM_VARIABLES = {
    "temperature_2m", "relative_humidity_2m", "apparent_temperature", "is_day", "precipitation", "rain", "showers",
    "snowfall", "weather_code", "cloud_cover", "pressure_msl", "surface_pressure", "wind_speed_10m", "wind_direction_10m",
    "wind_gusts_10m", "dew_point_2m", "visibility", "uv_index", "precipitation_probability", "temperature_2m_max",
    "temperature_2m_min", "apparent_temperature_max", "apparent_temperature_min", "sunrise", "sunset", "uv_index_max",
    "precipitation_sum", "rain_sum", "snowfall_sum", "precipitation_hours", "precipitation_probability_max",
    "wind_speed_10m_max", "wind_gusts_10m_max", "daylight_duration", "sunshine_duration",
}
_OM_DAILY = re.compile(r"_(?:max|min|sum|hours|duration)$|^(?:sunrise|sunset)$")
# URL の中(?current=a,b&...)と、params の dict の中("current": "a,b" / ["a", "b"] / 変数)
_OM_URL_PARAM = re.compile(r"[?&](current|daily|hourly)=([^&'\"\s]*)")
_OM_DICT_PARAM = re.compile(r"""['"](current|daily|hourly)['"]\s*:\s*(\[[^\]]*\]|"[^"]*"|'[^']*'|[A-Za-z_][\w.]*)""")
_OM_READ = re.compile(r"""\[\s*['"]([a-z][a-z0-9_]*)['"]\s*\]|\.get\(\s*['"]([a-z][a-z0-9_]*)['"]""")


def open_meteo_unrequested(code: str) -> list[tuple[str, str, str, str]]:
    """Open-Meteo の URL で頼んでいない変数を、コードが読んでいるもの。戻り値は [(変数, 足す所, old, new)]。

    current= に weather_code が無いのに current['weather_code'] を読み、KeyError を受けて「不明」にしていた(天気が
    いつも「不明」。qwen2.5-coder:7b で実際にあった)。足す所は current か daily。old と new は、そのまま edit_file に
    使える書き換え(頼む一覧が URL や params に無いときは old が空)。頼む一覧を変数で組み立てているときは見ない。
    """
    if "open-meteo.com" not in code:
        return []
    asked: dict[str, tuple[set[str], str, str]] = {}  # current/daily/hourly → (頼んだ変数, 書いてある所, 足したあと)
    for m in _OM_URL_PARAM.finditer(code):
        name, value = m.groups()
        if not value or "{" in value or "%s" in value:
            return []  # f-string などで組み立てている: 何を頼むかは、ここでは分からない
        names = set(re.findall(r"[a-z][a-z0-9_]*", value.replace("%2C", ",").replace("%2c", ","), re.I))
        asked[name] = (asked.get(name, (set(), "", ""))[0] | names, f"{name}={value}", f"{name}={value},{{}}")
    for m in _OM_DICT_PARAM.finditer(code):
        name, value = m.groups()
        if value[0] not in "[\"'":
            return []  # 変数で渡している
        names = set(re.findall(r"[a-z][a-z0-9_]*", value[1:-1] if value[0] == "[" else value.strip("'\""), re.I))
        names -= {"for", "in"}
        if value[0] == "[":
            quote = '"' if '"' in value else "'"
            grown = value[:-1].rstrip() + (f", {quote}{{}}{quote}]" if names else f"{quote}{{}}{quote}]")
        else:
            grown = value[:-1] + (",{}" if value[1:-1] else "{}") + value[-1]
        asked[name] = (asked.get(name, (set(), "", ""))[0] | names, value, grown)
    if not asked:
        return []
    requested = set().union(*(names for names, _, _ in asked.values()))
    read = {a or b for a, b in _OM_READ.findall(code)} & _OM_VARIABLES
    places: dict[str, list[str]] = {}  # 足す所 → 足す変数(同じ一覧に足すものは、1つの書き換えにまとめる)
    for variable in sorted(read - requested):
        places.setdefault("daily" if _OM_DAILY.search(variable) else "current", []).append(variable)
    result = []
    for place in ("current", "daily"):
        variables = places.get(place)
        if not variables:
            continue
        _, old, grown = asked.get(place, (set(), "", ""))
        if not old:  # その一覧が URL に無い: 足す形だけを示す
            result.append((", ".join(variables), place, "", f"&{place}={','.join(variables)}"))
            continue
        if grown.endswith("]"):
            quote = '"' if '"' in grown else "'"
            joined = f"{quote}, {quote}".join(variables)
        else:
            joined = ",".join(variables)
        result.append((", ".join(variables), place, old, grown.replace("{}", joined)))
    return result


def unrequested_note(code: str) -> str:
    """open_meteo_unrequested の結果を、AIに渡す文にする(無ければ空)。"""
    found = open_meteo_unrequested(code)
    if not found:
        return ""
    parts = []
    for variables, place, old, new in found:
        parts.append(f"{variables}(URL の {place}= に足す。" + (f"old {old!r} → new {new!r}" if old else
                     f"URL の最後に {new!r} を足す") + ")")
    return ("天気の API(Open-Meteo)の URL で頼んでいない項目を、コードが読んでいます: " + " / ".join(parts)
            + "。頼んでいない項目は答えに無いので、KeyError になるか、天気が「不明」などと出ます。URL に足してください"
            "(読まないようにしたり、無いときの値を入れたりしても、天気は出ません)")


def coding_hints(text: str) -> str:
    """プログラムを書く依頼のときだけ hints を返す(「明日は雨?」のような普通の質問には足さない)。"""
    return hints(text) if _CODING.search(text) else ""


_TIME_WORDS = re.compile(r"時間|時刻|時計|日時|\btime\b|\bclock\b", re.I)
# 動く時計: ページで時刻を作るか、1秒ごとに表示し直している(サーバーに毎秒時刻を聞く作りも、時計は動く)
_LIVE_CLOCK = re.compile(r"new\s+Date\s*\(|Date\.now\s*\(|setInterval\s*\([^;]*?,\s*1000\s*\)")
_WEATHER_WORDS = re.compile(r"快晴|晴れ|曇|くもり|雨|雪|霧|['\"](?:clear|sunny|cloudy|overcast|rain|snow|fog)", re.I)
# ページの JavaScript が、受け取った天気を要素に書き込んでいる(el.textContent = data.weather など)
_WRITES_WEATHER = re.compile(r"(?:textContent|innerText|innerHTML)\s*\+?=\s*[^;\n]*weather", re.I)
_SCRIPT_BODY = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)</script\s*>", re.I)


# 大きく表示する頼み。いちばん大きい文字が、これより小さければ足りない(だいたいの px。vw などは幅 1920px の画面として)
_BIG_ASK = re.compile(r"大きく|大きな|大きい|遠くから|離れて(?:も|いても)?(?:見|読)|\b(?:big|large|huge)\b", re.I)
_BIG_ENOUGH = 64.0
# font-size の指定(CSS・style 属性・JavaScript の style.fontSize・font の一括指定・大きさの変数 --clock-size など)
_FONT_SIZE = re.compile(r"font-?size\s*[:=]\s*['\"]?([^;'\"}\n]+)|\bfont\s*:\s*([^;}\n]+)|--[\w-]*(?:size|font)[\w-]*\s*:\s*([^;}\n]+)",
                        re.I)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(px|rem|em|vw|vh|vmin|vmax|pt|%)", re.I)
_SIZE_PX = {"px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "vw": 19.2, "vh": 10.8, "vmin": 10.8, "vmax": 19.2, "%": 0.16}
_BIG_CLASS = re.compile(r"\btext-(?:[6-9]xl|\[\d{3}px\])|\bdisplay-[1-4]\b", re.I)  # Tailwind・Bootstrap の大きな文字


def largest_text(files: dict[str, str]) -> tuple[float, str, str]:
    """ページ(HTML・CSS・JavaScript)で、いちばん大きい文字の (だいたいの px, その指定, ファイル)。

    vw などは、幅 1920px・高さ 1080px の画面として数える。指定が無ければ、ブラウザーの既定の 16px。
    """
    best = (16.0, "", "")
    for path, text in files.items():
        if not re.search(r"\.(?:html?|css|js)$", path, re.I):
            continue
        big = _BIG_CLASS.search(text)
        if big:
            return 96.0, big.group(0), path
        for m in _FONT_SIZE.finditer(text):
            for size in _SIZE.finditer(m.group(1) or m.group(2) or m.group(3) or ""):
                px = float(size.group(1)) * _SIZE_PX[size.group(2).lower()]
                if px > best[0]:
                    best = (px, " ".join(m.group(0).split())[:60], path)
    return best


def _size_checked(task: str) -> bool:
    """文字の大きさを、表示画面の決まった確かめで見る依頼か(大きく表示する表示画面)。"""
    return bool(_DISPLAY.search(task) and _BIG_ASK.search(task))


def display_gaps(task: str, files: dict[str, str]) -> list[str]:
    """表示画面(サイネージ・ダッシュボード・時計)を作る依頼で、作ったファイルに決まって足りないもの(報告の前の確かめ)。

    小さいAIは、時刻をサーバーで作ってページに埋め込むだけ(開いたときの時刻のまま動かない)や、天気を番号
    (weather_code)のまま表示しがち(qwen2.5-coder:7b で実際にあった)。files は {道のり: 中身}。
    """
    if not _DISPLAY.search(task):
        return []
    pages = "\n".join(text for path, text in files.items() if re.search(r"\.(?:html?|js)$", path, re.I))
    gaps = []
    if pages and _TIME_WORDS.search(task) and not _LIVE_CLOCK.search(pages):
        gaps.append("時計が動きません(ページを開いたときの時刻のまま): ページの JavaScript で setInterval(updateClock, 1000) と "
                    "new Date() を使い、毎秒表示し直してください")
    code = "\n".join(files.values())
    if "weather_code" in code:
        # 天気の文字を作っている所で確かめる: ページが番号を読んでいるなら、ページ(サーバーで文字にしても、ページが
        # 自分の文字で上書きしていた。実際にあった)
        in_page = "weather_code" in pages
        scripts = "\n".join(text if path.lower().endswith(".js") else "\n".join(_SCRIPT_BODY.findall(text))
                            for path, text in files.items() if re.search(r"\.(?:html?|js)$", path, re.I))
        python = "\n".join(text for path, text in files.items() if path.lower().endswith((".py", ".pyw")))
        # JavaScript が受け取った天気を書き込むなら、HTML(テンプレートの {{ }})の中の文字は、開いたすぐあとに上書きされる
        writes = not in_page and bool(_WRITES_WEATHER.search(scripts))
        scope = pages if in_page else (scripts + "\n" + python) if writes else code
        where = ("(ページの JavaScript が weather_code を読んで文字にしているので、ページの中で直してください。サーバーで文字に"
                 "しても、ページの表示は変わりません)") if in_page else (
                 "(ページの JavaScript が、サーバーから受け取った天気をそのまま書き込んでいます。HTML の {{ }} で文字にしても、"
                 "JavaScript が番号で上書きします。JavaScript の中か、サーバーが返す JSON を作る Python の中で文字にしてください)"
                 ) if writes else ""
        if not _WEATHER_WORDS.search(scope):
            gaps.append("天気が番号(weather_code)のまま表示されます" + where + ": 0 快晴、1 晴れ、2 晴れ時々曇り、3 曇り、45/48 霧、"
                        "51-57 霧雨、61-67 雨、71-77 雪、80-82 にわか雨、95-99 雷雨 のように、文字にして表示してください")
        elif not re.search(r"雨|\brain", scope, re.I):  # 晴れ・曇りの番号しか文字にしていない
            gaps.append("天気の文字が晴れや曇りの番号にしかなく、雨や雪の日は「不明」などになります" + where + ": 45/48 霧、"
                        "51-57 霧雨、61-67 雨、71-77 雪、80-82 にわか雨、95-99 雷雨 も文字にして表示してください")
    for path, text in files.items():
        if path.lower().endswith((".py", ".pyw")):
            note = unrequested_note(text)
            if note:  # 保存したときにも知らせているが、直さないまま報告しようとした
                gaps.append(f"{path}: {note}")
    page_files = [path for path in files if re.search(r"\.html?$", path, re.I)]
    if page_files and _BIG_ASK.search(task):
        # 大きく表示する頼みなのに、文字が小さい(いちばん大きくても 48px だった。AIの確かめは依頼を言い直しただけで、
        # 何を直せばよいか分からず、同じ起動を繰り返していた。実際にあった)
        biggest, declared, place = largest_text(files)
        if biggest < _BIG_ENOUGH:
            gaps.append(f"文字が小さいままです(いちばん大きい文字でも {declared or '指定が無く、既定の 16px'})。離れても読めるように、"
                        "時計は font-size: 12vw、天気・気温・湿度は font-size: 5vw のように、画面の幅に合わせた大きさにしてください"
                        f"({place or page_files[0]} の font-size を edit_file で直します)")
    return gaps


# 表示画面で決まって出すもの: 依頼やAIの言い方 → ページ(HTML・JS)で、それを出していると分かる書き方
_SHOWN_TERMS = (
    (re.compile(r"天気|天候|weather", re.I), re.compile(r"weather|天気", re.I)),
    (re.compile(r"気温|温度|温湿度|temperature", re.I), re.compile(r"temperature|\btemp\b|気温|温度", re.I)),
    (re.compile(r"湿度|humidity", re.I), re.compile(r"humidity|湿度", re.I)),
    (re.compile(r"時刻|時間|時計|clock|\btime\b", re.I), re.compile(r"new Date\(|toLocaleTimeString|getHours|時刻|時計", re.I)),
    (re.compile(r"日付|曜日|\bdate\b", re.I), re.compile(r"new Date\(|toLocaleDateString|getDate|日付", re.I)),
)
_OTHER_ASK = re.compile(r"[一-鿿ァ-ヿA-Za-z]{2,}")


def already_shown(item: str, files: dict[str, str], task: str = "") -> bool:
    """やり残しとして挙がったものが、表示画面で決まって出すもの(天気・気温・湿度・時刻・日付)だけで、ページがもうそれを
    出しているか(AIの確かめが、もう表示している天気や温湿度を「無い」と挙げていた。実際にあった)。

    ほかの言葉(風速・予報など)が入っているものは、ここでは判断しない(偽を返す)。依頼の言い直し(「今日の天気と気温・湿度を
    大きく表示するサイネージ」)も、ページがもう出しているものなら除く。大きさは、文字が大きいか、表示画面の決まった確かめ
    (文字の大きさ)で見る依頼のときだけ、出していると見る。
    """
    pages = "\n".join(text for path, text in files.items() if re.search(r"\.(?:html?|js)$", path, re.I))
    if not pages:
        return False
    if _BIG_ASK.search(item) and largest_text(files)[0] < _BIG_ENOUGH and not _size_checked(task):
        return False  # 大きく表示する頼みで、文字が小さい(ほかの確かめでも見ない): 残す
    rest = _BIG_ASK.sub(" ", item)
    rest = re.sub(r"今日|本日|サイネージ|ダッシュボード|画面|ページ|\b(?:signage|dashboard|screen|page|today)\b", " ", rest,
                  flags=re.I)
    needed = []
    for asked, shown in _SHOWN_TERMS:
        if asked.search(rest):  # 元の言い方で探す(温湿度 は、気温と湿度の両方)
            needed.append(shown)
            rest = asked.sub(" ", rest)
    rest = re.sub(r"表示|情報|現在|今の|機能|部分|欄|項目|エリア|領域|追加|実装|作成|の|と|や|を|が|する|される|されて|いない|"
                  r"ありません|無い|ない|\b(?:show|display|the|and|current|not|shown|missing|info|information|feature|"
                  r"function|section|area|part|widget|add|added)\b", " ", rest, flags=re.I)
    if not needed or _OTHER_ASK.search(rest):
        return False
    return all(shown.search(pages) for shown in needed)


_KEYLESS_HOSTS = re.compile(r"open-meteo\.com|holidays-jp\.github\.io|open\.er-api\.com|api\.p2pquake\.net", re.I)
_USES_KEY = re.compile(r"api[_-]?key|apikey|app[_-]?id|access[_-]?(?:key|token)|auth[_-]?token|bearer|YOUR_", re.I)
_KEY_WORD = re.compile(r"API\s*キー|api[ _-]?key|アクセスキー|アクセストークン", re.I)
_KEY_NEEDED = re.compile(r"設定|取得|必要|入力|入れ|用意|要ります|要る|ない|ありません|登録|need|requir|missing|not set|obtain|"
                         r"enter|\bset\b|configure|provide|\badd\b|get (?:a|an|your)", re.I)
_KEY_FINE = re.compile(r"不要|要りません|いりません|必要(?:は)?ありません|必要なく|なしで|無しで|使わず|no (?:api )?key|"
                       r"without (?:an |any )?(?:api )?key|not need|no need|isn't needed|is not needed", re.I)
_SENTENCE = re.compile(r".+?(?:[。！？]|[.!?](?=\s|$)|$)\s*")
_LEAD = re.compile(r"^\s*(?:[-*・]|\d+[.)．])?\s*")
_TODO_HEAD = re.compile(r"^\s*(?:#+\s*|[-*・]\s*)?(?:\d+[.)．]\s*)?(?:\*\*)?(?:未完了|やり残し|未実装|残っている(?:こと|作業)|"
                        r"unfinished|still to do)[^\n。.]{0,12}$", re.I)


def drop_false_key_claims(answer: str, files: dict[str, str]) -> str:
    """報告から、「API キーが要る・設定していない」という誤った文を除く。

    キーの要らない Open-Meteo で天気を出せているサイネージなのに、報告が「天気情報のAPIキーを設定していないため、天気情報が
    表示されません」と書いていた(qwen2.5-coder:7b で実際にあった)。書いたコードがキーの要らない API を使い、キーらしい
    名前をどこにも使っていないときだけ除く(キーの要る API を使うコードなら、キーの話は正しいことがある)。
    「キーは要りません」のような正しい文は残す。中身が無くなった「未完了」の見出しも除く。
    """
    code = "\n".join(files.values())
    if not _KEY_WORD.search(answer) or not _KEYLESS_HOSTS.search(code) or _USES_KEY.search(code):
        return answer
    lines: list[str | None] = []  # None = 除いた行
    for line in answer.split("\n"):
        if not _KEY_WORD.search(line):
            lines.append(line)
            continue
        lead = _LEAD.match(line).group(0)
        kept = [s for s in _SENTENCE.findall(line[len(lead):])
                if not (_KEY_WORD.search(s) and _KEY_NEEDED.search(s) and not _KEY_FINE.search(s))]
        rest = "".join(kept).strip()
        lines.append(lead + rest if rest else None)
    out = []
    for i, line in enumerate(lines):
        if line is None:
            continue
        if _TODO_HEAD.match(line):  # 見出しのあとの行(空の行か次の見出しまで)を、どれも除いたなら、見出しも除く
            section = []
            for following in lines[i + 1:]:
                if following is not None and (not following.strip() or _TODO_HEAD.match(following)):
                    break
                section.append(following)
            if section and all(s is None for s in section):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def hints(text: str) -> str:
    """依頼に関係する公開 API の使い方(関係しなければ空文字)。指示文の最後に足して使う。"""
    found = [fact for pattern, fact in _TOPICS if pattern.search(text)]
    if _DISPLAY.search(text):
        found.append(_SCREEN)
    if re.search(r"flask", text, re.I):
        found.append(_FLASK_SHARED if _OTHER_DEVICES.search(text) else _FLASK)
    if _WEB_SERVER.search(text) and not _FRAMEWORK.search(text):  # 標準ライブラリだけの Web サーバー
        found.append(_HTTP_SERVER)
    if not found:
        return ""
    if any(fact not in (_SCREEN, _FLASK, _FLASK_SHARED, _HTTP_SERVER) for fact in found):  # 公開 API の話があるときだけ
        found.append(_CODE)
    return "\n\nUseful facts for this request:\n" + "\n".join(f"- {fact}" for fact in found)
