"""條款 PDF 解析工具測試；不需網路，也不讀取真實保單檔案。

分段規則以文字直接測試；PDF 讀取以程式產生的兩頁英文 PDF 測試（內建字型不含中文）。
執行：python -B -m unittest discover -s tests -p test_parse_pdf.py -v
外部套件：pdfplumber。
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools import parse_pdf as pp


def make_pdf(path: Path, pages: list[list[str]]) -> None:
    """產生最小可用的 PDF：每頁數行 Helvetica 文字。"""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        stream = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join(
            f"({line}) Tj T*" for line in lines) + " ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_no = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


class SplitParagraphsTest(unittest.TestCase):
    def test_rejoins_wrapped_cjk_lines_and_splits_at_articles(self):
        text = ("第八條 宣告利率之計算\n本公司於每月第一個營業日\n公告宣告利率。\n"
                "第九條 保險金額\n一、身故保險金\n依約定給付。")
        paras = pp.split_paragraphs(text)
        self.assertEqual([p["text"] for p in paras], [
            "第八條 宣告利率之計算本公司於每月第一個營業日公告宣告利率。",  # 行內原有空白保留
            "第九條 保險金額", "一、身故保險金依約定給付。"])
        self.assertEqual([p["article"] for p in paras], ["第八條", "第九條", "第九條"])

    def test_text_content_is_preserved(self):
        text = "繳費年期 6年期\n投保年齡 0歲~74歲\n\n最低投保金額：6,000美元"
        joined = "".join(p["text"] for p in pp.split_paragraphs(text))
        self.assertEqual(pp._normalize(joined), pp._normalize(text))

    def test_latin_words_keep_a_space(self):
        self.assertEqual(pp.split_paragraphs("Death benefit is\npaid in USD.")[0]["text"],
                         "Death benefit is paid in USD.")

    def test_long_paragraph_is_split_at_sentence_end(self):
        text = "本契約條款內容說明。" * 100
        pieces = pp.split_paragraphs(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p["text"]) <= pp.MAX_PARAGRAPH_CHARS for p in pieces))
        self.assertEqual("".join(p["text"] for p in pieces), text)

    def test_empty_page(self):
        self.assertEqual(pp.split_paragraphs("   \n\n"), [])


class ParseDocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.pdf = self.folder / "sample.pdf"
        make_pdf(self.pdf, [["Article 1 Scope", "Premium is paid yearly."],
                            ["Declared rate is not guaranteed.", "Rate may change monthly."]])

    def test_pages_and_paragraphs_keep_page_numbers(self):
        doc = pp.parse_document(self.pdf, "doc_test", source_url="https://example.com/t.pdf")
        self.assertEqual(doc["page_count"], 2)
        self.assertEqual(doc["empty_pages"], [])
        self.assertEqual(len(doc["sha256"]), 64)
        self.assertIn("Premium is paid yearly.", doc["pages"][0]["text"])
        page_two = [p for p in doc["paragraphs"] if p["page"] == 2]
        self.assertTrue(page_two)
        self.assertTrue(all(p["paragraph_id"].startswith("doc_test_p002_") for p in page_two))
        self.assertEqual(len({p["paragraph_id"] for p in doc["paragraphs"]}), len(doc["paragraphs"]))

    def test_locate_dataset_evidence(self):
        doc = pp.parse_document(self.pdf, "doc_test", source_url="https://example.com/t.pdf")
        dataset = {"evidence": [
            {"evidence_id": "E_ok", "source_type": "pdf", "source_url": "https://example.com/t.pdf",
             "page": 2, "excerpt": "Declared rate is   not guaranteed."},
            {"evidence_id": "E_wrong_page", "source_type": "pdf", "source_url": "https://example.com/t.pdf",
             "page": 1, "excerpt": "Rate may change monthly."},
            {"evidence_id": "E_missing", "source_type": "pdf", "source_url": "https://example.com/t.pdf",
             "page": 1, "excerpt": "（計算值）570 × 87.7"},
            {"evidence_id": "E_other_doc", "source_type": "pdf", "source_url": "https://example.com/x.pdf",
             "page": 1, "excerpt": "Premium"},
        ]}
        result = {r["evidence_id"]: r for r in pp.locate_dataset_evidence(doc, dataset)}
        self.assertEqual(set(result), {"E_ok", "E_wrong_page", "E_missing"})
        self.assertEqual(result["E_ok"]["verdict"], "頁碼相符")
        self.assertEqual(result["E_wrong_page"]["found_pages"], [2])
        self.assertIn("頁碼不符", result["E_wrong_page"]["verdict"])
        self.assertEqual(result["E_missing"]["found_pages"], [])

    def test_cli_writes_output(self):
        out = self.folder / "out"
        code = pp.main([str(self.pdf), "--doc-id", "doc_cli", "--output-dir", str(out)])
        self.assertEqual(code, 0)
        data = json.loads((out / "doc_cli.json").read_text(encoding="utf-8"))
        self.assertEqual(data["page_count"], 2)

    def test_rejects_non_pdf_and_bad_id(self):
        fake = self.folder / "fake.pdf"
        fake.write_text("not a pdf", encoding="utf-8")
        with self.assertRaises(pp.PdfParseError):
            pp.parse_document(fake, "doc_fake")
        with self.assertRaises(pp.PdfParseError):
            pp.parse_document(self.folder / "missing.pdf", "doc_missing")
        self.assertEqual(pp.main([str(self.pdf), "--doc-id", "bad id!"]), 2)


if __name__ == "__main__":
    unittest.main()
