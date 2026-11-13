"""ネットの道具: Wikipedia の公式 API での調べもの、指定されたページの読み取り、ニュースの見出し。

検索エンジンの画面を機械的に読み取ることはしない(ボット対策の回避にあたるため)。
ニュースは、報道機関やニュースの検索が機械で読むために公開している RSS だけを使う。
利用者を名乗らず、正直なクライアント名で、公開されているページだけを読む。
"""

from __future__ import annotations

import email.utils
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "Celestial/0.1 (local AI assistant)"
MAX_BYTES = 2_000_000


class WebError(Exception):
    pass


def _get(url: str, timeout: float = 15) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_BYTES), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise WebError(f"ページを取得できませんでした(HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WebError(f"接続できませんでした: {e}") from e


def wikipedia(query: str, lang: str = "ja") -> str:
    lang = lang if lang in ("ja", "en") else "ja"
    api = f"https://{lang}.wikipedia.org/w/api.php?"
    body, _ = _get(api + urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 4}))
    hits = json.loads(body).get("query", {}).get("search", [])
    if not hits:
        return f"Wikipedia({lang})に「{query}」に当たる項目は見つかりませんでした"
    title = hits[0]["title"]
    body, _ = _get(api + urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1, "exintro": 1,
                                                  "titles": title, "format": "json", "redirects": 1}))
    page = next(iter(json.loads(body).get("query", {}).get("pages", {}).values()), {})
    extract = (page.get("extract") or "").strip()[:2500]
    others = "、".join(h["title"] for h in hits[1:])
    link = f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    return f"# {page.get('title', title)}\n{extract or '(概要なし)'}\n出典: {link}" + (f"\n関連する項目: {others}" if others else "")


class _Text(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _is_public(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


# 最新のニュースは、報道機関が公開している RSS(機械で読むための見出しの一覧)から取る
NEWS_FEEDS = {"ja": ("NHK", "https://www.nhk.or.jp/rss/news/cat0.xml"), "en": ("BBC", "https://feeds.bbci.co.uk/news/rss.xml")}
# 分野を指定されたら、その分野の RSS を使う(配信元, アドレス, その分野を指す言葉)
NEWS_SECTIONS = {
    "ja": [("NHK 社会", "https://www.nhk.or.jp/rss/news/cat1.xml", "社会 事件 事故"),
           ("NHK 暮らし", "https://www.nhk.or.jp/rss/news/cat2.xml", "暮らし 生活"),
           ("NHK 科学・文化", "https://www.nhk.or.jp/rss/news/cat3.xml", "科学 文化 科学技術 宇宙 学術"),
           ("NHK 政治", "https://www.nhk.or.jp/rss/news/cat4.xml", "政治 選挙 国会 政府"),
           ("NHK 経済", "https://www.nhk.or.jp/rss/news/cat5.xml", "経済 ビジネス 金融 景気 企業 市場"),
           ("NHK 国際", "https://www.nhk.or.jp/rss/news/cat6.xml", "国際 海外 世界 外国"),
           ("NHK スポーツ", "https://www.nhk.or.jp/rss/news/cat7.xml", "スポーツ")],
    "en": [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world international global foreign"),
           ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "business economy economic finance financial markets"),
           ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "technology tech"),
           ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "science environment climate"),
           ("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml", "health medical medicine"),
           ("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml", "politics political"),
           ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "entertainment arts culture"),
           ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml", "sport sports")],
}
# それ以外の話題(「AI」「大谷翔平」など)は、ニュースの検索の RSS で探す
NEWS_SEARCH = "https://news.google.com/rss/search?"
_SEARCH_NAMES = {"ja": ("Google ニュース", {"hl": "ja", "gl": "JP", "ceid": "JP:ja"}),
                 "en": ("Google News", {"hl": "en-US", "gl": "US", "ceid": "US:en"})}


def _search_url(lang: str, query: str) -> str:
    return NEWS_SEARCH + urllib.parse.urlencode({"q": query, **_SEARCH_NAMES[lang][1]})


def news_feed(lang: str = "ja", topic: str = "") -> tuple[str, str]:
    """ニュースを取る RSS: (配信元, アドレス)。話題がなければ主なニュース、分野ならその分野、ほかは話題で直近1週間を検索する。"""
    lang = lang if lang in NEWS_FEEDS else "ja"
    topic = topic.strip()
    if not topic:
        return NEWS_FEEDS[lang]
    for source, url, words in NEWS_SECTIONS[lang]:
        if topic.lower() in words.split():
            return source, url
    return _SEARCH_NAMES[lang][0], _search_url(lang, f"{topic} when:7d")


def news_time(item: dict) -> float:
    """見出しの配信日時(UNIX 時刻。分からなければ 0)。"""
    try:
        return email.utils.parsedate_to_datetime(item.get("date", "")).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0.0


