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


def request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
            body: str = "", timeout: float = 15) -> str:
    """公開されている URL へ HTTP リクエスト(GET/POST/PUT/DELETE/PATCH)を送る(SSRF 対策済み)。"""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("http:// または https:// で始まるアドレスを指定してください")
    if not _is_public(parsed.hostname):
        raise WebError("公開されているアドレスにだけ接続できます")
    m = (method or "GET").strip().upper()
    if m not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
        raise WebError(f"未対応の HTTP メソッドです: {method}")
    req_headers = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
    if headers:
        for k, v in headers.items():
            req_headers[str(k)] = str(v)
    data = None
    if body or m in ("POST", "PUT", "PATCH"):
        data = body.encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            try:
                json.loads(body)
                req_headers["Content-Type"] = "application/json; charset=utf-8"
            except ValueError:
                req_headers["Content-Type"] = "text/plain; charset=utf-8"
    req = urllib.request.Request(parsed.geturl(), data=data, method=m, headers=req_headers)
    opener = urllib.request.build_opener(_PublicRedirects)
    try:
        with opener.open(req, timeout=timeout) as response:
            text = response.read(MAX_BYTES).decode("utf-8", "replace")
            return f"HTTP {response.status}\n{text[:8000]}".strip()
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read(1500).decode("utf-8", "replace")
        except OSError:
            pass
        raise WebError(f"取得・送信できませんでした(HTTP {e.code})" + (f"\n{err_body}" if err_body else "")) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WebError(f"接続できませんでした: {e}") from e


def post(url: str, body: str, timeout: float = 15) -> str:
    """公開されているアドレス(Slack や Discord の Webhook など)に送る。JSON として読める本文は JSON で送る。"""
    return request(url, method="POST", body=body, timeout=timeout)


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


class _ExtractParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.tables: list[list[list[str]]] = []
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self._link_href: str | None = None
        self._link_buf: list[str] = []
        self._in_table = False
        self._cur_table: list[list[str]] = []
        self._cur_row: list[str] | None = None
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        amap = dict(attrs)
        if t in ("script", "style", "noscript"):
            self._skip += 1
        elif t == "title":
            self._in_title = True
        elif t == "meta" and (amap.get("name", "").lower() == "description" or amap.get("property", "").lower() == "og:description"):
            if amap.get("content") and not self.description:
                self.description = html.unescape(amap["content"]).strip()
        elif t in ("h1", "h2", "h3"):
            self._heading_tag = t
            self._heading_buf = []
        elif t == "a" and amap.get("href"):
            href = amap["href"].strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._link_href = urllib.parse.urljoin(self.base_url, href)
                self._link_buf = []
        elif t == "table":
            self._in_table = True
            self._cur_table = []
        elif self._in_table and t == "tr":
            self._cur_row = []
        elif self._in_table and self._cur_row is not None and t in ("th", "td"):
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif t == "title":
            self._in_title = False
        elif t == self._heading_tag:
            txt = re.sub(r"\s+", " ", "".join(self._heading_buf)).strip()
            if txt:
                self.headings.append(f"{t.upper()}: {txt}")
            self._heading_tag = None
        elif t == "a" and self._link_href is not None:
            txt = re.sub(r"\s+", " ", "".join(self._link_buf)).strip() or self._link_href
            self.links.append((txt[:80], self._link_href))
            self._link_href = None
        elif t in ("th", "td") and self._in_cell:
            if self._cur_row is not None:
                self._cur_row.append(re.sub(r"\s+", " ", "".join(self._cell_buf)).strip())
            self._in_cell = False
        elif t == "tr" and self._cur_row is not None:
            if any(c for c in self._cur_row):
                self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif t == "table" and self._in_table:
            if self._cur_table:
                self.tables.append(self._cur_table)
            self._in_table = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._heading_tag:
            self._heading_buf.append(data)
        if self._link_href is not None:
            self._link_buf.append(data)
        if self._in_cell:
            self._cell_buf.append(data)


