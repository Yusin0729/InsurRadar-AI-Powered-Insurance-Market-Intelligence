"""解析保險條款／商品 DM 的 PDF：逐頁取出文字、切成段落並保留頁碼。

用法（在專案根目錄）：
    python -m tools.parse_pdf data/real/docs/ACUPL_DM_2026_06.pdf --doc-id doc_acupl_dm
    python -m tools.parse_pdf <PDF> --doc-id <ID> --locate data/real/dataset.json
        # 另外檢查快照中指向這份文件的證據原文，實際出現在第幾頁（只回報，不修改快照）

輸出：data/real/processed/pdf/<doc-id>.json
- pages：每頁的原始抽取文字（pdfplumber 原樣輸出）。
- paragraphs：段落清單，每段帶 paragraph_id、頁碼、頁內序號、所屬條號（如「第八條」）。
- empty_pages：抽不到文字的頁面（多半是掃描圖片，需要 OCR，本工具不處理）。
- 檔案 SHA-256 與頁數，用來確認之後引用的是同一個版本。

段落規則：同一頁內把被 PDF 換行切斷的句子接回，遇到空行、條號（第X條）、
項次（一、／（一）／1.）或句末標點（。！？）就分段。段落不跨頁，頁碼才能精確對應。
CJK 字之間的換行直接接起，英數之間補一個空白，不改動任何文字內容。

外部套件：pdfplumber。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "pdf"
TAIPEI = timezone(timedelta(hours=8))

MAX_PARAGRAPH_CHARS = 600   # 太長的段落在句號處再切開，方便引用與檢索

ARTICLE_RE = re.compile(r"^\s*(第\s*[一二三四五六七八九十百零〇0-9]+\s*條)")
ITEM_RE = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十0-9]+[）)]|\d{1,2}[.、．]\s*\D)")
SENTENCE_END = "。！？"
CJK_RE = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


class PdfParseError(RuntimeError):
    """檔案不存在、不是 PDF 或無法讀取。"""


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
def extract_pages(pdf_path: Path) -> list[dict[str, Any]]:
    """逐頁抽取文字；回傳 [{page, text}]，頁碼從 1 開始。"""
    import pdfplumber
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            return [{"page": number, "text": page.extract_text() or ""}
                    for number, page in enumerate(pdf.pages, start=1)]
    except (PDFSyntaxError, OSError, ValueError) as err:
        raise PdfParseError(f"無法讀取 PDF：{pdf_path}（{err}）") from err
    except Exception as err:  # pdfminer 對損壞檔案會拋出各種例外
        raise PdfParseError(f"PDF 解析失敗：{pdf_path}（{type(err).__name__}: {err}）") from err


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------
def _join(left: str, right: str) -> str:
    """接回被換行切斷的文字：兩側都是 CJK 或標點時直接相連，否則補一個空白。"""
    if not left:
        return right
    if CJK_RE.match(left[-1]) or CJK_RE.match(right[0]):
        return left + right
    return f"{left} {right}"


def _split_long(text: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[。！？；])", text):
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def split_paragraphs(page_text: str) -> list[dict[str, Any]]:
    """把一頁文字切成段落；回傳 [{text, article}]。article 為該段開頭的條號或 None。"""
    paragraphs: list[dict[str, Any]] = []
    current, article = "", None

    def flush():
        nonlocal current, article
        text = current.strip()
        if text:
            for piece in _split_long(text):
                paragraphs.append({"text": piece, "article": article})
        current = ""

    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        heading = ARTICLE_RE.match(line)
        if heading or ITEM_RE.match(line):
            flush()
            if heading:
                article = re.sub(r"\s+", "", heading.group(1))
        current = _join(current, line)
        if current.endswith(tuple(SENTENCE_END)):
            flush()
    flush()
    return paragraphs


def parse_document(pdf_path: Path, doc_id: str, *, source_url: str | None = None,
                   title: str | None = None) -> dict[str, Any]:
    if not pdf_path.exists():
        raise PdfParseError(f"找不到檔案：{pdf_path}")
    content = pdf_path.read_bytes()
    if not content.startswith(b"%PDF"):
        raise PdfParseError(f"不是 PDF 檔：{pdf_path}")
    pages = extract_pages(pdf_path)

    paragraphs, article = [], None
    for page in pages:
        for index, item in enumerate(split_paragraphs(page["text"]), start=1):
            # 條號可能在前一頁出現，沿用到後面的段落，直到出現新的條號
            if item["article"]:
                article = item["article"]
            paragraphs.append({
                "paragraph_id": f"{doc_id}_p{page['page']:03d}_{index:03d}",
                "page": page["page"], "index": index,
                "article": article, "text": item["text"],
            })

    try:
        relative = str(pdf_path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        relative = str(pdf_path)
    return {
        "doc_id": doc_id, "title": title, "source_url": source_url,
        "source_file": relative, "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content), "page_count": len(pages),
        "parsed_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "empty_pages": [p["page"] for p in pages if not p["text"].strip()],
        "pages": pages, "paragraphs": paragraphs,
    }


# ---------------------------------------------------------------------------
# 證據定位：確認快照的原文片段實際在第幾頁
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text)


def locate_excerpt(document: dict[str, Any], excerpt: str) -> list[int]:
    """回傳原文（忽略空白與全半形差異）完整出現的頁碼；找不到回傳空清單。"""
    needle = _normalize(excerpt)
    if not needle:
        return []
    return [p["page"] for p in document["pages"] if needle in _normalize(p["text"])]


def locate_dataset_evidence(document: dict[str, Any], dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """檢查快照中指向這份文件（local_path 或 source_url 相同）的 pdf 證據。"""
    results = []
    for evidence in dataset.get("evidence", []):
        if evidence.get("source_type") != "pdf":
            continue
        same_file = evidence.get("local_path") and evidence["local_path"] == document["source_file"]
        same_url = document.get("source_url") and evidence.get("source_url") == document["source_url"]
        if not (same_file or same_url):
            continue
        found = locate_excerpt(document, evidence.get("excerpt") or "")
        recorded = evidence.get("page")
        if not found:
            verdict = "原文在 PDF 中找不到（可能是摘要、計算值或混入說明文字）"
        elif recorded in found:
            verdict = "頁碼相符"
        else:
            verdict = f"頁碼不符：快照記錄第 {recorded} 頁"
        results.append({"evidence_id": evidence["evidence_id"], "recorded_page": recorded,
                        "found_pages": found, "verdict": verdict})
    return results


# ---------------------------------------------------------------------------
# 指令列
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="解析條款／DM PDF，切段並保留頁碼")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--doc-id", required=True, help="文件識別碼，例如 doc_acupl_dm；只用英數與底線")
    parser.add_argument("--source-url", help="文件原始網址（記錄用，並用來比對快照證據）")
    parser.add_argument("--title", help="文件標題（記錄用）")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--locate", type=Path, help="快照 dataset.json；檢查證據原文所在頁碼（只回報）")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[A-Za-z0-9_]+", args.doc_id):
        print("[錯誤] --doc-id 只能使用英數與底線", file=sys.stderr)
        return 2
    pdf_path = args.pdf if args.pdf.is_absolute() else PROJECT_ROOT / args.pdf
    try:
        document = parse_document(pdf_path, args.doc_id, source_url=args.source_url, title=args.title)
    except PdfParseError as err:
        print(f"[失敗] {err}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{args.doc_id}.json"
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[成功] {document['page_count']} 頁、{len(document['paragraphs'])} 段 → {output}")
    if document["empty_pages"]:
        print(f"[提示] 第 {', '.join(map(str, document['empty_pages']))} 頁抽不到文字（可能是掃描圖片，需 OCR）")

    if args.locate:
        path = args.locate if args.locate.is_absolute() else PROJECT_ROOT / args.locate
        dataset = json.loads(path.read_text(encoding="utf-8"))
        results = locate_dataset_evidence(document, dataset)
        if not results:
            print("—— 快照中沒有指向這份文件的 pdf 證據 ——")
        for item in results:
            pages = "、".join(map(str, item["found_pages"])) or "—"
            print(f"{item['evidence_id']}：出現於第 {pages} 頁｜{item['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
