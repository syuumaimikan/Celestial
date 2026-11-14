"""Word(.docx)・Excel(.xlsx)・PowerPoint(.pptx)のファイルを、追加の部品なし(標準ライブラリだけ)で作る。

どれも中身は ZIP に入った XML(Office Open XML)なので、必要な最小限の部品を書き出す。
AIエージェントの道具(make_word / make_excel / make_slides)から使う。
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NAVY, BLUE, GOLD, PALE = "17203F", "2F56D9", "C9A227", "E8ECF8"
_FONT_LATIN, _FONT_EA = "Segoe UI", "Yu Gothic"


def _x(text: str) -> str:
    """XML の文字列(制御文字は XML に入れられないので除く)。"""
    text = "".join(c for c in str(text) if c in "\t\n\r" or ord(c) >= 0x20)
    return escape(text, {'"': "&quot;"})


def _zip(path: Path, parts: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))  # 先頭に置く決まり
        for name, data in parts.items():
            z.writestr(name, data)
    return path


def _core(title: str) -> str:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{_x(title)}</dc:title><dc:creator>Celestial</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')


_APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>Celestial</Application></Properties>')


def _root_rels(main: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{main}"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')


def _types(overrides: dict[str, str]) -> str:
    items = "".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in overrides.items())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            f'{items}</Types>')


def _inline(text: str) -> list[tuple[str, set[str]]]:
    """**太字** *斜体* `コード` を (文字, 書式) の並びにする。"""
    out: list[tuple[str, set[str]]] = []
    pattern = re.compile(r"\*\*(.+?)\*\*|(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])|`([^`]+)`")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], set()))
        if m.group(1) is not None:
            out.append((m.group(1), {"b"}))
        elif m.group(2) is not None:
            out.append((m.group(2), {"i"}))
        else:
            out.append((m.group(3), {"code"}))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], set()))
    return out


def read_pdf_text(path: str | Path, limit: int = 20000) -> str:
    """PDF からテキストを取り出す(標準ライブラリのみ: FlateDecode・ToUnicode CMap・文字列オペランドに対応)。"""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise ValueError(f"PDF を開けません: {e}") from e
    if not raw.lstrip().startswith(b"%PDF"):
        raise ValueError(f"PDF ファイルではありません: {p.name}")

    # pypdf 等が入っていれば高精度に読む。未導入でも下の標準ライブラリ解析で読む
    try:
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(str(p))
        pages = []
        for idx, page in enumerate(reader.pages, 1):
            txt = (page.extract_text() or "").strip()
            if txt:
                pages.append(f"[ページ {idx}]\n{txt}")
        if pages:
            return "\n\n".join(pages)[:limit]
    except Exception:
        pass

    streams: list[bytes] = []
    for m in re.finditer(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\n?endstream", raw, re.S):
        header, body = m.group(1), m.group(2)
        if b"/FlateDecode" in header:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompress(body.rstrip(b"\r\n"))
                except zlib.error:
                    continue
        streams.append(body)

    cmap: dict[bytes, str] = {}
    for st in streams:
        if b"beginbfchar" in st or b"beginbfrange" in st:
            txt = st.decode("latin1", errors="ignore")
            for m in re.finditer(r"beginbfchar\s*(.*?)\s*endbfchar", txt, re.S):
                for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", m.group(1)):
                    try:
                        cmap[bytes.fromhex(a)] = bytes.fromhex(b).decode("utf-16-be", errors="ignore")
                    except ValueError:
                        pass
            for m in re.finditer(r"beginbfrange\s*(.*?)\s*endbfrange", txt, re.S):
                for s, e, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", m.group(1)):
                    try:
                        sv, ev, dv = int(s, 16), int(e, 16), int(dst, 16)
                        blen = len(s) // 2
                        for code in range(sv, min(ev + 1, sv + 512)):
                            cmap[code.to_bytes(blen, "big")] = chr(dv + (code - sv))
                    except ValueError:
                        pass

    def _unescape_lit(data: bytes) -> str:
        out = bytearray()
        i = 0
        esc_map = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        while i < len(data):
            ch = data[i]
            if ch == 92 and i + 1 < len(data):  # '\\'
                nxt = data[i + 1]
                if nxt in esc_map:
                    out.append(esc_map[nxt])
                    i += 2
                    continue
                if 48 <= nxt <= 55:  # octal \ddd
                    j = i + 1
                    while j < min(i + 4, len(data)) and 48 <= data[j] <= 55:
                        j += 1
                    out.append(int(data[i + 1 : j], 8) & 0xFF)
                    i = j
                    continue
                if nxt in (10, 13):
                    i += 2
                    continue
                out.append(nxt)
                i += 2
            else:
                out.append(ch)
                i += 1
        if out.startswith(b"\xfe\xff"):
            return out[2:].decode("utf-16-be", errors="replace")
        return bytes(out).decode("utf-8", errors="ignore") if not cmap else bytes(out).decode("latin1", errors="ignore")

    def _decode_hex(hex_bytes: bytes) -> str:
        clean = re.sub(rb"\s+", b"", hex_bytes)
        if len(clean) % 2:
            clean += b"0"
        try:
            raw_b = bytes.fromhex(clean.decode("ascii"))
        except ValueError:
            return ""
        if raw_b.startswith(b"\xfe\xff"):
            return raw_b[2:].decode("utf-16-be", errors="replace")
        if cmap:
            step = 2 if len(raw_b) % 2 == 0 and any(len(k) == 2 for k in cmap) else 1
            pieces = [cmap.get(raw_b[i : i + step], raw_b[i : i + step].decode("latin1", errors="ignore"))
                      for i in range(0, len(raw_b), step)]
            return "".join(pieces)
        try:
            return raw_b.decode("utf-8")
        except UnicodeDecodeError:
            return raw_b.decode("latin1", errors="ignore")

    text_chunks: list[str] = []
    token_re = re.compile(rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]+)>|\b(Tj|TJ|T\*|Td|TD|Tm|ET)\b")
    for st in streams:
        if b"BT" not in st and b"Tj" not in st and b"TJ" not in st:
            continue
        buf: list[str] = []
        line_parts: list[str] = []
        for tok in token_re.finditer(st):
            if tok.group(1) is not None:
                buf.append(_unescape_lit(tok.group(1)))
            elif tok.group(2) is not None:
                buf.append(_decode_hex(tok.group(2)))
            else:
                op = tok.group(3)
                if op in (b"Tj", b"TJ"):
                    if buf:
                        line_parts.append("".join(buf))
                        buf.clear()
                elif op in (b"T*", b"Td", b"TD", b"Tm", b"ET"):
                    if line_parts:
                        text_chunks.append(" ".join(p for p in line_parts if p).strip())
                        line_parts.clear()
                    buf.clear()
        if line_parts:
            text_chunks.append(" ".join(p for p in line_parts if p).strip())
    result = "\n".join(ln for ln in text_chunks if ln and re.search(r"[A-Za-z0-9ぁ-んァ-ヶ一-龠]", ln))
    return result[:limit] if result else "(PDF から文字を取り出せませんでした。スキャン画像のみの PDF の可能性があります)"


def read_text(path: str | Path, limit: int = 20000) -> str:
    """Word・Excel・PowerPoint・PDF のファイルの中身を文字にする(AIエージェントが読むため)。Excel は CSV にする。"""
    path = Path(path)
    kind = path.suffix.lower()
    if kind == ".pdf":
        return read_pdf_text(path, limit=limit)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"Office のファイルとして開けません: {e}") from e
    with z:
        names = set(z.namelist())
        if kind == ".docx":
            root = ET.fromstring(z.read("word/document.xml"))
            lines = []
            for p in root.iter(f"{_WNS}p"):
                text = "".join(t.text or "" for t in p.iter(f"{_WNS}t"))
                if text.strip():
                    lines.append(text)
            return "\n".join(lines)[:limit]
        if kind == ".xlsx":
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_SNS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_SNS}t")))
            out = []
            shown = _cell_formats(ET.fromstring(z.read("xl/styles.xml"))) if "xl/styles.xml" in names else {}
            sheets = [s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{_SNS}sheet")]
            for i, name in enumerate(sheets, 1):
                part = f"xl/worksheets/sheet{i}.xml"
                if part not in names:
                    continue
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                for row in ET.fromstring(z.read(part)).iter(f"{_SNS}row"):
                    values: dict[int, str] = {}
                    for c in row.iter(f"{_SNS}c"):
                        col = _column_index(re.sub(r"\d", "", c.get("r", "A")))
                        formula, value = c.find(f"{_SNS}f"), c.find(f"{_SNS}v")
                        if formula is not None and formula.text:
                            values[col] = "=" + formula.text
                        elif c.get("t") == "inlineStr":
                            values[col] = "".join(t.text or "" for t in c.iter(f"{_SNS}t"))
                        elif c.get("t") == "s" and value is not None:
                            values[col] = shared[int(value.text)]
                        elif value is not None:
                            values[col] = _shown(value.text or "", shown.get(int(c.get("s", "0"))) if c.get("t") in (None, "n") else None)
                    if values:
                        writer.writerow([values.get(n, "") for n in range(1, max(values) + 1)])
                out.append(f"[シート: {name}]\n{buffer.getvalue().strip()}")
            return "\n\n".join(out)[:limit]
        if kind == ".pptx":
            slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.search(r"(\d+)", n).group(1)))
            out = []
            for n, part in enumerate(slides, 1):
                root = ET.fromstring(z.read(part))
                paras = ["".join(t.text or "" for t in p.iter(f"{_ANS}t")) for p in root.iter(f"{_ANS}p")]
                out.append(f"[スライド {n}]\n" + "\n".join(p for p in paras if p.strip()))
            return "\n\n".join(out)[:limit]
    raise ValueError(f"読めない種類のファイルです: {path.name}")


def _cell_formats(styles: ET.Element) -> dict[int, tuple]:
    """styles.xml から、セルの書式の番号 → 見せ方(パーセント、または単位つきの数)。それ以外の書式は含めない。"""
    codes = {9: "0%", 10: "0.00%"}
    codes.update({int(f.get("numFmtId", "0")): f.get("formatCode", "") for f in styles.iter(f"{_SNS}numFmt")})
    xfs = styles.find(f"{_SNS}cellXfs")
    out: dict[int, tuple] = {}
    for i, xf in enumerate(xfs.findall(f"{_SNS}xf") if xfs is not None else []):
        code = codes.get(int(xf.get("numFmtId", "0")), "")
        percent = re.fullmatch(r"0(?:\.(0+))?%", code)
        unit = re.fullmatch(r'(?:"([^"]*)")?(#,##0|0)(?:\.(0+))?(?:"([^"]*)")?', code)
        if percent:
            out[i] = ("%", len(percent.group(1) or ""))
        elif unit and (unit.group(1) or unit.group(4)):
            out[i] = (unit.group(1) or "", unit.group(2) == "#,##0", len(unit.group(3) or ""), unit.group(4) or "")
    return out


def _shown(value: str, fmt: tuple | None) -> str:
    """数のセルを、Excel での見え方(105%、1,200円 など)の文字にする。単位を失わずに読み書きできるように。"""
    if not fmt:
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    if fmt[0] == "%":
        return f"{number * 100:.{fmt[1]}f}%"
    pre, grouped, decimals, suf = fmt
    digits = f"{abs(number):,.{decimals}f}" if grouped else f"{abs(number):.{decimals}f}"
    return ("-" if number < 0 else "") + pre + digits + suf


def sheets_from_text(text: str) -> dict[str, str]:
    """read_text で読んだ Excel の中身({シート名: CSV})に戻す(「[シート: 名前]」の行で区切る)。"""
    parts = re.split(r"^\[シート: (.+?)\]\s*$", text.strip(), flags=re.M)
    if len(parts) == 1:
        return {"Sheet1": text}
    sheets = {}
    for i in range(1, len(parts) - 1, 2):
        sheets[parts[i].strip()] = parts[i + 1].strip("\n")
    return sheets


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n or 1


OFFICE_SUFFIXES = (".docx", ".xlsx", ".pptx")
READABLE_DOC_SUFFIXES = (".docx", ".xlsx", ".pptx", ".pdf")
_WNS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_ANS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

KINDS = {"docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"),
         "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel"),
         "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "PowerPoint")}


def first_table(markdown: str) -> list[list[str]] | None:
    """文章の中の最初の表(Markdown の表、または CSV のコードブロック)。なければ None。"""
    lines = unfence_markdown(markdown).split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            block = []
            for row in lines[i:]:
                if not row.strip().startswith("|"):
                    break
                block.append(row)
            rows = _table_rows(block)
            if len(rows) >= 2:
                return rows
    for fenced in re.finditer(r"```(csv|tsv)?[ \t]*\n([\s\S]*?)```", markdown, re.I):
        body = fenced.group(2)
        if "," not in body and "\t" not in body:
            continue
        rows = _rows(body)
        widths = {len(r) for r in rows}
        # 言語の書いていない囲いは、どの行も同じ列の数のときだけ表とみなす(カンマを含むプログラムを表と間違えない)
        if len(rows) >= 2 and len(rows[0]) >= 2 and (fenced.group(1) or len(widths) == 1):
            return rows
    return None


_DELIM_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def pipe_tables(text: str) -> str:
    """両端の | を省いた Markdown の表(「月 | 売上」の次の行が「--- | ---」。GFM では正しい書き方)を、
    両端に | のある形にそろえる。コードブロックの中は変えない。"""
    lines = text.split("\n")
    out: list[str] = []
    i, fenced = 0, False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            fenced = not fenced
        elif (not fenced and "|" in line and i + 1 < len(lines) and "|" in lines[i + 1]
              and _DELIM_ROW.match(lines[i + 1]) and len(_cells(line)) == len(_cells(lines[i + 1]))):
            block = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            out.extend("| " + " | ".join(_cells(row)) + " |" for row in block)
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def unfence_markdown(text: str) -> str:
    """```markdown で囲まれた部分は、囲いを外して普通の Markdown として扱う(AI は表などをよくこう囲む)。
    両端の | を省いた表も、両端に | のある形にそろえる。"""
    text = re.sub(r"```(?:markdown|md)\s*\n([\s\S]*?)\n?```", lambda m: m.group(1), text.replace("\r\n", "\n"))
    return pipe_tables(text)


def file_name(title: str, limit: int = 40) -> str:
    """会話の題名から、保存するファイルの名前(拡張子なし)を作る。
    ファイル名に使えない文字を除き、途中で切られた題名(末尾が「…」)は読点などの切れ目まで戻す。"""
    name = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)).strip()
    cut = name.endswith(("…", "...")) or len(name) > limit
    name = name.rstrip("…. ")[:limit]
    if cut:
        at = max(name.rfind(mark) for mark in "、。，,;:!?！？")
        if at < limit // 3:
            at = name.rfind(" ")
        if at >= limit // 3:
            name = name[:at]
    return name.strip(" 、。,.") or "回答"


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def answer_to_slides(markdown: str, title: str) -> str:
    """見出しのない回答も、スライドにできる形(# 見出しごとに1枚)に直す。表は表だけの1枚にする。"""
    text = unfence_markdown(markdown).strip()
    cover = _short(title, 40)
    if re.search(r"^#{1,3}\s+\S", text, re.M):
        return f"# {cover}\n\n{text}" if not re.match(r"^#\s", text) else text
    short = _short(title, 20)
    slides, points, count = [f"# {cover}"], [], 0

    def flush() -> None:
        nonlocal count
        for n in range(0, len(points), 6):  # 1枚に6行まで
            count += 1
            slides.append(f"# {short}({count})\n" + "\n".join(f"- {p}" for p in points[n:n + 6]))
        points.clear()

    lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            flush()
            count += 1
            slides.append(f"# {short}({count})\n" + "\n".join(block))
            continue
        line = re.sub(r"^\s*(?:[-*+]|・|\d+[.)])\s+", "", lines[i]).strip()
        if line:
            points.append(line)
        i += 1
    flush()
    return "\n\n".join(slides)


def export_answer(kind: str, markdown: str, title: str, path: str | Path) -> Path:
    """会話の回答を Word・Excel・PowerPoint のファイルにする。Excel は回答の中の表を使う(なければ ValueError)。"""
    if kind == "docx":
        return write_docx(path, markdown, title)
    if kind == "xlsx":
        rows = first_table(markdown)
        if rows is None:
            raise ValueError("この回答には表がありません(Excel にできるのは表を含む回答です)")
        return write_xlsx(path, [[c for c in r] for r in rows])
    if kind == "pptx":
        return write_pptx(path, answer_to_slides(markdown, title))
    raise ValueError(f"未知の形式: {kind}")


# ---- Word ---------------------------------------------------------------------

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _w_runs(text: str, bold: bool = False) -> str:
    runs = []
    for part, fmt in _inline(text):
        props = ""
        if bold or "b" in fmt:
            props += "<w:b/>"
        if "i" in fmt:
            props += "<w:i/>"
        if "code" in fmt:
            props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="MS Gothic"/><w:shd w:val="clear" w:color="auto" w:fill="F0F2F8"/>'
        runs.append(f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{_x(part)}</w:t></w:r>')
    return "".join(runs)


def _w_par(text: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num:
        props += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    return f'<w:p>{f"<w:pPr>{props}</w:pPr>" if props else ""}{_w_runs(text)}</w:p>'


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for n, line in enumerate(lines):
        cells = _cells(line)
        # 区切りの行(|---|---|)。2行目なら「|-|-|」のような短い形も区切りとみなす
        if (n == 1 and _DELIM_ROW.match(line)) or all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _w_table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    widths = [min(4200, max(900, 160 * max((_width(r[i]) if i < len(r) else 0) for r in rows) + 300)) for i in range(cols)]
    total = sum(widths)
    if total > 9000:  # ページの幅に収める
        widths = [int(w * 9000 / total) for w in widths]
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    body = []
    for n, row in enumerate(rows):
        cells = []
        for i in range(cols):
            text = row[i] if i < len(row) else ""
            shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{PALE}"/>' if n == 0 else ""
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[i]}" w:type="dxa"/>{shade}</w:tcPr>'
                         f'<w:p><w:pPr><w:spacing w:before="40" w:after="40"/></w:pPr>{_w_runs(text, bold=n == 0)}</w:p></w:tc>')
        header = '<w:trPr><w:tblHeader/></w:trPr>' if n == 0 else ""
        body.append(f"<w:tr>{header}{''.join(cells)}</w:tr>")
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
            f'<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
            f'</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(body)}</w:tbl>')


def _docx_body(markdown: str, title: str | None, *, num_offset: int = 0, heading_title: bool = True) -> tuple[str, int]:
    """Markdown を本文の XML にする。戻り値は (本文, 番号つきの箇条書きの数)。"""
    out: list[str] = []
    if title:
        out.append(_w_par(title, "Title"))
    lines = markdown.replace("\r\n", "\n").split("\n")
    i, numbered_lists, current_num = 0, 0, None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                out.append(f'<w:p><w:pPr><w:pStyle w:val="Code"/></w:pPr><w:r><w:t xml:space="preserve">{_x(lines[i])}</w:t></w:r></w:p>')
                i += 1
            i += 1
            current_num = None
            continue
        if stripped.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = _table_rows(block)
            if rows:
                out.append(_w_table(rows))
                out.append("<w:p/>")
            current_num = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^(\s*)(?:[-*+]|・)\s+(.*)$", line)
        number = re.match(r"^(\s*)\d+[.)]\s+(.*)$", line)
        if heading:
            level = min(3, len(heading.group(1)))
            if heading_title and not out and not title and level == 1:
                out.append(_w_par(heading.group(2), "Title"))
            else:
                out.append(_w_par(heading.group(2), f"Heading{level}"))
            current_num = None
        elif bullet:
            out.append(_w_par(bullet.group(2), "ListParagraph", (1, 1 if len(bullet.group(1)) >= 2 else 0)))
            current_num = None
        elif number:
            if current_num is None:
                numbered_lists += 1
                current_num = num_offset + numbered_lists + 1  # 番号つきの箇条書きは、まとまりごとに 1 から数え直す
            out.append(_w_par(number.group(2), "ListParagraph", (current_num, 1 if len(number.group(1)) >= 2 else 0)))
        elif stripped in ("---", "***"):
            out.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
            current_num = None
        elif stripped:
            out.append(_w_par(stripped))
            current_num = None
        else:
            current_num = None
        i += 1
    out.append("<w:p/>")
    return "".join(out), numbered_lists


def _docx_numbering(numbered_lists: int) -> str:
    def level(i: int, fmt: str, text: str, indent: int) -> str:
        return (f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
                f'<w:pPr><w:ind w:left="{indent}" w:hanging="360"/></w:pPr></w:lvl>')

    bullets = level(0, "bullet", "•", 720) + level(1, "bullet", "◦", 1440)
    numbers = level(0, "decimal", "%1.", 720) + level(1, "decimal", "%2)", 1440)
    nums = '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    for n in range(numbered_lists):
        nums += (f'<w:num w:numId="{n + 2}"><w:abstractNumId w:val="1"/>'
                 '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering {_W}>'
            f'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>{bullets}</w:abstractNum>'
            f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>{numbers}</w:abstractNum>{nums}</w:numbering>')


def _docx_styles() -> str:
    def heading(n: int, size: int, color: str, before: int) -> str:
        return (f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/><w:basedOn w:val="Normal"/>'
                f'<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="{before}" w:after="120"/>'
                f'<w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:color w:val="{color}"/><w:sz w:val="{size}"/></w:rPr></w:style>')

    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles {_W}>'
            f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{_FONT_LATIN}" w:hAnsi="{_FONT_LATIN}" w:eastAsia="{_FONT_EA}" w:cs="{_FONT_LATIN}"/>'
            '<w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="en-US" w:eastAsia="ja-JP"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
            f'<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
            f'<w:pPr><w:pBdr><w:bottom w:val="single" w:sz="12" w:space="6" w:color="{GOLD}"/></w:pBdr><w:spacing w:after="240"/></w:pPr>'
            f'<w:rPr><w:b/><w:color w:val="{NAVY}"/><w:sz w:val="40"/></w:rPr></w:style>'
            + heading(1, 32, NAVY, 360) + heading(2, 26, "2A3766", 240) + heading(3, 23, "2A3766", 200) +
            '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
            '<w:qFormat/><w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="260" w:lineRule="auto"/>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F0F2F8"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="MS Gothic"/><w:sz w:val="19"/></w:rPr></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/>'
            '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:tblPr><w:tblBorders>'
            + "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="B9C2D9"/>' for side in ("top", "left", "bottom", "right", "insideH", "insideV")) +
            '</w:tblBorders></w:tblPr></w:style></w:styles>')


def write_docx(path: str | Path, markdown: str, title: str | None = None) -> Path:
    """Markdown(見出し・箇条書き・番号・表・太字・コード・--- で改ページ)から Word の文書を作る。"""
    markdown = unfence_markdown(markdown)
    body, numbered = _docx_body(markdown, title)
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {_W}><w:body>{body}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
                'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>')
    doc_title = title or next((m.group(1) for m in [re.search(r"^#\s+(.+)$", markdown, re.M)] if m), Path(path).stem)
    parts = {
        "[Content_Types].xml": _types({
            "/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            "/word/styles.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
            "/word/numbering.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"}),
        "_rels/.rels": _root_rels("word/document.xml"),
        "docProps/core.xml": _core(doc_title),
        "docProps/app.xml": _APP,
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
            '</Relationships>'),
        "word/document.xml": document,
        "word/styles.xml": _docx_styles(),
        "word/numbering.xml": _docx_numbering(numbered),
    }
    return _zip(Path(path), parts)


def append_docx(path: str | Path, markdown: str) -> Path:
    """既存の Word(.docx)の末尾に Markdown(段落・見出し・箇条書き・表)を追記する。ファイルが無ければ新規作成する。"""
    target = Path(path)
    if not target.is_file():
        return write_docx(target, markdown)
    markdown = unfence_markdown(markdown)
    with zipfile.ZipFile(target, "r") as z:
        parts = {name: z.read(name) for name in z.namelist()}
    if "word/document.xml" not in parts:
        raise ValueError(f"Word の形式として読めません: {target.name}")

    num_ids = [1]
    if "word/numbering.xml" in parts:
        num_xml = parts["word/numbering.xml"].decode("utf-8", errors="replace")
        num_ids += [int(x) for x in re.findall(r'w:numId="(\d+)"', num_xml)]
    else:
        num_xml = ""
    num_offset = max(num_ids)
    extra_body, numbered = _docx_body(markdown, None, num_offset=num_offset, heading_title=False)

    doc_xml = parts["word/document.xml"].decode("utf-8", errors="replace")
    sect_pos = doc_xml.rfind("<w:sectPr")
    body_end = doc_xml.rfind("</w:body>")
    insert_at = sect_pos if sect_pos != -1 else body_end
    if insert_at == -1:
        raise ValueError("Word 本文の位置が見つかりませんでした")
    parts["word/document.xml"] = (doc_xml[:insert_at] + extra_body + doc_xml[insert_at:]).encode("utf-8")

    if numbered and num_xml and "</w:numbering>" in num_xml:
        extra_nums = "".join(
            f'<w:num w:numId="{num_offset + n + 2}"><w:abstractNumId w:val="1"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
            for n in range(numbered)
        )
        parts["word/numbering.xml"] = num_xml.replace("</w:numbering>", extra_nums + "</w:numbering>", 1).encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        if "[Content_Types].xml" in parts:
            z.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        for name, data in parts.items():
            z.writestr(name, data)
    return target


# ---- Excel --------------------------------------------------------------------

_NUMBER = re.compile(r"^[-+]?(\d+(\.\d+)?|\.\d+)([eE][-+]?\d+)?$")
_GROUPED = re.compile(r"^[-+]?\d{1,3}(,\d{3})+(\.\d+)?$")
_PERCENT = re.compile(r"^[-+]?\d+(\.\d+)?%$")


def parse_table(data) -> dict[str, list[list]]:
    """表の元データを {シート名: 行の並び} にする。

    受け付ける形: CSV(タブ区切りも可)・Markdown の表・JSON(行の配列、辞書の配列、{シート名: 表})。
    """
    if isinstance(data, str):
        text = data.strip()
        if text.startswith(("[", "{")):
            try:
                data = json.loads(text)
            except ValueError:
                pass
    if isinstance(data, dict):
        return {str(name)[:31] or f"Sheet{i + 1}": _rows(value) for i, (name, value) in enumerate(data.items())}
    return {"Sheet1": _rows(data)}


def _rows(value) -> list[list]:
    if isinstance(value, list):
        if value and all(isinstance(r, dict) for r in value):
            keys: list[str] = []
            for r in value:
                keys += [k for k in r if k not in keys]
            return [keys] + [[r.get(k, "") for k in keys] for r in value]
        return [list(r) if isinstance(r, (list, tuple)) else [r] for r in value]
    text = pipe_tables(str(value).strip().replace("\r\n", "\n"))
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and all(l.strip().startswith("|") for l in lines):
        return _table_rows(lines)
    dialect = "excel-tab" if "\t" in text and "," not in text.splitlines()[0] else "excel"
    return [row for row in csv.reader(io.StringIO(text), dialect=dialect) if any(c.strip() for c in row)]


def _column(n: int) -> str:
    name = ""
    while n:
        n, rest = divmod(n - 1, 26)
        name = chr(65 + rest) + name
    return name


_UNITS = ("万円", "億円", "千円", "円", "ドル", "ユーロ", "元", "時間", "分", "秒", "個", "件", "人", "名", "本", "台",
          "枚", "冊", "回", "点", "社", "店", "kg", "km", "cm", "mm", "mL", "ml", "GB", "MB", "KB", "TB", "g", "m", "L", "t")
_UNIT_NUMBER = re.compile(r"^(?P<sign>[-+−▲△])?\s*(?P<pre>[¥￥$€£])?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)\s*"
                          r"(?P<suf>" + "|".join(map(re.escape, _UNITS)) + r")?$")


def _unit_number(value) -> tuple[float, str, str, int] | None:
    """「1,200円」「¥1,200」「▲300円」「12個」のような単位つきの数 → (値, 前の記号, 後ろの単位, 小数の桁)。"""
    if not isinstance(value, str):
        return None
    m = _UNIT_NUMBER.match(value.strip())
    if not m or not (m.group("pre") or m.group("suf")):
        return None
    num = m.group("num")
    number = float(num.replace(",", ""))
    if m.group("sign") in ("-", "−", "▲", "△"):  # ▲・△ は日本の帳票でよく使うマイナスの印
        number = -number
    return number, m.group("pre") or "", m.group("suf") or "", len(num.split(".")[1]) if "." in num else 0


def _number(value) -> float | None:
    """セルの値が数(桁区切りや単位つきも含む)なら、その値。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    plain = "" if value is None else str(value).strip()
    if _NUMBER.match(plain) or _GROUPED.match(plain):
        return float(plain.replace(",", ""))
    unit = _unit_number(plain)
    return unit[0] if unit else None


def _num_code(pre: str, suf: str, decimals: int, grouped: bool) -> str:
    """Excel の数の表示形式(例: #,##0"円"、"$"#,##0.00)。"""
    code = ("#,##0" if grouped else "0") + ("." + "0" * decimals if decimals else "")
    return (f'"{pre}"' if pre else "") + code + (f'"{suf}"' if suf else "")


class _Styles:
    """ブックで使うセルの書式を集めて styles.xml を作る。0: 標準 1: 見出し 2: 桁区切り 3: パーセント、4 から先は単位つきの数など。"""

    def __init__(self) -> None:
        self.codes: list[str] = []

    def of(self, code: str) -> int:
        if code == "#,##0":
            return 2
        if code == "0%":
            return 3
        if code not in self.codes:
            self.codes.append(code)
        return 4 + self.codes.index(code)

    def xml(self) -> str:
        fmts = "".join(f'<numFmt numFmtId="{164 + i}" formatCode="{_x(code)}"/>' for i, code in enumerate(self.codes))
        xfs = "".join(f'<xf numFmtId="{164 + i}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
                      for i in range(len(self.codes)))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                + (f'<numFmts count="{len(self.codes)}">{fmts}</numFmts>' if self.codes else "")
                + f'<fonts count="2"><font><sz val="11"/><name val="{_FONT_EA}"/><family val="3"/><charset val="128"/></font>'
                f'<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="{_FONT_EA}"/><family val="3"/><charset val="128"/></font></fonts>'
                '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                f'<fill><patternFill patternType="solid"><fgColor rgb="FF{NAVY}"/><bgColor indexed="64"/></patternFill></fill></fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                f'<cellXfs count="{4 + len(self.codes)}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
                '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
                f'<xf numFmtId="9" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>{xfs}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() and abs(number) < 1e15 else repr(float(number))


def _x_cell(ref: str, value, header: bool, grouped: bool = False, styles: _Styles | None = None,
            unit: tuple[str, str, int] | None = None, percent: int | None = None) -> str:
    """grouped: この列は大きな数を扱うので、数と数式の結果を桁区切り(1,200)で見せる。
    unit: この列の単位(前の記号, 後ろの単位, 小数の桁)。percent: この列はパーセント(小数の桁)。
    どちらも、数式の結果をその形で見せるために使う。"""
    styles = styles or _Styles()
    style = ' s="1"' if header else ""
    grouped_style = ' s="2"' if grouped else ""
    formula_style = grouped_style
    if unit:
        formula_style = f' s="{styles.of(_num_code(unit[0], unit[1], unit[2], grouped))}"'
    elif percent is not None:
        formula_style = f' s="{styles.of("0" + ("." + "0" * percent if percent else "") + "%")}"'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{style}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style or grouped_style}><v>{value}</v></c>'
    text = "" if value is None else str(value)
    plain = text.strip()
    if not header:
        if plain.startswith("=") and len(plain) > 1:
            return f'<c r="{ref}"{formula_style}><f>{_x(plain[1:])}</f></c>'  # 数式(開いたときに計算される)
        if _NUMBER.match(plain):
            return f'<c r="{ref}"{grouped_style}><v>{float(plain) if any(c in plain for c in ".eE") else int(plain)}</v></c>'
        if _GROUPED.match(plain):
            return f'<c r="{ref}" s="2"><v>{plain.replace(",", "")}</v></c>'  # 1,200 のような桁区切りの数
        if _PERCENT.match(plain):
            decimals = len(plain[:-1].split(".")[1]) if "." in plain else 0
            code = "0" + ("." + "0" * decimals if decimals else "") + "%"
            return f'<c r="{ref}" s="{styles.of(code)}"><v>{_plain(float(plain[:-1]) / 100)}</v></c>'
        found = _unit_number(plain)
        if found:  # 1,200円 のような単位つきの数: 数として入れて、単位は表示の形で見せる(合計の数式で足せるように)
            number, pre, suf, decimals = found
            if unit and (pre, suf) == unit[:2]:
                decimals = max(decimals, unit[2])
            code = _num_code(pre, suf, decimals, grouped or abs(number) >= 1000)
            return f'<c r="{ref}" s="{styles.of(code)}"><v>{_plain(number)}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{_x(text)}</t></is></c>'


_ABOVE = re.compile(r"\b(SUM|AVERAGE|MAX|MIN|COUNT|COUNTA|MEDIAN)\(\s*above\s*\)", re.I)
_TOTAL_LABELS = {"合計", "総計", "計", "合計額", "総額", "小計", "total", "sum", "grand total"}


def _headerless(rows: list[list]) -> bool:
    """1行目が見出しではなくデータか(「4月,120」のように、見出しを付けずに書かれた表)。"""
    if len(rows) < 2 or not rows[0]:
        return False

    def numeric(value) -> bool:
        return not str(value).strip().startswith("=") and _number(value) is not None

    first = [c for c, v in enumerate(rows[0]) if numeric(v)]
    # 数が年(2023 など)だけの行は、年ごとの列の見出しとみなす
    if not first or all(float(_number(rows[0][c])).is_integer() and 1900 <= _number(rows[0][c]) <= 2100 for c in first):
        return False
    below = rows[1]  # 見出しの行なら、下の行で数になっている列は、見出しでは文字のはず
    return all(c < len(below) and (numeric(below[c]) or str(below[c]).strip().startswith("=")) for c in first)


_RANGE_FN = re.compile(r"^=(SUM|AVERAGE|MAX|MIN|COUNT|MEDIAN)\(([A-Z]{1,3})(\d+):\2(\d+)\)$", re.I)


def _fix_formulas(rows: list[list], first: int = 2) -> list[list]:
    """=SUM(above) などを、その列のデータの最初の行からこの行の上までの範囲に直す(表の大きさに関係なく正しく書けるように)。

    「合計」「平均」の行の範囲の数式は、データの行ちょうどにそろえる(小さいAIは例の数式を写して、範囲を直し忘れることがある)。
    文字の列(月の名前など)を指している数式は、自分の列を指すように直す(文字を足しても 0 になるだけなので)。
    first はデータが始まる行(見出しがあれば 2、見出しのない表なら 1)。
    """
    def label_of(row: list) -> str:
        return str(row[0]).strip().lower() if row else ""

    def is_formula(value) -> bool:
        return isinstance(value, str) and value.strip().startswith("=")

    def has_numbers(col: int, top: int, bottom: int) -> bool:
        return any(col <= len(rows[n - 1]) and not is_formula(rows[n - 1][col - 1]) and _number(rows[n - 1][col - 1]) is not None
                   for n in range(top, bottom + 1))

    summary = _TOTAL_LABELS | _AVERAGE_LABELS
    fixed = []
    for r, row in enumerate(rows, 1):
        new_row = []
        for c, value in enumerate(row, 1):
            if is_formula(value) and r > first:
                col = _column(c)
                # この行より上で、合計・平均の行や数式ではない最後の行(データの終わり)
                last = max((n for n in range(first, r) if label_of(rows[n - 1]) not in summary
                            and not (c <= len(rows[n - 1]) and is_formula(rows[n - 1][c - 1]))), default=r - 1)
                value = _ABOVE.sub(lambda m: f"{m.group(1).upper()}({col}{first}:{col}{last})", value.strip())
                found = _RANGE_FN.match(value)
                if found and label_of(row) in summary:
                    fn, ref, top, bottom = found.group(1).upper(), found.group(2).upper(), int(found.group(3)), int(found.group(4))
                    if ref != col and not has_numbers(_column_index(ref), first, last) and has_numbers(c, first, last):
                        ref = col  # 数のない列(見出しの列など)を指している → 自分の列
                    if top in (1, 2):  # 表の最初から数える範囲は、データの行ちょうどにする(短すぎ・長すぎ・自分の行を含む循環を直す)
                        top, bottom = first, last
                    value = f"={fn}({ref}{top}:{ref}{bottom})"
            new_row.append(value)
        fixed.append(new_row)
    return fixed


_AVERAGE_LABELS = {"平均", "平均値", "average", "avg", "mean"}


def _decimals(value) -> int:
    text = str(value).strip().rstrip("%")
    unit = _unit_number(text)
    if unit:
        return unit[3]
    return len(text.split(".")[1]) if "." in text and _number(text) is not None else 0


def _live_totals(rows: list[list], first: int = 2, notes: list[str] | None = None) -> list[list]:
    """「合計」「平均」の行の数が、上のデータの行の合計・平均と同じ(表示の桁まで)なら、数式(=SUM・=AVERAGE)に置き換える。
    値は変えずに、あとでデータを直したときも合計が自動で正しくなるようにする(AIは合計を数で書くことが多い)。

    notes を渡したとき(新しく作るとき)は、合わない数も数式に直し、直したことを notes に足す(gemma3:4b が 120+135+128 の
    合計を 478 と書いていた。実際にあった)。今ある表を書き換えるときは、合わない数に触らない。"""
    def label(row: list) -> str:
        return str(row[0]).strip().lower() if row else ""

    out = [list(r) for r in rows]
    summary = _TOTAL_LABELS | _AVERAGE_LABELS
    top = first - 1  # データの最初の行(0 から数える)
    for r in range(top + 1, len(out)):
        kind = "SUM" if label(out[r]) in _TOTAL_LABELS else "AVERAGE" if label(out[r]) in _AVERAGE_LABELS else None
        data = [i for i in range(top, r) if label(out[i]) not in summary]
        # データの行が見出しのすぐ下から切れ目なく続き、その後ろは合計・平均の行だけのときに限る(小計をはさむ表は触らない)
        if not kind or not data or data != list(range(top, data[-1] + 1)):
            continue
        for c in range(1, len(out[r])):
            cell = out[r][c]
            value = _number(cell)
            if value is None or (isinstance(cell, str) and cell.strip().startswith("=")):
                continue
            numbers = [n for n in (_number(out[i][c]) if c < len(out[i]) else None for i in data) if n is not None]
            if len(numbers) < 2:
                continue
            expected = sum(numbers) if kind == "SUM" else sum(numbers) / len(numbers)
            col = _column(c + 1)
            formula = f"={kind}({col}{first}:{col}{data[-1] + 1})"
            if abs(value - expected) <= 0.5 * 10 ** -_decimals(cell) + 1e-9 * abs(expected):
                out[r][c] = formula
            elif notes is not None:
                out[r][c] = formula
                what = "合計" if kind == "SUM" else "平均"
                notes.append(f"「{out[r][0]}」の行の {cell} は、上の数の{what}({round(expected, 6):g})と違ったので、{what}の数式 "
                             f"{formula} にしました")
    return out


def _sheet(rows: list[list], styles: _Styles | None = None, notes: list[str] | None = None) -> str:
    styles = styles or _Styles()
    first = 1 if _headerless(rows) else 2  # データが始まる行(見出しを付けずに書かれた表は1行目から)
    rows = _fix_formulas(_live_totals(rows, first, notes), first)
    body = rows[first - 1:]
    cols = max((len(r) for r in rows), default=1)
    widths = []
    for i in range(cols):
        longest = max((_width(r[i]) for r in rows if i < len(r) and not str(r[i]).startswith("=")), default=8)
        widths.append(min(60, max(8, longest + 2)))
    col_xml = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    # 1000 以上の数がある列は、数も数式の結果も桁区切りで見せる
    grouped = [any(abs(n) >= 1000 for n in (_number(r[i]) for r in body if i < len(r)) if n is not None) for i in range(cols)]
    # 単位つきの数がある列は、いちばん多い単位を列の単位にする(合計などの数式の結果もその単位で見せる)
    units: list[tuple[str, str, int] | None] = []
    for i in range(cols):
        found = [u for u in (_unit_number(r[i]) for r in body if i < len(r)) if u]
        if not found:
            units.append(None)
            continue
        kinds = [(u[1], u[2]) for u in found]
        pre, suf = max(kinds, key=kinds.count)
        units.append((pre, suf, max(u[3] for u in found if (u[1], u[2]) == (pre, suf))))
    # 数がすべてパーセントの列は、数式の結果もパーセントで見せる(=SUM(above) が 1 ではなく 100% と見えるように)
    percents: list[int | None] = []
    for i in range(cols):
        values = [str(r[i]).strip() for r in body if i < len(r) and not str(r[i]).strip().startswith("=")]
        marked = [v for v in values if _PERCENT.match(v)]
        numeric = [v for v in values if _PERCENT.match(v) or _number(v) is not None]
        percents.append(max(len(v[:-1].split(".")[1]) if "." in v else 0 for v in marked) if marked and len(marked) == len(numeric) else None)
    data = []
    for r, row in enumerate(rows, 1):
        cells = "".join(_x_cell(f"{_column(c)}{r}", v, r == 1 and first == 2, grouped[c - 1], styles, units[c - 1], percents[c - 1])
                        for c, v in enumerate(row, 1))
        data.append(f'<row r="{r}">{cells}</row>')
    pane = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>') if len(rows) > 1 and first == 2 else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{pane}<sheetFormatPr defaultRowHeight="18"/><cols>{col_xml}</cols><sheetData>{"".join(data)}</sheetData></worksheet>')


_RESULT_FN = re.compile(r"^=(SUM|AVERAGE|MAX|MIN|COUNT|MEDIAN)\(\s*([A-Z]{1,3})(\d+)\s*:\s*([A-Z]{1,3})(\d+)\s*\)$", re.I)


def with_results(text: str) -> str:
    """read_text の表(CSV)の、範囲の数式(=SUM(B2:B4) など)に計算の結果を添える(AIが、できた表を自分で確かめられるように)。

    数のない範囲(見出しの列など)を指しているときは、そのことも書く。見せるためだけのもの(編集に使うと結果まで書き戻される)。
    """
    blocks = []
    for block in text.split("\n\n"):
        lines = block.split("\n")
        head = lines[:1] if lines and lines[0].startswith("[シート") else []
        rows = list(csv.reader(lines[len(head):]))

        def cell(col: int, row: int) -> str:
            return rows[row - 1][col - 1].strip() if 1 <= row <= len(rows) and 1 <= col <= len(rows[row - 1]) else ""

        changed = False
        for row in rows:
            for c, value in enumerate(row):
                m = _RESULT_FN.match(value.strip())
                if not m:
                    continue
                fn = m.group(1).upper()
                c1, c2 = sorted((_column_index(m.group(2).upper()), _column_index(m.group(4).upper())))
                r1, r2 = sorted((int(m.group(3)), int(m.group(5))))
                numbers = [n for rr in range(r1, r2 + 1) for cc in range(c1, c2 + 1)
                           if not cell(cc, rr).startswith("=") and (n := _number(cell(cc, rr))) is not None]
                if not numbers:
                    shown = ("0" if fn in ("SUM", "COUNT") else "エラー") + f"({m.group(2)}{r1}:{m.group(4)}{r2} に数がありません)"
                else:
                    ordered = sorted(numbers)
                    middle = len(ordered) // 2
                    result = {"SUM": sum(numbers), "AVERAGE": sum(numbers) / len(numbers), "MAX": max(numbers), "MIN": min(numbers),
                              "COUNT": float(len(numbers)),
                              "MEDIAN": ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2}[fn]
                    shown = _plain(round(result, 4))
                row[c] = f"{value.strip()} → {shown}"
                changed = True
        if changed:
            buffer = io.StringIO()
            csv.writer(buffer, lineterminator="\n").writerows(rows)
            block = "\n".join(head + [buffer.getvalue().rstrip("\n")])
        blocks.append(block)
    return "\n\n".join(blocks)


def write_xlsx(path: str | Path, data, notes: list[str] | None = None) -> Path:
    """表(CSV・Markdown の表・JSON)から Excel のブックを作る。1行目は見出し(固定・強調)。= で始まる値は数式。

    notes を渡すと(新しく作るとき)、合計の行の合わない数を合計の数式に直し、直したことを notes に足す。"""
    sheets = {name: rows for name, rows in parse_table(data).items() if rows} or {"Sheet1": [[""]]}
    names = list(sheets)
    sheet_entries = "".join(f'<sheet name="{_x(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, 1))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{sheet_entries}</sheets><calcPr calcId="0" fullCalcOnLoad="1"/></workbook>')
    rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                   f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))
    rels += (f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
             'Target="styles.xml"/>')
    overrides = {"/xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                 "/xl/styles.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"}
    for i in range(1, len(names) + 1):
        overrides[f"/xl/worksheets/sheet{i}.xml"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    parts = {
        "[Content_Types].xml": _types(overrides),
        "_rels/.rels": _root_rels("xl/workbook.xml"),
        "docProps/core.xml": _core(Path(path).stem),
        "docProps/app.xml": _APP,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                                      f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'),
    }
    styles = _Styles()
    for i, name in enumerate(names, 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet(sheets[name], styles, notes)
    parts["xl/styles.xml"] = styles.xml()  # シートで使った書式がそろってから作る
    return _zip(Path(path), parts)


# ---- PowerPoint ---------------------------------------------------------------

_P = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
_SLIDE_W, _SLIDE_H = 12192000, 6858000  # 16:9
_GROUP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
          '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def parse_slides(markdown: str) -> list[dict]:
    """「# 見出し」ごとに1枚。見出しの下の行は箇条書き(字下げで2段目)、| の行は表。--- でも区切れる。"""
    slides: list[dict] = []
    current: dict | None = None
    for line in unfence_markdown(markdown).replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        heading = re.match(r"^#{1,3}\s+(.*)$", stripped)
        if heading:
            current = {"title": heading.group(1).strip(), "points": [], "table": []}
            slides.append(current)
            continue
        if stripped.startswith("|") and current is not None:
            current["table"].append(line)  # 表の行(あとでまとめて表にする)
            continue
        if stripped in ("---", "***"):
            current = None
            continue
        if not stripped:
            continue
        if current is None:
            current = {"title": stripped.lstrip("-*・ ").strip(), "points": [], "table": []}
            slides.append(current)
            continue
        bullet = re.match(r"^(\s*)(?:[-*+]|・|\d+[.)])\s+(.*)$", line)
        if bullet:
            current["points"].append((1 if len(bullet.group(1)) >= 2 else 0, bullet.group(2).strip()))
        else:
            current["points"].append((0, stripped))
    return slides or [{"title": "(無題)", "points": [], "table": []}]


def _a_runs(text: str, size: int | None = None, color: str | None = None) -> str:
    runs = []
    for part, fmt in _inline(text):
        attrs = ' lang="ja-JP" altLang="en-US"' + (f' sz="{size}"' if size else "") + (' b="1"' if "b" in fmt else "") + (' i="1"' if "i" in fmt else "")
        fill = f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>' if color else ""
        props = f'<a:rPr{attrs} dirty="0">{fill}</a:rPr>' if fill else f'<a:rPr{attrs} dirty="0"/>'
        runs.append(f'<a:r>{props}<a:t>{_x(part)}</a:t></a:r>')
    return "".join(runs) or '<a:endParaRPr lang="ja-JP" dirty="0"/>'


def _placeholder(id_: int, name: str, ph: str, body: str, xfrm: str = "") -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{id_}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
            f'<p:spPr>{xfrm}</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>')


def _rect(id_: int, name: str, x: int, y: int, w: int, h: int, color: str) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{id_}" name="{name}"/><p:cNvSpPr/><p:nvPr userDrawn="1"/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr rtlCol="0" anchor="ctr"/><a:lstStyle/><a:p><a:endParaRPr lang="ja-JP"/></a:p></p:txBody></p:sp>')


def _xfrm(x: int, y: int, w: int, h: int) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'


def _pptx_master() -> str:
    title = _placeholder(2, "Title Placeholder 1", '<p:ph type="title"/>', '<a:p><a:r><a:rPr lang="ja-JP"/><a:t>タイトル</a:t></a:r></a:p>',
                         _xfrm(609600, 365125, 10972800, 1015000))
    body = _placeholder(3, "Text Placeholder 2", '<p:ph type="body" idx="1"/>', '<a:p><a:pPr lvl="0"/><a:r><a:rPr lang="ja-JP"/><a:t>本文</a:t></a:r></a:p>',
                        _xfrm(609600, 1600200, 10972800, 4572000))
    accent = _rect(4, "Accent", 0, 0, 152400, _SLIDE_H, NAVY)
    rule = _rect(5, "Rule", 609600, 1380125, 1219200, 38100, GOLD)
    level = lambda n, size, mar: (f'<a:lvl{n}pPr marL="{mar}" indent="-285750"><a:spcBef><a:spcPts val="600"/></a:spcBef>'
                                  f'<a:buClr><a:srgbClr val="{BLUE}"/></a:buClr><a:buFont typeface="Arial"/><a:buChar char="{"•" if n == 1 else "–"}"/>'
                                  f'<a:defRPr sz="{size}"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/>'
                                  f'<a:ea typeface="+mn-ea"/></a:defRPr></a:lvl{n}pPr>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sldMaster {_P}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
            f'<p:spTree>{_GROUP}{accent}{rule}{title}{body}</p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
            'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/><p:sldLayoutId id="2147483650" r:id="rId2"/></p:sldLayoutIdLst>'
            '<p:txStyles><p:titleStyle><a:lvl1pPr algn="l"><a:defRPr sz="3400" b="1"><a:solidFill><a:schemeClr val="tx2"/></a:solidFill>'
            '<a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/></a:defRPr></a:lvl1pPr></p:titleStyle>'
            f'<p:bodyStyle>{level(1, 2200, 342900)}{level(2, 1800, 742950)}</p:bodyStyle>'
            '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>')


def _pptx_layout(kind: str) -> str:
    if kind == "title":
        ctr = _placeholder(2, "Title 1", '<p:ph type="ctrTitle"/>', '<a:p><a:endParaRPr lang="ja-JP"/></a:p>', _xfrm(914400, 2130425, 10363200, 1470025))
        sub = _placeholder(3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>', '<a:p><a:endParaRPr lang="ja-JP"/></a:p>', _xfrm(914400, 3886200, 10363200, 1219200))
        rule = _rect(4, "Rule", 914400, 3700000, 1828800, 45720, GOLD)
        bg = f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{NAVY}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
        # 表紙では、元の型(マスター)の飾り(左の帯・見出しの下の線)を出さない
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sldLayout {_P} type="title" preserve="1" showMasterSp="0"><p:cSld name="Title Slide">{bg}'
                f'<p:spTree>{_GROUP}{ctr}{rule}{sub}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    title = _placeholder(2, "Title 1", '<p:ph type="title"/>', '<a:p><a:endParaRPr lang="ja-JP"/></a:p>')
    body = _placeholder(3, "Content 2", '<p:ph idx="1"/>', '<a:p><a:endParaRPr lang="ja-JP"/></a:p>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sldLayout {_P} type="obj" preserve="1"><p:cSld name="Title and Content">'
            f'<p:spTree>{_GROUP}{title}{body}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')


def _pptx_slide(slide: dict, first: bool) -> str:
    if first:  # 表紙は紺の地に白い文字
        title = _placeholder(2, "Title 1", '<p:ph type="ctrTitle"/>', f'<a:p>{_a_runs(slide["title"], 4000, "FFFFFF")}</a:p>')
        sub = "".join(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>{_a_runs(text, 2000, "C9D2F0")}</a:p>'
                      for _, text in slide["points"]) or '<a:p><a:endParaRPr lang="ja-JP"/></a:p>'
        body = _placeholder(3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>', sub)
    else:
        title = _placeholder(2, "Title 1", '<p:ph type="title"/>', f'<a:p>{_a_runs(_short(slide["title"], 40))}</a:p>')
        paras = "".join(f'<a:p><a:pPr lvl="{lvl}"/>{_a_runs(text)}</a:p>' for lvl, text in slide["points"])
        rows = _table_rows(slide.get("table") or [])
        if rows:  # 表のあるスライド: 箇条書きがあれば上に、表をその下に置く
            top = 1600200
            body = ""
            if paras:
                body = _placeholder(3, "Content 2", '<p:ph idx="1"/>', paras, _xfrm(609600, 1600200, 10972800, 1150000))
                top += 1250000
            body += _a_table(4, rows, 609600, top, 10972800, _SLIDE_H - top - 400000)
        else:
            body = _placeholder(3, "Content 2", '<p:ph idx="1"/>', paras or '<a:p><a:endParaRPr lang="ja-JP"/></a:p>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sld {_P}><p:cSld><p:spTree>{_GROUP}{title}{body}</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def _a_table(id_: int, rows: list[list[str]], x: int, y: int, w: int, max_h: int) -> str:
    """スライドの表(見出しの行は紺の地に白い太字、行ごとに薄く色を変える)。"""
    cols = max(len(r) for r in rows)
    weights = [max(4, max((_width(r[i]) if i < len(r) else 0) for r in rows)) for i in range(cols)]
    widths = [int(w * k / sum(weights)) for k in weights]
    widths[-1] = w - sum(widths[:-1])
    row_h = min(457200, max(274320, max_h // max(1, len(rows))))
    size = 1600 if len(rows) <= 8 else 1200
    line = lambda tag: f'<a:{tag} w="9525"><a:solidFill><a:srgbClr val="B9C2D9"/></a:solidFill></a:{tag}>'
    body = []
    for n, row in enumerate(rows):
        cells = []
        for i in range(cols):
            text = row[i] if i < len(row) else ""
            if n == 0:
                runs, fill = _a_runs(f"**{text}**" if text and "**" not in text else text, size, "FFFFFF"), NAVY
            else:
                runs, fill = _a_runs(text, size), "F4F6FB" if n % 2 == 0 else "FFFFFF"
            cells.append(f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{runs}</a:p></a:txBody>'
                         f'<a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">'
                         f'{line("lnL")}{line("lnR")}{line("lnT")}{line("lnB")}<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill></a:tcPr></a:tc>')
        body.append(f'<a:tr h="{row_h}">{"".join(cells)}</a:tr>')
    grid = "".join(f'<a:gridCol w="{cw}"/>' for cw in widths)
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{id_}" name="Table {id_}"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/>'
            f'</p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{row_h * len(rows)}"/></p:xfrm>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr firstRow="1" bandRow="1"/>'
            f'<a:tblGrid>{grid}</a:tblGrid>{"".join(body)}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')


def _pptx_theme() -> str:
    def fill(alpha: str = "") -> str:
        return f'<a:solidFill><a:schemeClr val="phClr">{alpha}</a:schemeClr></a:solidFill>'

    fills = fill() + fill('<a:tint val="50000"/>') + fill('<a:shade val="80000"/>')
    lines = "".join(f'<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>'
                    for w in (6350, 12700, 19050))
    effects = '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Celestial"><a:themeElements>'
            '<a:clrScheme name="Celestial"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
            f'<a:dk2><a:srgbClr val="{NAVY}"/></a:dk2><a:lt2><a:srgbClr val="{PALE}"/></a:lt2><a:accent1><a:srgbClr val="{BLUE}"/></a:accent1>'
            f'<a:accent2><a:srgbClr val="{GOLD}"/></a:accent2><a:accent3><a:srgbClr val="2A3766"/></a:accent3><a:accent4><a:srgbClr val="5B8DEF"/></a:accent4>'
            '<a:accent5><a:srgbClr val="2E9E6B"/></a:accent5><a:accent6><a:srgbClr val="D9534F"/></a:accent6>'
            '<a:hlink><a:srgbClr val="2F56D9"/></a:hlink><a:folHlink><a:srgbClr val="6B4FBB"/></a:folHlink></a:clrScheme>'
            '<a:fontScheme name="Celestial"><a:majorFont><a:latin typeface="Segoe UI Semibold"/><a:ea typeface="Yu Gothic UI Semibold"/><a:cs typeface=""/></a:majorFont>'
            '<a:minorFont><a:latin typeface="Segoe UI"/><a:ea typeface="Yu Gothic UI"/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
            f'<a:fmtScheme name="Celestial"><a:fillStyleLst>{fills}</a:fillStyleLst><a:lnStyleLst>{lines}</a:lnStyleLst>'
            f'<a:effectStyleLst>{effects}</a:effectStyleLst><a:bgFillStyleLst>{fills}</a:bgFillStyleLst></a:fmtScheme>'
            '</a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


def write_pptx(path: str | Path, markdown: str) -> Path:
    """Markdown の見出しごとに1枚のスライドを作る。最初の1枚は表紙(紺の地、見出しの下の行は副題)。"""
    slides = parse_slides(markdown)
    rel = lambda i, kind, target: f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}" Target="{target}"/>'
    rels_doc = lambda items: ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                              f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}</Relationships>')
    presentation_rels = rel(1, "slideMaster", "slideMasters/slideMaster1.xml")
    slide_ids = ""
    for i in range(len(slides)):
        presentation_rels += rel(i + 2, "slide", f"slides/slide{i + 1}.xml")
        slide_ids += f'<p:sldId id="{256 + i}" r:id="rId{i + 2}"/>'
    presentation_rels += rel(len(slides) + 2, "theme", "theme/theme1.xml")
    presentation = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:presentation {_P} saveSubsetFonts="1">'
                    '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                    f'<p:sldIdLst>{slide_ids}</p:sldIdLst><p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/>'
                    '</p:presentation>')
    overrides = {"/ppt/presentation.xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
                 "/ppt/slideMasters/slideMaster1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
                 "/ppt/slideLayouts/slideLayout1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
                 "/ppt/slideLayouts/slideLayout2.xml": "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
                 "/ppt/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml"}
    parts = {
        "_rels/.rels": _root_rels("ppt/presentation.xml"),
        "docProps/core.xml": _core(slides[0]["title"]),
        "docProps/app.xml": _APP,
        "ppt/presentation.xml": presentation,
        "ppt/_rels/presentation.xml.rels": rels_doc(presentation_rels),
        "ppt/slideMasters/slideMaster1.xml": _pptx_master(),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": rels_doc(rel(1, "slideLayout", "../slideLayouts/slideLayout1.xml")
                                                                 + rel(2, "slideLayout", "../slideLayouts/slideLayout2.xml")
                                                                 + rel(3, "theme", "../theme/theme1.xml")),
        "ppt/slideLayouts/slideLayout1.xml": _pptx_layout("title"),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": rels_doc(rel(1, "slideMaster", "../slideMasters/slideMaster1.xml")),
        "ppt/slideLayouts/slideLayout2.xml": _pptx_layout("content"),
        "ppt/slideLayouts/_rels/slideLayout2.xml.rels": rels_doc(rel(1, "slideMaster", "../slideMasters/slideMaster1.xml")),
        "ppt/theme/theme1.xml": _pptx_theme(),
    }
    for i, slide in enumerate(slides, 1):
        parts[f"ppt/slides/slide{i}.xml"] = _pptx_slide(slide, first=i == 1)
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = rels_doc(rel(1, "slideLayout", f"../slideLayouts/slideLayout{1 if i == 1 else 2}.xml"))
        overrides[f"/ppt/slides/slide{i}.xml"] = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
    parts["[Content_Types].xml"] = _types(overrides)
    return _zip(Path(path), parts)


def append_pptx(path: str | Path, markdown: str) -> Path:
    """既存の PowerPoint(.pptx)の末尾に新しいスライドを追加する。ファイルが無ければ新規作成する。"""
    target = Path(path)
    if not target.is_file():
        return write_pptx(target, markdown)
    slides = parse_slides(markdown)
    with zipfile.ZipFile(target, "r") as z:
        parts = {name: z.read(name) for name in z.namelist()}
    required = {"ppt/presentation.xml", "ppt/_rels/presentation.xml.rels", "[Content_Types].xml"}
    if not required <= set(parts):
        raise ValueError(f"PowerPoint の形式として読めません: {target.name}")

    existing_nums = [int(m.group(1)) for n in parts if (m := re.match(r"ppt/slides/slide(\d+)\.xml$", n))]
    next_slide = max(existing_nums, default=0) + 1
    pres_xml = parts["ppt/presentation.xml"].decode("utf-8", errors="replace")
    rels_xml = parts["ppt/_rels/presentation.xml.rels"].decode("utf-8", errors="replace")
    ct_xml = parts["[Content_Types].xml"].decode("utf-8", errors="replace")

    next_rid = max((int(x) for x in re.findall(r'Id="rId(\d+)"', rels_xml)), default=1) + 1
    next_sid = max((int(x) for x in re.findall(r'<p:sldId\s+id="(\d+)"', pres_xml)), default=255) + 1

    layout_target = "../slideLayouts/slideLayout2.xml" if "ppt/slideLayouts/slideLayout2.xml" in parts else "../slideLayouts/slideLayout1.xml"
    new_rel_nodes, new_sld_nodes, new_ct_nodes = [], [], []
    rels_doc = lambda items: ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                              f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}</Relationships>')
    rel_xml = lambda rid, kind, tgt: f'<Relationship Id="rId{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}" Target="{tgt}"/>'

    for i, slide in enumerate(slides):
        s_num = next_slide + i
        r_num = next_rid + i
        sid = next_sid + i
        parts[f"ppt/slides/slide{s_num}.xml"] = _pptx_slide(slide, first=False).encode("utf-8")
        parts[f"ppt/slides/_rels/slide{s_num}.xml.rels"] = rels_doc(rel_xml(1, "slideLayout", layout_target)).encode("utf-8")
        new_rel_nodes.append(rel_xml(r_num, "slide", f"slides/slide{s_num}.xml"))
        new_sld_nodes.append(f'<p:sldId id="{sid}" r:id="rId{r_num}"/>')
        if f"/ppt/slides/slide{s_num}.xml" not in ct_xml:
            new_ct_nodes.append(f'<Override PartName="/ppt/slides/slide{s_num}.xml" '
                                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')

    if "</Relationships>" not in rels_xml or "</Types>" not in ct_xml:
        raise ValueError("PowerPoint の関連ファイルが不正です")
    rels_xml = rels_xml.replace("</Relationships>", "".join(new_rel_nodes) + "</Relationships>", 1)
    ct_xml = ct_xml.replace("</Types>", "".join(new_ct_nodes) + "</Types>", 1)
    if "</p:sldIdLst>" in pres_xml:
        pres_xml = pres_xml.replace("</p:sldIdLst>", "".join(new_sld_nodes) + "</p:sldIdLst>", 1)
    elif "<p:sldIdLst/>" in pres_xml:
        pres_xml = pres_xml.replace("<p:sldIdLst/>", f"<p:sldIdLst>{''.join(new_sld_nodes)}</p:sldIdLst>", 1)
    else:
        raise ValueError("PowerPoint のスライド一覧が見つかりません")

    parts["ppt/presentation.xml"] = pres_xml.encode("utf-8")
    parts["ppt/_rels/presentation.xml.rels"] = rels_xml.encode("utf-8")
    parts["[Content_Types].xml"] = ct_xml.encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        for name, data in parts.items():
            z.writestr(name, data)
    return target


def render_chart(path: str | Path, data, *, kind: str = "bar", title: str = "") -> Path:
    """CSV・Markdown の表・JSON のデータから、見やすいグラフ(.svg または .html)を生成する。"""
    import math as _math

    tables = parse_table(data)
    sheet_name, rows = next(iter(tables.items()))
    if len(rows) < 2 or len(rows[0]) < 2:
        raise ValueError("グラフには見出し行と、ラベル＋数値を含む 2 行以上が必要です")
    chart_title = title or (sheet_name if sheet_name != "Sheet1" else "Chart")
    headers = [str(h) for h in rows[0]]
    labels: list[str] = []
    series_vals: list[list[float]] = [[] for _ in range(len(headers) - 1)]
    for r in rows[1:]:
        labels.append(str(r[0] if r else ""))
        for si in range(1, len(headers)):
            raw_v = r[si] if si < len(r) else 0
            num = _number(raw_v)
            if num is None:
                parsed = _unit_number(raw_v)
                num = parsed[0] if parsed else 0.0
            series_vals[si - 1].append(float(num))

    colors = ["#2f56d9", "#c9a227", "#1f8a5b", "#c0392b", "#7c3aed", "#0284c7"]
    w, h_px = 760, 440
    ml, mr, mt, mb = 72, 28, 56, 74
    pw, ph = w - ml - mr, h_px - mt - mb
    kind = kind if kind in ("bar", "line", "pie") else "bar"

    parts: list[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h_px}" '
                        'style="font-family:Segoe UI,Yu Gothic UI,sans-serif;background:#ffffff">']
    parts.append(f'<text x="{w // 2}" y="30" text-anchor="middle" font-size="18" font-weight="600" fill="#17203f">{_x(chart_title)}</text>')

    if kind == "pie":
        vals = [max(0.0, v) for v in series_vals[0]]
        total = sum(vals) or 1.0
        cx, cy, r_px = 230, 235, 138
        ang = -_math.pi / 2
        for idx, (lab, v) in enumerate(zip(labels, vals)):
            frac = v / total
            step = frac * 2 * _math.pi
            x1, y1 = cx + r_px * _math.cos(ang), cy + r_px * _math.sin(ang)
            x2, y2 = cx + r_px * _math.cos(ang + step), cy + r_px * _math.sin(ang + step)
            large = 1 if step > _math.pi else 0
            col = colors[idx % len(colors)]
            if frac >= 0.9999:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r_px}" fill="{col}"/>')
            elif v > 0:
                parts.append(f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} A {r_px} {r_px} 0 {large} 1 {x2:.1f} {y2:.1f} Z" '
                             f'fill="{col}" stroke="#fff" stroke-width="2"><title>{_x(lab)}: {v:g} ({frac*100:.1f}%)</title></path>')
            ly = 95 + (idx % 12) * 26
            lx = 420 if idx < 12 else 590
            parts.append(f'<rect x="{lx}" y="{ly - 11}" width="13" height="13" rx="3" fill="{col}"/>')
            parts.append(f'<text x="{lx + 20}" y="{ly}" font-size="12.5" fill="#1f2937">{_x(lab[:14])}: {v:g} ({frac*100:.0f}%)</text>')
            ang += step
    else:
        flat = [v for s in series_vals for v in s] or [0.0]
        vmin, vmax = min(0.0, min(flat)), max(0.0, max(flat))
        if vmax == vmin:
            vmax = vmin + 1.0
        y_pos = lambda val: mt + ph * (vmax - val) / (vmax - vmin)
        y0 = y_pos(0.0)
        for tick in range(5):
            gv = vmin + (vmax - vmin) * tick / 4
            gy = y_pos(gv)
            parts.append(f'<line x1="{ml}" y1="{gy:.1f}" x2="{ml + pw}" y2="{gy:.1f}" stroke="#e5e7eb"/>')
            parts.append(f'<text x="{ml - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#6b7280">{gv:,.0f}</text>' if abs(vmax - vmin) >= 10 else
                         f'<text x="{ml - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#6b7280">{gv:.2f}</text>')
        parts.append(f'<line x1="{ml}" y1="{y0:.1f}" x2="{ml + pw}" y2="{y0:.1f}" stroke="#9ca3af"/>')
        n = max(1, len(labels))
        band = pw / n
        if kind == "bar":
            ns = max(1, len(series_vals))
            bw = max(4.0, min(42.0, (band * 0.72) / ns))
            for i, lab in enumerate(labels):
                gx = ml + i * band + (band - bw * ns) / 2
                for si, vals in enumerate(series_vals):
                    v = vals[i]
                    top_y = y_pos(max(0.0, v))
                    bot_y = y_pos(min(0.0, v))
                    bh = max(1.0, bot_y - top_y)
                    col = colors[si % len(colors)]
                    parts.append(f'<rect x="{gx + si * bw:.1f}" y="{top_y:.1f}" width="{bw - 1.5:.1f}" height="{bh:.1f}" '
                                 f'rx="2" fill="{col}"><title>{_x(lab)} / {_x(headers[si+1])}: {v:g}</title></rect>')
                parts.append(f'<text x="{ml + (i + 0.5) * band:.1f}" y="{h_px - mb + 18}" text-anchor="middle" font-size="11" fill="#374151">{_x(lab[:12])}</text>')
        else:  # line
            for si, vals in enumerate(series_vals):
                col = colors[si % len(colors)]
                pts = [(ml + (i + 0.5) * band, y_pos(v)) for i, v in enumerate(vals)]
                d = " ".join(f"{'M' if k == 0 else 'L'} {px:.1f} {py:.1f}" for k, (px, py) in enumerate(pts))
                parts.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round"/>')
                for (px, py), lab, v in zip(pts, labels, vals):
                    parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{col}"><title>{_x(lab)}: {v:g}</title></circle>')
            for i, lab in enumerate(labels):
                parts.append(f'<text x="{ml + (i + 0.5) * band:.1f}" y="{h_px - mb + 18}" text-anchor="middle" font-size="11" fill="#374151">{_x(lab[:12])}</text>')
        if len(headers) > 2:
            for si, hname in enumerate(headers[1:]):
                lx = ml + si * 130
                parts.append(f'<rect x="{lx}" y="{h_px - 26}" width="11" height="11" rx="2" fill="{colors[si % len(colors)]}"/>')
                parts.append(f'<text x="{lx + 16}" y="{h_px - 16}" font-size="11.5" fill="#374151">{_x(hname[:16])}</text>')
    parts.append("</svg>")
    svg = "\n".join(parts)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".svg":
        target.write_text(svg, encoding="utf-8")
    else:
        head_cells = "".join(f"<th>{_x(c)}</th>" for c in headers)
        body_rows = "".join("<tr>" + "".join(f"<td>{_x(c)}</td>" for c in r[:len(headers)]) + "</tr>" for r in rows[1:])
        html_doc = (
            "<!doctype html>\n<html lang=\"ja\"><head><meta charset=\"utf-8\">"
            f"<title>{_x(chart_title)}</title>"
            "<style>body{font-family:'Segoe UI','Yu Gothic UI',sans-serif;max-width:860px;margin:28px auto;padding:0 16px;color:#17203f;background:#f8fafc}"
            ".card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,.04)}"
            "svg{width:100%;height:auto;display:block}table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}"
            "th,td{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left}th{background:#f1f5f9}</style></head>"
            f"<body><div class=\"card\">{svg}<table><thead><tr>{head_cells}</tr></thead><tbody>{body_rows}</tbody></table></div></body></html>"
        )
        target.write_text(html_doc, encoding="utf-8")
    return target


# ---- 汎用データ・文書変換 ----------------------------------------------------

def _load_structured(src: Path):
    """JSON / YAML / TOML / CSV / TSV / Excel / Markdown表 を読み込んで Python データに直す。"""
    suf = src.suffix.lower()
    if suf in (".json", ".yaml", ".yml", ".toml"):
        raw = src.read_text(encoding="utf-8", errors="replace")
        if suf == ".json":
            return json.loads(raw), None
        if suf in (".yaml", ".yml"):
            import yaml
            return yaml.safe_load(raw), None
        import tomllib
        return tomllib.loads(raw), None
    if suf in (".csv", ".tsv"):
        delim = "\t" if suf == ".tsv" else ","
        with open(src, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=delim))
        return rows, rows
    if suf == ".xlsx":
        tables = parse_table(read_text(src))
        # "[シート: name]" ブロックまたは単一表を取り出す
        text = read_text(src)
        parsed = []
        for blk in text.split("\n\n"):
            lines = [ln for ln in blk.splitlines() if not ln.startswith("[シート:")]
            if lines:
                parsed.extend(list(csv.reader(lines)))
                break
        rows = parsed or next(iter(tables.values()), [])
        return rows, rows
    if suf == ".md":
        md = src.read_text(encoding="utf-8", errors="replace")
        tbl = first_table(md)
        if tbl:
            return tbl, tbl
        return md, None
    raise ValueError(f"未対応の変換元形式です: {src.suffix}")


def _rows_to_dicts(rows: list[list[str]]) -> list[dict] | list[list[str]]:
    if len(rows) >= 2 and all(isinstance(h, str) and h.strip() for h in rows[0]):
        headers = [h.strip() for h in rows[0]]
        return [{headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))} for r in rows[1:]]
    return rows


def _dicts_to_rows(data) -> list[list[str]]:
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        cols = list(dict.fromkeys(k for d in data for k in d))
        return [cols] + [[str(d.get(c, "")) for c in cols] for d in data]
    if isinstance(data, list) and all(isinstance(r, (list, tuple)) for r in data):
        return [[str(c) for c in r] for r in data]
    if isinstance(data, dict):
        return [["key", "value"]] + [[str(k), json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)]
                                      for k, v in data.items()]
    raise ValueError("表形式 (行の配列またはオブジェクト配列) に変換できないデータです")


def convert_file(src: str | Path, dest: str | Path) -> Path:
    """各種データ・文書形式 (.json/.yaml/.toml/.csv/.tsv/.md/.txt/.html/.docx/.xlsx/.pptx/.pdf) を相互変換する。"""
    src_p, dest_p = Path(src), Path(dest)
    if not src_p.is_file():
        raise FileNotFoundError(f"ファイルがありません: {src_p}")
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    ss, ds = src_p.suffix.lower(), dest_p.suffix.lower()

    structured_exts = {".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".xlsx"}
    doc_read_exts = {".docx", ".xlsx", ".pptx", ".pdf"}

    # 1) 構造化データ間の変換
    if ss in structured_exts or (ss == ".md" and ds in {".json", ".yaml", ".yml", ".csv", ".tsv", ".xlsx"}):
        if ss == ".xlsx" and ds in (".md", ".txt", ".html"):
            txt = read_text(src_p)
            if ds == ".html":
                html_doc = (
                    f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><title>{_x(src_p.stem)}</title>"
                    "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:28px auto;padding:0 16px}"
                    "pre{white-space:pre-wrap;background:#f8fafc;padding:14px;border-radius:8px}</style></head>"
                    f"<body><h1>{_x(src_p.name)}</h1><pre>{_x(txt)}</pre></body></html>"
                )
                dest_p.write_text(html_doc, encoding="utf-8")
                return dest_p
            dest_p.write_text(txt, encoding="utf-8")
            return dest_p
        obj, rows = _load_structured(src_p)
        if ds in (".csv", ".tsv"):
            table = rows if rows is not None else _dicts_to_rows(obj)
            delim = "\t" if ds == ".tsv" else ","
            buf = io.StringIO()
            csv.writer(buf, delimiter=delim, lineterminator="\n").writerows(table)
            dest_p.write_text(buf.getvalue(), encoding="utf-8")
            return dest_p
        if ds == ".xlsx":
            table = rows if rows is not None else _dicts_to_rows(obj)
            return write_xlsx(dest_p, table)
        if ds == ".json":
            payload = _rows_to_dicts(rows) if rows is not None else obj
            dest_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return dest_p
        if ds in (".yaml", ".yml"):
            import yaml
            payload = _rows_to_dicts(rows) if rows is not None else obj
            dest_p.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return dest_p
        if ds == ".md":
            table = rows if rows is not None else _dicts_to_rows(obj)
            if not table:
                dest_p.write_text("", encoding="utf-8")
                return dest_p
            head = "| " + " | ".join(str(c) for c in table[0]) + " |"
            sep = "| " + " | ".join("---" for _ in table[0]) + " |"
            body = ["| " + " | ".join(str(c) for c in r) + " |" for r in table[1:]]
            dest_p.write_text("\n".join([head, sep, *body]), encoding="utf-8")
            return dest_p

    # 2) 文書読み込み (.docx/.pptx/.pdf/.md/.txt/.html) -> (.md/.txt/.html/.docx/.pptx/.json)
    if ss in doc_read_exts:
        text = read_text(src_p)
    else:
        raw = src_p.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)) if ss in (".html", ".htm") else raw

    if ds in (".md", ".txt"):
        dest_p.write_text(text, encoding="utf-8")
        return dest_p
    if ds in (".html", ".htm"):
        from markdown_it import MarkdownIt
        rendered = MarkdownIt("commonmark", {"breaks": True}).enable("table").render(text)
        doc = (
            f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><title>{_x(src_p.stem)}</title>"
            "<style>body{font-family:'Segoe UI','Yu Gothic UI',sans-serif;max-width:860px;margin:32px auto;padding:0 18px;line-height:1.7;color:#111827}"
            "table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #d1d5db;padding:8px 10px;text-align:left}"
            "th{background:#f3f4f6}pre,code{background:#f1f5f9;border-radius:6px;padding:2px 6px}pre{padding:12px;overflow:auto}</style></head>"
            f"<body>{rendered}</body></html>"
        )
        dest_p.write_text(doc, encoding="utf-8")
        return dest_p
    if ds == ".docx":
        return write_docx(dest_p, text, title=src_p.stem)
    if ds == ".pptx":
        return write_pptx(dest_p, answer_to_slides(text, src_p.stem))
    if ds == ".json":
        dest_p.write_text(json.dumps({"title": src_p.stem, "text": text}, ensure_ascii=False, indent=2), encoding="utf-8")
        return dest_p
    raise ValueError(f"{ss} → {ds} の変換には対応していません")
