"""把 parse_pdf 解析出的條款／DM 段落加入真實快照的 evidence，供向量庫索引。

為什麼放進 evidence：D 組的 validate_retrieved_items() 要求檢索結果的 evidence_id
必須存在於快照，且 excerpt 必須是該證據原文的連續子片段。把每個 PDF 段落登記成
一筆 evidence（原文一字不改），HybridRetriever 不必修改就能索引，引用彈窗也能
直接以 evidence_id 顯示頁碼、條號、網址與版本。

用法（在專案根目錄）：
    python -m tools.add_pdf_evidence data/real/dataset.json --dataset-id real_2026_11_05_v3 `
        --as-of 2026-11-05T15:00:00+08:00 --backup data/real/handoff/dataset.v2_before_pdf_evidence.json

規則：
- evidence_id＝paragraph_id；source_type＝pdf；page、section（條號）、excerpt 皆照抄段落。
- source_url、local_path、captured_at（解析時間）取自解析檔；商品關聯與版本見 JOBS。
- 冪等：先移除同一份文件（<doc_id>_p 開頭）既有的段落證據再重新加入，重跑結果相同。
- 本機 PDF 的 SHA-256 與解析檔不符時拒絕寫入（解析結果過期，需先重跑 parse_pdf）。
- 一律為 unreviewed，不補任何複核紀錄；不修改商品的 evidence_ids。
- 內容有變動時必須換新的 dataset_id；驗證有錯誤時不寫檔。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

from services.data_service import load_schema
from services.validation import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "pdf"

# (解析檔, 關聯商品, 文件版本)；新增 PDF 時在此加一行。版本不明就填 None，不要猜。
JOBS: list[tuple[Path, list[str], str | None]] = [
    (PDF_DIR / "doc_acupl_dm_2026_06.json", ["prd_acupl"], "2026年6月版"),
    (PDF_DIR / "doc_taiwanlife_term_034.json", ["prd_u09r0"], None),
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_source_file(doc: dict[str, Any], root: Path = PROJECT_ROOT) -> None:
    """本機 PDF 仍在時，確認與解析檔記錄的 SHA-256 相同。"""
    source_file = doc.get("source_file")
    if not source_file or not doc.get("sha256"):
        return
    path = root / source_file
    if path.exists() and _sha256(path) != doc["sha256"]:
        raise ValueError(f"{source_file} 與解析檔的 SHA-256 不符，請先重跑 tools.parse_pdf。")


def paragraph_evidence(doc: dict[str, Any], product_ids: list[str], document_version: str | None) -> list[dict[str, Any]]:
    """將一份解析檔的段落轉成 evidence 紀錄；excerpt 為段落原文，不做任何改寫。"""
    doc_id = doc["doc_id"]
    if not doc.get("source_url") and not doc.get("source_file"):
        raise ValueError(f"{doc_id} 缺少 source_url 與 source_file，無法追溯來源。")
    records = []
    for para in doc["paragraphs"]:
        text = para.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if not para["paragraph_id"].startswith(f"{doc_id}_p"):
            raise ValueError(f"段落編號 {para['paragraph_id']} 不屬於 {doc_id}。")
        records.append({
            "evidence_id": para["paragraph_id"],
            "is_mock": False,
            "source_type": "pdf",
            "source_title": doc.get("title") or doc_id,
            "source_url": doc.get("source_url"),
            "local_path": doc.get("source_file"),
            "source_date": None,
            "captured_at": doc.get("parsed_at"),
            "document_version": document_version,
            "page": para["page"],
            "section": para.get("article"),
            "excerpt": text,
            "product_ids": list(product_ids),
            "human_review_status": "unreviewed",
            "reviewed_by": None,
            "reviewed_at": None,
        })
    return records


def merge_pdf_evidence(
    dataset: dict[str, Any],
    jobs: Iterable[tuple[dict[str, Any], list[str], str | None]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """回傳新快照（不修改輸入）與各文件段落數；同一文件的舊段落證據先移除。"""
    result = copy.deepcopy(dataset)
    known_products = {p["product_id"] for p in result.get("products", [])}
    counts: dict[str, int] = {}
    for doc, product_ids, version in jobs:
        missing = set(product_ids) - known_products
        if missing:
            raise ValueError(f"{doc['doc_id']} 關聯的商品不存在於快照：{sorted(missing)}")
        prefix = f"{doc['doc_id']}_p"
        kept = [e for e in result["evidence"] if not e["evidence_id"].startswith(prefix)]
        new = paragraph_evidence(doc, product_ids, version)
        clash = {e["evidence_id"] for e in kept} & {e["evidence_id"] for e in new}
        if clash:
            raise ValueError(f"證據編號與既有資料重複：{sorted(clash)[:5]}")
        result["evidence"] = kept + new
        counts[doc["doc_id"]] = len(new)
    return result, counts


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as file:
        return json.load(file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 PDF 段落加入真實快照的 evidence。")
    parser.add_argument("dataset", type=Path, help="真實快照，例如 data/real/dataset.json")
    parser.add_argument("--dataset-id", required=True, help="新的快照識別，內容變動時必須更換")
    parser.add_argument("--as-of", required=True, help="新的快照基準時間，例如 2026-11-05T15:00:00+08:00")
    parser.add_argument("--backup", type=Path, help="寫入前先備份原快照（檔案已存在時不覆蓋）")
    args = parser.parse_args(argv)

    original = _read(args.dataset)
    jobs = []
    for doc_path, product_ids, version in JOBS:
        doc = _read(doc_path)
        check_source_file(doc)
        jobs.append((doc, product_ids, version))
    try:
        updated, counts = merge_pdf_evidence(original, jobs)
    except ValueError as err:
        print(f"[失敗] {err}", file=sys.stderr)
        return 1

    if updated["evidence"] == original["evidence"]:
        print("PDF 段落證據與快照相同，未寫檔。")
        return 0
    if args.dataset_id == original.get("dataset_id"):
        print("[失敗] 快照內容有變動，必須使用新的 --dataset-id。", file=sys.stderr)
        return 1
    updated["dataset_id"] = args.dataset_id
    updated["as_of"] = args.as_of

    report = validate_dataset(updated, load_schema())
    for item in report["errors"]:
        print(f"[錯誤] {item['code']} {item['path']} {item.get('record_id') or ''}：{item['message']}", file=sys.stderr)
    if report["errors"]:
        print(f"驗證失敗，共 {len(report['errors'])} 項錯誤，未寫檔。", file=sys.stderr)
        return 1

    if args.backup and not args.backup.exists():
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.dataset, args.backup)
        print(f"已備份原快照到 {args.backup}")
    with args.dataset.open("w", encoding="utf-8") as file:
        json.dump(updated, file, ensure_ascii=False, indent=2)
        file.write("\n")
    detail = "、".join(f"{doc_id} {n} 段" for doc_id, n in counts.items())
    print(f"已寫入 {args.dataset}：dataset_id={args.dataset_id}，evidence 共 {len(updated['evidence'])} 筆（{detail}）；"
          f"驗證 0 錯誤、{len(report['warnings'])} 項提示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
