import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from celestial.backends.base import ModelInfo
from celestial.backends.mock import MockBackend
from celestial.config import Config
from celestial.evolve.store import Store
from celestial.knowledge import KnowledgeBase, chunk_text
from celestial.manager import Celestial


def _pdf_bytes(text: str) -> bytes:
    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj\n"
        b"4 0 obj<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>stream\n"
        + stream + b"\nendstream endobj\ntrailer<< /Root 1 0 R >>\n%%EOF"
    )


class KnowledgeAndMemoryTest(unittest.TestCase):
    def test_chunk_text_splits_with_overlap(self):
        text = "\n\n".join(f"段落{i}: " + ("あ" * 180) for i in range(5))
        chunks = chunk_text(text, max_chars=400, overlap=60)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 450 for c in chunks))

    def test_kb_note_file_folder_url_and_hybrid_search(self):
        def embed(text: str):
            return [
                1.0 if ("東京本社" in text or "オフィスの所在地" in text) else 0.0,
                1.0 if ("経費精算" in text or "領収書" in text) else 0.0,
                0.05,
            ]

        backend = MockBackend("mock", lambda _m, _msgs, _s=None: "ok", [ModelInfo("small", "mock", int(1e9))], embedder=embed)
        store = Store()
        app = Celestial(Config(home=Path("unused"), cache=False), backends={"mock": backend}, store=store)
        kb = app.kb

        kb.add_note("拠点案内", "東京本社の住所は東京都千代田区丸の内1-1-1です。", tags="社内")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rules.md").write_text("経費精算は月末締め翌月15日払いです。領収書原本を添付してください。", encoding="utf-8")
            (root / "manual.pdf").write_bytes(_pdf_bytes("Celestial Manual Page One"))
            (root / "ignore.png").write_bytes(b"\x89PNG\r\n")
            res = kb.add_folder(root)
            self.assertEqual(res["indexed"], 2)

        with mock.patch("celestial.web.fetch_url", return_value="社内VPNの接続手順書です。SSL-VPNクライアントを利用します。"):
            kb.add_url("https://example.com/vpn", title="VPN手順")

        st = kb.stats()
        self.assertEqual(st["docs"], 4)
        self.assertGreaterEqual(st["embedded"], 4)

        hits = kb.search("オフィスの所在地はどこですか", top_k=2)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "拠点案内")

        pdf_hits = kb.search("Manual Page", top_k=2)
        self.assertTrue(any("Celestial Manual Page One" in h["text"] for h in pdf_hits))

        # 削除できること
        doc_id = kb.list_docs()[0]["id"]
        self.assertTrue(kb.remove(doc_id))
        self.assertEqual(kb.stats()["docs"], 3)

    def test_rag_context_injection_and_toggle(self):
        prompts: list[str] = []

        def respond(model, msgs, schema=None):
            if schema and "role" in schema.get("properties", {}):
                return json.dumps({"role": "general"})
            prompts.append(msgs[0]["content"] + "\n" + msgs[-1]["content"])
            return "回答しました"

        with tempfile.TemporaryDirectory() as tmp:
            backend = MockBackend("mock", respond, [ModelInfo("small", "mock", int(1e9))])
            app = Celestial(Config(home=Path(tmp), cache=False), backends={"mock": backend}, store=Store())
            app.kb.add_note("プロジェクトZ仕様", "プロジェクトZのリリース日は2027年4月1日です。")

            r1 = app.run("プロジェクトZのリリース日はいつですか?")
            self.assertIn("2027年4月1日", prompts[-1])
            self.assertTrue(any("プロジェクトZ仕様" in s["title"] for s in r1.sources))

            app.set_setting("use_kb", False)
            prompts.clear()
            r2 = app.run("プロジェクトZのリリース日はいつですか?")
            self.assertNotIn("2027年4月1日", prompts[-1])
            self.assertEqual(r2.sources, [])

    def test_natural_memory_commands_and_tool_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = MockBackend("mock", lambda _m, _msgs, _s=None: "ok", [ModelInfo("small", "mock", int(1e9))])
            app = Celestial(Config(home=Path(tmp), cache=False), backends={"mock": backend}, store=Store())

            r_save = app.run("覚えて: 好きな飲み物は緑茶")
            self.assertIn("緑茶", r_save.text)
            self.assertIn("好きな飲み物は緑茶", app.cfg.memories)

            r_list = app.run("何覚えてる?")
            self.assertIn("緑茶", r_list.text)

            r_forget = app.run("忘れて: 緑茶")
            self.assertIn("忘れました", r_forget.text)
            self.assertEqual(app.cfg.memories, [])

            # _memorize_tool 経由の保存・一覧・削除
            out_save = app._memorize_tool("remember", "愛犬の名前はポチ")
            self.assertIn("覚えました", out_save)
            self.assertIn("ポチ", app._memorize_tool("list", ""))
            app._memorize_tool("forget", "ポチ")
            self.assertEqual(app.cfg.memories, [])


if __name__ == "__main__":
    unittest.main()