def _feed_items(url: str) -> list[dict]:
    from xml.etree import ElementTree as ET

    body, _ = _get(url, timeout=10)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise WebError(f"ニュースを読めませんでした: {e}") from e
    items, seen = [], set()
    for item in root.iter("item"):
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        outlet = re.sub(r"\s+", " ", item.findtext("source") or "").strip()
        if outlet and title.endswith(" - " + outlet):  # 検索の見出しは「見出し - 媒体」の形
            title = title[: -len(outlet) - 3].strip()
        if not title or title in seen:
            continue
        seen.add(title)
        summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))).strip()
        if summary.startswith(title):  # 見出しの繰り返しだけなら省く
            summary = ""
        items.append({"title": title, "url": (item.findtext("link") or "").strip(), "summary": summary[:300],
                      "date": (item.findtext("pubDate") or "").strip(), "outlet": outlet})
    return items


# 検索の結果に混ざる迷惑な記事: 見えない文字や、埋め込み前の型({SxC-00} など)が見出しに残っているもの。
# 広告(PR)と、ニュースではない株価・基準価格のページ(「GX 半導体【2243】：株価・株式情報」)も除く
_HIDDEN = re.compile("[​-‏⁠-⁤﻿]")
_JUNK = re.compile(r"[{}]|[（(]PR[)）]\s*$|【[0-9A-Z]{3,10}】：")
PER_OUTLET = 3


def _pick(items: list[dict], limit: int) -> list[dict]:
    """検索の結果から、検索の並び(関連の強い順)のまま選ぶ。迷惑な記事と広告を除き、1つの媒体は3件まで
    (「AI」の検索では、1つの媒体の迷惑な記事が99件中75件を占め、新しい順に並べると上位を埋めた)。選んだものを新しい順に並べる。"""
    picked: list[dict] = []
    per: dict[str, int] = {}
    for item in items:
        if _HIDDEN.search(item["title"]) or _JUNK.search(item["title"]) or per.get(item["outlet"], 0) >= PER_OUTLET:
            continue
        per[item["outlet"]] = per.get(item["outlet"], 0) + 1
        picked.append(item)
        if len(picked) >= limit:
            break
    return sorted(picked, key=news_time, reverse=True)


def news(lang: str = "ja", limit: int = 10, topic: str = "") -> tuple[str, list[dict]]:
    """最新のニュースの見出し: (配信元, [{title, url, summary, date, outlet}])。
    話題(「AI」など)を渡すと、その分野の RSS か、ニュースの検索(直近1週間)から取る。"""
    lang = lang if lang in NEWS_FEEDS else "ja"
    source, url = news_feed(lang, topic)
    items = _feed_items(url)
    if not url.startswith(NEWS_SEARCH):
        return source, items[:limit]
    if not items:  # 直近1週間になければ、期間を限らずに探す
        items = _feed_items(_search_url(lang, topic.strip()))
    return source, _pick(items, limit)


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    """転送先が内側の機器(ルーターやこの PC 自身)なら、たどらない。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname
        if not host or not _is_public(host):
            raise WebError("内側の機器への転送はたどりません")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def post(url: str, body: str, timeout: float = 15) -> str:
    """公開されているアドレス(Slack や Discord の Webhook など)に送る。JSON として読める本文は JSON で送る。"""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("http:// または https:// で始まるアドレスを指定してください")
    if not _is_public(parsed.hostname):
        raise WebError("公開されているアドレスにだけ送れます")
    try:
        json.loads(body)
        kind = "application/json; charset=utf-8"
    except ValueError:
        kind = "text/plain; charset=utf-8"
    request = urllib.request.Request(parsed.geturl(), data=body.encode("utf-8"), method="POST",
                                     headers={"User-Agent": USER_AGENT, "Content-Type": kind})
    opener = urllib.request.build_opener(_PublicRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read(MAX_BYTES).decode("utf-8", "replace")
            return f"HTTP {response.status}\n{text[:4000]}".strip()
    except urllib.error.HTTPError as e:
        raise WebError(f"送れませんでした(HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WebError(f"接続できませんでした: {e}") from e


def fetch_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("http:// または https:// で始まるアドレスを指定してください")
    if not _is_public(parsed.hostname):  # 自宅のルーターやこのPC自身など、内側の機器には触れない
        raise WebError("公開されているウェブページだけを読めます")
    body, content_type = _get(parsed.geturl())
    if "html" not in content_type and not content_type.startswith("text/") and "json" not in content_type:
        raise WebError(f"文章として読めない種類のページです({content_type or '種類不明'})")
    charset = re.search(r"charset=([\w-]+)", content_type)
    try:
        text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    if "html" not in content_type:
        return text[:6000]
    parser = _Text()
    parser.feed(text)
    lines = [re.sub(r"[ \t　]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    content = "\n".join(line for line in lines if line)
    return (f"# {parser.title.strip()}\n" if parser.title.strip() else "") + (content[:6000] or "(本文を取り出せませんでした)")