def extract_page(url: str, *, mode: str = "links", limit: int = 40) -> str:
    """公開ウェブページからリンク一覧 (links)・HTML 表 (tables)・見出しと概要 (meta) を取り出す。"""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("http:// または https:// で始まるアドレスを指定してください")
    if not _is_public(parsed.hostname):
        raise WebError("公開されているウェブページだけを読めます")
    body, content_type = _get(parsed.geturl())
    if "html" not in content_type and not content_type.startswith("text/"):
        raise WebError(f"HTML ページではありません({content_type or '種類不明'})")
    charset = re.search(r"charset=([\w-]+)", content_type)
    try:
        text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    parser = _ExtractParser(parsed.geturl())
    parser.feed(text)
    kind = (mode or "links").lower().strip()
    lim = max(1, min(100, int(limit)))
    if kind == "tables":
        if not parser.tables:
            return "(ページ内に HTML テーブルはありませんでした)"
        blocks = []
        for idx, tbl in enumerate(parser.tables[:8], 1):
            w = max(len(r) for r in tbl)
            rows = [r + [""] * (w - len(r)) for r in tbl[:35]]
            head = "| " + " | ".join(rows[0]) + " |"
            sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
            b = "\n".join("| " + " | ".join(r) + " |" for r in rows[1:])
            blocks.append(f"### 表 {idx}\n{head}\n{sep}" + ("\n" + b if b else ""))
        return "\n\n".join(blocks)[:6000]
    if kind == "meta":
        lines = []
        if parser.title.strip():
            lines.append(f"タイトル: {parser.title.strip()}")
        if parser.description:
            lines.append(f"概要: {parser.description}")
        if parser.headings:
            lines.append("見出し:\n" + "\n".join("- " + h for h in parser.headings[:lim]))
        return "\n".join(lines)[:6000] or "(メタ情報はありませんでした)"
    # links
    if not parser.links:
        return "(リンクは見つかりませんでした)"
    dedup, seen = [], set()
    for label, href in parser.links:
        if href not in seen:
            seen.add(href)
            dedup.append(f"- [{label}]({href})")
            if len(dedup) >= lim:
                break
    return "\n".join(dedup)[:6000]


def search(query: str, *, lang: str = "ja", limit: int = 8) -> tuple[str, list[dict]]:
    """公式 API(DuckDuckGo Instant Answer / Wikipedia Search API) と公式 RSS を使って調べものをする。"""
    q = (query or "").strip()
    if not q:
        raise WebError("検索語が空です")
    lang = "en" if lang == "en" else "ja"
    lim = max(1, min(15, int(limit)))
    hits: list[dict] = []
    # 1) DuckDuckGo Instant Answer 公式 JSON API
    try:
        raw, _ = _get(f"https://api.duckduckgo.com/?q={urllib.parse.quote(q)}&format=json&no_html=1&skip_disambig=1", timeout=8)
        ddg = json.loads(raw.decode("utf-8", "replace"))
        if ddg.get("AbstractText") and ddg.get("AbstractURL"):
            hits.append({"source": "ddg", "title": ddg.get("Heading") or q, "url": ddg["AbstractURL"],
                         "snippet": ddg["AbstractText"][:240]})
        for rel in (ddg.get("RelatedTopics") or [])[:4]:
            if isinstance(rel, dict) and rel.get("Text") and rel.get("FirstURL"):
                hits.append({"source": "ddg", "title": rel["Text"].split(" - ")[0][:80], "url": rel["FirstURL"],
                             "snippet": rel["Text"][:200]})
    except (WebError, ValueError):
        pass
    # 2) Wikipedia 公開 Search API
    try:
        api = f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(q)}&format=json&utf8=1&srlimit={min(lim, 6)}"
        raw, _ = _get(api, timeout=8)
        for item in (json.loads(raw.decode("utf-8", "replace")).get("query", {}).get("search") or []):
            title = str(item.get("title") or "")
            if not title:
                continue
            snippet = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("snippet") or ""))).strip()
            url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            hits.append({"source": "wikipedia", "title": title, "url": url, "snippet": snippet[:220]})
    except (WebError, ValueError):
        pass
    # 3) 最新ニュース RSS で補う
    if len(hits) < lim:
        try:
            _, items = news(lang=lang, limit=max(2, lim - len(hits)), topic=q)
            for it in items:
                hits.append({"source": "news", "title": it.get("title", ""), "url": it.get("link", ""),
                             "snippet": it.get("source", "")})
        except WebError:
            pass
    if not hits:
        return "該当する公開情報が見つかりませんでした", []
    lines = []
    for i, it in enumerate(hits[:lim], 1):
        lines.append(f"{i}. [{it['source']}] {it['title']}\n   {it['url']}" + (f"\n   {it['snippet']}" if it.get("snippet") else ""))
    return "\n".join(lines)[:6000], hits[:lim]


def download_file(url: str, dest, *, max_bytes: int = 20_000_000, timeout: float = 30) -> tuple[int, str]:
    """公開 URL のファイルをダウンロードして dest に保存する(リダイレクト先も SSRF 検証)。"""
    from pathlib import Path

    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("http:// または https:// で始まるアドレスを指定してください")
    if not _is_public(parsed.hostname):
        raise WebError("公開されている URL だけをダウンロードできます")

    class _SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            np = urllib.parse.urlparse(newurl)
            if np.scheme not in ("http", "https") or not np.hostname or not _is_public(np.hostname):
                raise WebError("安全でないリダイレクト先のため中断しました")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(parsed.geturl(), headers={"User-Agent": USER_AGENT})
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            with open(dest_p, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise WebError(f"ファイルサイズが上限({max_bytes // (1024 * 1024)} MB)を超えています")
                    f.write(chunk)
    except WebError:
        dest_p.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as e:
        raise WebError(f"ダウンロードできませんでした(HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WebError(f"接続できませんでした: {e}") from e
    return written, ctype
