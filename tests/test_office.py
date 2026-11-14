"""Word・Excel・PowerPoint の作成(標準ライブラリだけで書く Office Open XML)の試験。"""

import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from celestial import office
from celestial.tools import ToolContext, ToolError, execute

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def parts(path: Path) -> dict[str, ET.Element]:
    """ZIP の中の XML をすべて読む(どれも XML として正しいことも確かめる)。"""
    with zipfile.ZipFile(path) as z:
        assert z.namelist()[0] == "[Content_Types].xml"
        return {name: ET.fromstring(z.read(name)) for name in z.namelist()}


class OfficeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_word(self):
        md = "# 計画\n\n## 決定事項\n- 公開は **12月1日**\n  - 広告を中心に\n1. テスト\n2. 申請\n\n手順2\n1. もう一度\n\n| 項目 | 担当 |\n|---|---|\n| 試験 | 開発 |\n\n```\ncode & <tag>\n```"
        x = parts(office.write_docx(self.dir / "a.docx", md))
        body = x["word/document.xml"].find(f"{W}body")
        text = "".join(t.text or "" for t in body.iter(f"{W}t"))
        for expected in ("計画", "決定事項", "12月1日", "広告を中心に", "試験", "code & <tag>"):
            self.assertIn(expected, text)
        styles = [p.find(f"{W}pPr/{W}pStyle").get(f"{W}val") for p in body.iter(f"{W}p") if p.find(f"{W}pPr/{W}pStyle") is not None]
        self.assertEqual(styles[:2], ["Title", "Heading2"])
        self.assertEqual(len(body.findall(f"{W}tbl")), 1)
        # 番号つきの箇条書きは、まとまりごとに 1 から数え直す(別々の番号の定義を使う)
        nums = {p.find(f"{W}pPr/{W}numPr/{W}numId").get(f"{W}val") for p in body.iter(f"{W}p")
                if p.find(f"{W}pPr/{W}numPr") is not None and p.find(f"{W}pPr/{W}numPr/{W}numId").get(f"{W}val") != "1"}
        self.assertEqual(len(nums), 2)
        self.assertIn("word/numbering.xml", x)

    def test_excel(self):
        x = parts(office.write_xlsx(self.dir / "b.xlsx", '月,売上,前年比\n4月,"1,200,000",105%\n5月,1350000,112%\n合計,=SUM(B2:B3),'))
        cells = {c.get("r"): c for c in x["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["A1"].get("t"), "inlineStr")
        self.assertEqual(cells["A1"].get("s"), "1")  # 見出しの書式
        self.assertEqual(cells["B2"].find(f"{S}v").text, "1200000")
        self.assertEqual(cells["B3"].find(f"{S}v").text, "1350000")
        self.assertEqual(cells["B3"].get("s"), "2")  # 大きな数の列は桁区切り
        self.assertEqual(cells["C2"].find(f"{S}v").text, "1.05")
        self.assertEqual(cells["B4"].find(f"{S}f").text, "SUM(B2:B3)")
        self.assertEqual(cells["B4"].get("s"), "2")

    def test_excel_without_header_and_text_column(self):
        # 小さいAIは見出しを付けず、合計の式で文字の列(A)を指しがち: データは1行目から、式は数の列(B)に直す
        path = office.write_xlsx(self.dir / "r.xlsx", "4月,120\n5月,135\n6月,128\n合計,=SUM(A2:A4)")
        cells = {c.get("r"): c for c in parts(path)["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B4"].find(f"{S}f").text, "SUM(B1:B3)")
        self.assertIsNone(cells["A1"].get("s"))                      # 1行目はデータなので、見出しの書式にしない
        self.assertIn("=SUM(B1:B3) → 383", office.with_results(office.read_text(path)))
        # 年が並ぶ1行目は見出しのまま
        years = office.write_xlsx(self.dir / "y.xlsx", "地域,2023,2024\n東京,100,200\n大阪,50,70\n合計,=SUM(above),=SUM(above)")
        self.assertIn("=SUM(C2:C3) → 270", office.with_results(office.read_text(years)))

    def test_results_show_empty_ranges(self):
        shown = office.with_results("[シート: S]\n名前,点\nA,x\nB,y\n合計,=SUM(B2:B3)\n平均,=AVERAGE(B2:B3)")
        self.assertIn("=SUM(B2:B3) → 0(B2:B3 に数がありません)", shown)
        self.assertIn("=AVERAGE(B2:B3) → エラー", shown)
        ctx = ToolContext(self.dir, allow_write=True, confirm=False)
        made = execute(ctx, "make_excel", {"path": "売上.xlsx", "data": "月,売上\n4月,120\n5月,135\n合計,=SUM(above)"})
        self.assertIn("=SUM(B2:B3) → 255", made)                      # 作ったときに、できた表を結果つきで見せる
        self.assertIn("=SUM(B2:B3) → 255", execute(ctx, "read_file", {"path": "売上.xlsx"}))

    def test_excel_formulas_cover_the_data(self):
        circular = parts(office.write_xlsx(self.dir / "h.xlsx", "月,売上\n4月,100\n5月,200\n合計,=SUM(B2:B6)"))
        cells = {c.get("r"): c for c in circular["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B4"].find(f"{S}f").text, "SUM(B2:B3)")  # 自分の行まで含める循環参照も直す
        x = parts(office.write_xlsx(self.dir / "g.xlsx", "月,売上,件数\n4月,100,1\n5月,200,2\n6月,300,3\n合計,=SUM(B2:B3),=SUM(above)\n平均,=AVERAGE(above),"))
        cells = {c.get("r"): c for c in x["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B5"].find(f"{S}f").text, "SUM(B2:B4)")  # 合計の行の範囲の直し忘れを直す
        self.assertEqual(cells["C5"].find(f"{S}f").text, "SUM(C2:C4)")  # above はこの行の上まで
        self.assertEqual(cells["B6"].find(f"{S}f").text, "AVERAGE(B2:B4)")  # 合計の行は含めない

    def test_excel_numbers_with_units(self):
        # 「1,200,000円」のような単位つきの数も数として入れる(文字のままだと合計の数式が 0 になる)
        path = office.write_xlsx(self.dir / "u.xlsx", '月,売上,前年比,件数\n4月,"1,200,000円",105%,12件\n5月,"¥1,350,000",112.5%,▲3件\n合計,=SUM(above),,=SUM(above)')
        x = parts(path)
        cells = {c.get("r"): c for c in x["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B2"].find(f"{S}v").text, "1200000")
        self.assertEqual(cells["B3"].find(f"{S}v").text, "1350000")
        self.assertEqual(cells["D3"].find(f"{S}v").text, "-3")  # ▲ はマイナス
        self.assertEqual(cells["C3"].find(f"{S}v").text, "1.125")
        codes = {int(f.get("numFmtId")): f.get("formatCode") for f in x["xl/styles.xml"].iter(f"{S}numFmt")}
        xfs = x["xl/styles.xml"].find(f"{S}cellXfs").findall(f"{S}xf")
        code_of = lambda ref: codes.get(int(xfs[int(cells[ref].get("s"))].get("numFmtId")))
        self.assertEqual(code_of("B2"), '#,##0"円"')
        self.assertEqual(code_of("B3"), '"¥"#,##0')
        self.assertEqual(code_of("B4"), '#,##0"円"')  # 合計の数式の結果も、列の単位で見せる
        self.assertEqual(code_of("D4"), '0"件"')
        self.assertEqual(code_of("C3"), "0.0%")
        self.assertEqual(int(x["xl/styles.xml"].find(f"{S}cellXfs").get("count")), len(xfs))
        # 読み返すと Excel での見え方になる(単位を失わずに書き直せる)
        text = office.read_text(path)
        self.assertIn('4月,"1,200,000円",105%,12件', text)
        self.assertIn('5月,"¥1,350,000",112.5%,-3件', text)
        self.assertEqual(office._unit_number("4月"), None)  # 月や日は単位として扱わない(見出しの「4月」は文字のまま)

    def test_written_totals_become_formulas(self):
        # AI が数で書いた合計・平均も、上の行の合計・平均と同じなら数式にする(データを直すと合計も変わる)
        rows = office.first_table("| 店舗 | 売上 | 構成比 | 前年比 |\n|---|---|---|---|\n| 東京 | 1,200,000円 | 40% | 105% |\n"
                                  "| 大阪 | 950,000円 | 35% | 98% |\n| 名古屋 | 850,000円 | 25% | 110% |\n"
                                  "| 合計 | 3,000,000円 | =SUM(above) | 101% |\n| 平均 | 1,000,000円 | | |")
        x = parts(office.write_xlsx(self.dir / "t.xlsx", rows))
        cells = {c.get("r"): c for c in x["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B5"].find(f"{S}f").text, "SUM(B2:B4)")
        self.assertEqual(cells["B6"].find(f"{S}f").text, "AVERAGE(B2:B4)")  # 合計の行は平均に含めない
        self.assertIsNone(cells["D5"].find(f"{S}f"))  # 合計と合わない数(前年比)は、そのまま
        codes = {int(f.get("numFmtId")): f.get("formatCode") for f in x["xl/styles.xml"].iter(f"{S}numFmt")}
        xfs = x["xl/styles.xml"].find(f"{S}cellXfs").findall(f"{S}xf")
        fmt = lambda ref: xfs[int(cells[ref].get("s"))].get("numFmtId")
        self.assertEqual(codes[int(fmt("B5"))], '#,##0"円"')
        self.assertEqual(fmt("C5"), "9")  # パーセントの列の数式は 100% と見せる
        # 合わない合計(AIの計算違い)は勝手に直さない
        wrong = parts(office.write_xlsx(self.dir / "w.xlsx", "月,売上\n4月,100\n5月,200\n合計,301"))
        self.assertIsNone({c.get("r"): c for c in wrong["xl/worksheets/sheet1.xml"].iter(f"{S}c")}["B4"].find(f"{S}f"))

    def test_agent_edit_keeps_units(self):
        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=self.dir / ".backup")
        execute(ctx, "make_excel", {"path": "売上.xlsx", "data": "月,売上\n4月,1200円\n5月,1350円"})
        execute(ctx, "append_file", {"path": "売上.xlsx", "content": "合計,=SUM(above)"})
        text = execute(ctx, "read_file", {"path": "売上.xlsx"})
        self.assertIn('4月,"1,200円"', text)
        self.assertIn("合計,=SUM(B2:B3)", text)
        cells = {c.get("r"): c for c in parts(self.dir / "売上.xlsx")["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B2"].find(f"{S}v").text, "1200")  # 書き直したあとも数のまま

    def test_excel_sheets_and_formats(self):
        x = parts(office.write_xlsx(self.dir / "c.xlsx", '{"国内": [["店舗","売上"],["東京",500]], "海外": [{"国":"米国","売上":800}]}'))
        names = [s.get("name") for s in x["xl/workbook.xml"].iter(f"{S}sheet")]
        self.assertEqual(names, ["国内", "海外"])
        second = {c.get("r"): c for c in x["xl/worksheets/sheet2.xml"].iter(f"{S}c")}
        self.assertEqual(second["A1"].find(f"{S}is/{S}t").text, "国")
        self.assertEqual(second["B2"].find(f"{S}v").text, "800")
        md = office.parse_table("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(md, {"Sheet1": [["a", "b"], ["1", "2"]]})

    def test_slides(self):
        path = office.write_pptx(self.dir / "d.pptx", "# 表紙\n副題\n\n# 内容\n- 点1\n  - 小さな点\n- 点2\n\n# まとめ\n- 以上")
        x = parts(path)
        slides = sorted(n for n in x if n.startswith("ppt/slides/slide"))
        self.assertEqual(len(slides), 3)
        texts = ["".join(t.text or "" for t in x[f"ppt/slides/slide{i}.xml"].iter(f"{A}t")) for i in (1, 2, 3)]
        self.assertEqual(texts, ["表紙副題", "内容点1小さな点点2", "まとめ以上"])
        levels = [p.get("lvl") for p in x["ppt/slides/slide2.xml"].iter(f"{A}pPr")]
        self.assertEqual(levels, ["0", "1", "0"])
        self.assertIn("ppt/theme/theme1.xml", x)

    def test_answer_export_handles_fenced_tables(self):
        answer = "まとめます。\n\n```markdown\n| 月 | 売上 |\n|---|---|\n| 4月 | 1,200 |\n```\n\n以上です。"
        x = parts(office.export_answer("docx", answer, "売上", self.dir / "e.docx"))
        self.assertEqual(len(x["word/document.xml"].find(f"{W}body").findall(f"{W}tbl")), 1)  # 囲いの中の表も表になる
        x = parts(office.export_answer("pptx", answer, "とても長い会話の題名なので途中で切って短くする必要がある", self.dir / "e.pptx"))
        slides = sorted(n for n in x if re.match(r"ppt/slides/slide\d+\.xml$", n))
        tables = [n for n in slides if list(x[n].iter(f"{A}tbl"))]
        self.assertEqual(len(tables), 1)  # 表はスライドの表になる
        titles = ["".join(t.text or "" for t in next(x[n].iter("{http://schemas.openxmlformats.org/presentationml/2006/main}sp")).iter(f"{A}t")) for n in slides]
        self.assertTrue(all(len(t) <= 40 for t in titles))
        with self.assertRaises(ValueError):
            office.export_answer("xlsx", "表のない回答", "題", self.dir / "f.xlsx")

    def test_tables_without_outer_pipes(self):
        # 両端の | を省いた表(GFM では正しい書き方。小さいAIはよくこう書く)も表として扱う
        answer = "売上表です。\n\n月 | 売上\n------- | --------\n4月 | 1,200,000円\n合計 | =SUM(above)\n\n以上です。"
        self.assertEqual(office.first_table(answer), [["月", "売上"], ["4月", "1,200,000円"], ["合計", "=SUM(above)"]])
        x = parts(office.export_answer("docx", answer, "売上", self.dir / "p.docx"))
        self.assertEqual(len(x["word/document.xml"].find(f"{W}body").findall(f"{W}tbl")), 1)
        x = parts(office.export_answer("xlsx", answer, "売上", self.dir / "p.xlsx"))
        cells = {c.get("r"): c for c in x["xl/worksheets/sheet1.xml"].iter(f"{S}c")}
        self.assertEqual(cells["B3"].find(f"{S}f").text, "SUM(B2:B2)")
        self.assertEqual(office.parse_table("a | b\n-|-\n1 | 2"), {"Sheet1": [["a", "b"], ["1", "2"]]})
        # 区切りの行がなければ表ではない。コードブロックの中も変えない
        plain = "a | b の違い\nどちらでもよい"
        self.assertEqual(office.pipe_tables(plain), plain)
        fenced = "```\nx | y\n--- | ---\n```"
        self.assertEqual(office.pipe_tables(fenced), fenced)

    def test_code_with_commas_is_not_a_table(self):
        self.assertIsNone(office.first_table("```\ndef f(a, b):\n    return a + b\n```"))
        self.assertEqual(office.first_table("```\na,b\n1,2\n```"), [["a", "b"], ["1", "2"]])
        self.assertEqual(office.first_table("```csv\n名前,点\n佐藤,80\n```"), [["名前", "点"], ["佐藤", "80"]])

    def test_file_names(self):
        self.assertEqual(office.file_name("作業フォルダに、4月 1,200,000円、5月 1,350,000円、6月 1…"),
                         "作業フォルダに、4月 1,200,000円、5月 1,350,000円")  # 切れた題名は切れ目まで戻す
        self.assertEqual(office.file_name("売上の報告"), "売上の報告")
        self.assertEqual(office.file_name('a/b:c*?"d'), "a b c d")  # ファイル名に使えない文字を除く
        self.assertEqual(office.file_name("…"), "回答")

    def test_tools_create_documents_in_workspace(self):
        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=self.dir / ".backup")
        self.assertIn("作成しました", execute(ctx, "make_word", {"path": "報告", "content": "# 報告\n本文"}))
        self.assertTrue((self.dir / "報告.docx").is_file())  # 拡張子は自動で付く
        execute(ctx, "make_excel", {"path": "表.xlsx", "data": "a,b\n1,2"})
        execute(ctx, "make_slides", {"path": "発表.pptx", "content": "# 題\n- 点"})
        self.assertIn("上書き", execute(ctx, "make_word", {"path": "報告.docx", "content": "# 報告\n更新"}))
        self.assertTrue(any((self.dir / ".backup").rglob("報告.docx")))  # 上書きの前の版が残る
        with self.assertRaises(ToolError):
            execute(ctx, "make_word", {"path": "../外.docx", "content": "x"})
        with self.assertRaises(ToolError):  # ファイルの変更が許可されていなければ使えない
            execute(ToolContext(workspace=self.dir), "make_excel", {"path": "x.xlsx", "data": "a"})

    def test_agent_can_read_but_not_text_edit_office_files(self):
        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=self.dir / ".backup")
        execute(ctx, "make_excel", {"path": "売上.xlsx", "data": "月,売上\n4月,\"1,200\"\n合計,=SUM(B2:B2)"})
        execute(ctx, "make_word", {"path": "報告.docx", "content": "# 報告\n- 要点"})
        execute(ctx, "make_slides", {"path": "発表.pptx", "content": "# 題\n副題\n\n# 内容\n- 点"})
        sheet = execute(ctx, "read_file", {"path": "売上.xlsx"})
        self.assertIn("月,売上", sheet)
        self.assertIn("4月,1200", sheet)
        self.assertIn("=SUM(B2:B2)", sheet)  # 数式は数式として読める
        self.assertIn("要点", execute(ctx, "read_file", {"path": "報告.docx"}))
        self.assertIn("[スライド 2]", execute(ctx, "read_file", {"path": "発表.pptx"}))
        with self.assertRaises(ToolError) as caught:
            execute(ctx, "edit_file", {"path": "報告.docx", "old": "要点", "new": "x"})
        self.assertIn("作り直して", str(caught.exception))  # 部分置換は壊さずに、作り直しを案内する

        append_msg = execute(ctx, "append_file", {"path": "報告.docx", "content": "\n\n## 追記\n追加テキスト"})
        self.assertIn("追記", append_msg)
        docx_text = execute(ctx, "read_file", {"path": "報告.docx"})
        self.assertIn("要点", docx_text)
        self.assertIn("追加テキスト", docx_text)

        pptx_msg = execute(ctx, "append_file", {"path": "発表.pptx", "content": "# 3 枚目\n\n- 追加項目"})
        self.assertIn("追記", pptx_msg)
        pptx_text = execute(ctx, "read_file", {"path": "発表.pptx"})
        self.assertIn("題", pptx_text)
        self.assertIn("[スライド 2]", pptx_text)
        self.assertIn("[スライド 3]", pptx_text)
        self.assertIn("追加項目", pptx_text)

        # stdlib PDF テキスト抽出 + make_chart (SVG / HTML) の検証
        import zlib
        pdf_stream = zlib.compress(b"BT /F1 12 Tf 72 720 Td (Celestial PDF Test) Tj ET")
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
            b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
            b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj\n"
            b"4 0 obj<< /Length " + str(len(pdf_stream)).encode() + b" /Filter /FlateDecode >>stream\n"
            + pdf_stream + b"\nendstream endobj\ntrailer<< /Root 1 0 R >>\n%%EOF"
        )
        (self.dir / "資料.pdf").write_bytes(pdf_bytes)
        pdf_text = execute(ctx, "read_file", {"path": "資料.pdf"})
        self.assertIn("Celestial PDF Test", pdf_text)

        chart_msg = execute(ctx, "make_chart", {
            "path": "グラフ.svg",
            "title": "月別件数",
            "kind": "bar",
            "data": [{"label": "1月", "value": 12}, {"label": "2月", "value": 24}],
        })
        self.assertIn("グラフ", chart_msg)
        svg = (self.dir / "グラフ.svg").read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("月別件数", svg)

    def test_agent_edits_excel_as_csv(self):
        ctx = ToolContext(workspace=self.dir, allow_write=True, backup_dir=self.dir / ".backup")
        # 書く道具に .xlsx の名前と CSV を渡しても、Excel のブックになる
        self.assertIn("作成しました", execute(ctx, "write_file", {"path": "売上.xlsx", "content": "月,売上\n4月,1200\n5月,1350\n6月,1280"}))
        # 表の最後に合計の行を足す(above は上のデータの行すべて)
        self.assertIn("追記しました", execute(ctx, "append_file", {"path": "売上.xlsx", "content": "合計,=SUM(above)"}))
        # CSV として見えている文字を書き換える
        self.assertIn("編集しました", execute(ctx, "edit_file", {"path": "売上.xlsx", "old": "5月,1350", "new": "5月,1400"}))
        text = execute(ctx, "read_file", {"path": "売上.xlsx"})
        self.assertIn("5月,1400", text)
        self.assertIn("合計,=SUM(B2:B4)", text)
        with self.assertRaises(ToolError):
            execute(ctx, "edit_file", {"path": "売上.xlsx", "old": "存在しない", "new": "x"})
        x = parts(self.dir / "売上.xlsx")  # Excel のブックとして正しい形のまま
        self.assertIn("xl/workbook.xml", x)


if __name__ == "__main__":
    unittest.main()
