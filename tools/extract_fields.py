"""LLM 結構化抽取：從已解析的條款／DM 段落抽出商品欄位，每個值都附來源段落、頁碼與原文引句。

用法（在專案根目錄，需設定 GEMINI_API_KEY）：
    python -m tools.extract_fields data/real/processed/pdf/doc_taiwanlife_term_034.json --product-id prd_u09r0

輸入是 tools/parse_pdf.py 的輸出；輸出寫到 data/real/processed/extracted/<doc_id>.json。

防止模型捏造的檢查（任何一項不通過，該值就被拒收並列出原因，不會進入結果）：
1. 回應必須是 {"items": [...]}，每筆只能有 field、term_years、value、paragraph_id、quote 五個欄位。
2. field 必須是下方 FIELDS 定義的欄位；paragraph_id 必須存在於這份文件。
3. quote 必須逐字出現在該段落中（只忽略空白與全半形差異）。
4. value 必須能依欄位型別解析，且數字本身必須出現在 quote 裡（例如 0-74 的 0 與 74）。
5. 同一欄位、同一年期出現不同值時，全部標記為 conflict，交給 B 組判斷。

抽取結果一律標記 generation_mode=llm、review_status=unreviewed；本工具不修改 dataset.json。
Prompt 存放於 prompts/extract_fields.txt。

外部套件：google-genai、pydantic（皆為專案既有依賴）。
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
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = PROJECT_ROOT / "prompts" / "extract_fields.txt"
OUTPUT_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "extracted"
DATASET_PATH = PROJECT_ROOT / "data" / "real" / "dataset.json"
TAIPEI = timezone(timedelta(hours=8))
MAX_CONTEXT_CHARS = 60000   # 文件太長時只送含關鍵字的段落

# 欄位定義：型別決定 value 的解析與驗證方式；per_term 表示每個繳費年期各一筆。
FIELDS: dict[str, dict[str, Any]] = {
    "currency": {"type": "text", "per_term": False, "label": "保單計價幣別（例如 美元）"},
    "payment_term_years": {"type": "int", "per_term": False,
                           "label": "可選的繳費年期（年），每個年期各輸出一筆；躉繳為 0"},
    "issue_age_range": {"type": "age_range", "per_term": True,
                        "label": "投保年齡範圍（歲），依繳費年期分別輸出"},
    "min_sum_assured": {"type": "number", "per_term": False,
                        "label": "最低投保金額／最低基本保額；照原文數字填寫，不換算單位"
                                 "（原文「1萬美元」寫 1，原文「6,000美元」寫 6000），單位以 quote 為準"},
    "predetermined_rate_percent": {"type": "number", "per_term": True,
                                   "label": "預定利率或增值回饋分享金計算用的利率（%），若依年期不同請分別輸出"},
    "assumed_declared_rate_percent": {"type": "number", "per_term": False,
                                      "label": "DM 或試算範例中「假設」的宣告利率（%），這是假設值，不是公告利率"},
}
KEYWORDS = ("年期", "躉繳", "歲", "年齡", "幣", "美元", "保額", "投保金額", "利率", "宣告", "假設", "預定")

Generate = Callable[[str], str]


class ExtractionError(RuntimeError):
    """設定、Prompt、模型呼叫或回應格式錯誤。"""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def select_paragraphs(document: dict[str, Any], limit: int = MAX_CONTEXT_CHARS) -> list[dict[str, Any]]:
    paragraphs = document.get("paragraphs") or []
    if sum(len(p["text"]) for p in paragraphs) <= limit:
        return paragraphs
    picked, total = [], 0
    for p in paragraphs:
        if any(k in p["text"] for k in KEYWORDS):
            if total + len(p["text"]) > limit:
                break
            picked.append(p)
            total += len(p["text"])
    return picked


def build_prompt(document: dict[str, Any], product_name: str) -> tuple[str, list[dict[str, Any]]]:
    if not PROMPT_PATH.exists():
        raise ExtractionError(f"找不到 Prompt 檔：{PROMPT_PATH}")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    for marker in ("<<PRODUCT_NAME>>", "<<FIELD_SPEC>>", "<<PARAGRAPHS>>"):
        if marker not in template:
            raise ExtractionError(f"Prompt 缺少 {marker}")
    paragraphs = select_paragraphs(document)
    field_spec = "\n".join(
        f"- {name}（{spec['type']}{'，依年期分別輸出' if spec['per_term'] else ''}）：{spec['label']}"
        for name, spec in FIELDS.items())
    lines = "\n".join(f"[{p['paragraph_id']}]（第 {p['page']} 頁）{p['text']}" for p in paragraphs)
    prompt = (template.replace("<<PRODUCT_NAME>>", product_name)
              .replace("<<FIELD_SPEC>>", field_spec).replace("<<PARAGRAPHS>>", lines))
    return prompt, paragraphs


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def gemini_generate(api_key: str, model: str) -> Generate:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel

    class Item(BaseModel):
        field: str
        term_years: int | None
        value: str
        paragraph_id: str
        quote: str

    class Output(BaseModel):
        items: list[Item]

    client = genai.Client(api_key=api_key)

    def generate(prompt: str) -> str:
        try:
            response = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_schema=Output, temperature=0.0))
        except Exception as err:
            raise ExtractionError(f"Gemini API 呼叫失敗：{err}") from err
        if not response or not response.text:
            raise ExtractionError("Gemini API 未回傳內容。")
        return response.text

    return generate


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------
def _numbers_in(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))


def _parse_value(field: str, raw: Any, quote: str) -> Any:
    """依型別解析 value，並確認數字確實出現在引句中。"""
    kind = FIELDS[field]["type"]
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    quote_numbers = set(_numbers_in(unicodedata.normalize("NFKC", quote)))
    if kind == "text":
        if not text or _normalize(text) not in _normalize(quote):
            raise ValueError("文字值沒有出現在引句中")
        return text
    if kind == "int":
        if not re.fullmatch(r"\d{1,3}", text):
            raise ValueError("不是整數")
        if text not in quote_numbers and not (text == "0" and "躉繳" in quote):
            raise ValueError("數字沒有出現在引句中")
        return int(text)
    if kind == "number":
        text = text.rstrip("%").strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise ValueError("不是數字")
        if text not in quote_numbers and str(float(text)) not in {str(float(n)) for n in quote_numbers}:
            raise ValueError("數字沒有出現在引句中")
        return float(text)
    if kind == "age_range":
        match = re.fullmatch(r"(\d{1,3})\s*[-~～至到]\s*(\d{1,3})", text)
        if not match:
            raise ValueError("年齡範圍格式應為 最小-最大")
        low, high = int(match.group(1)), int(match.group(2))
        if not 0 <= low <= high <= 120:
            raise ValueError("年齡範圍不合理")
        if not {str(low), str(high)} <= quote_numbers:
            raise ValueError("年齡數字沒有出現在引句中")
        return {"min": low, "max": high}
    raise ValueError(f"未知型別 {kind}")


def validate_items(raw_text: str, document: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """回傳 (accepted, rejected)。格式整體錯誤時拋出 ExtractionError。"""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as err:
        raise ExtractionError(f"模型回應不是合法 JSON：{err}") from err
    if not isinstance(data, dict) or set(data) != {"items"} or not isinstance(data["items"], list):
        raise ExtractionError("模型回應必須是只含 items 陣列的物件")

    paragraphs = {p["paragraph_id"]: p for p in document.get("paragraphs") or []}
    expected_keys = {"field", "term_years", "value", "paragraph_id", "quote"}
    accepted, rejected = [], []
    for item in data["items"]:
        def reject(reason):
            rejected.append({"item": item, "reason": reason})
        if not isinstance(item, dict) or set(item) != expected_keys:
            reject("欄位缺漏或包含未定義欄位"); continue
        field, term, pid, quote = item["field"], item["term_years"], item["paragraph_id"], item["quote"]
        if field not in FIELDS:
            reject(f"未定義的欄位 {field!r}"); continue
        if term is not None and (type(term) is not int or not 0 <= term <= 30):
            reject("term_years 必須是 0～30 的整數或 null"); continue
        if FIELDS[field]["type"] == "age_range" and term is None:
            reject("投保年齡需指定 term_years"); continue
        if pid not in paragraphs:
            reject(f"段落編號不存在：{pid!r}"); continue
        if not isinstance(quote, str) or len(_normalize(quote)) < 2:
            reject("引句為空"); continue
        if _normalize(quote) not in _normalize(paragraphs[pid]["text"]):
            reject("引句不是該段落的原文"); continue
        try:
            value = _parse_value(field, item["value"], quote)
        except ValueError as err:
            reject(str(err)); continue
        if field == "payment_term_years" and term not in (None, value):
            reject("payment_term_years 的 term_years 與 value 不一致"); continue
        accepted.append({
            "field": field, "term_years": value if field == "payment_term_years" else term,
            "value": value, "paragraph_id": pid, "page": paragraphs[pid]["page"],
            "article": paragraphs[pid].get("article"), "quote": quote, "status": "ok",
        })

    # 去重與矛盾標記
    unique, seen = [], {}
    for item in accepted:
        key = (item["field"], item["term_years"], json.dumps(item["value"], sort_keys=True))
        if key in seen:
            continue
        seen[key] = item
        unique.append(item)
    groups: dict[tuple, list[dict]] = {}
    for item in unique:
        if item["field"] != "payment_term_years":
            groups.setdefault((item["field"], item["term_years"]), []).append(item)
    for items in groups.values():
        if len(items) > 1:
            for item in items:
                item["status"] = "conflict"
    return unique, rejected


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------
def extract(document: dict[str, Any], product: dict[str, Any], *, generate: Generate,
            model: str) -> dict[str, Any]:
    prompt, used = build_prompt(document, product["product_name"])
    raw = generate(prompt)
    accepted, rejected = validate_items(raw, document)
    return {
        "doc_id": document["doc_id"], "doc_sha256": document.get("sha256"),
        "source_file": document.get("source_file"), "source_url": document.get("source_url"),
        "product_id": product["product_id"], "product_name": product["product_name"],
        "generation_mode": "llm", "review_status": "unreviewed", "model": model,
        "prompt_sha256": hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "paragraphs_sent": len(used), "paragraphs_total": len(document.get("paragraphs") or []),
        "items": accepted, "rejected": rejected, "raw_response": raw,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="以 Gemini 從條款段落抽取欄位（每個值附來源段落與頁碼）")
    parser.add_argument("document", type=Path, help="tools/parse_pdf.py 輸出的 JSON")
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH, help="用來查商品名稱的快照")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    resolve = lambda p: p if p.is_absolute() else PROJECT_ROOT / p
    document = json.loads(resolve(args.document).read_text(encoding="utf-8"))
    dataset = json.loads(resolve(args.dataset).read_text(encoding="utf-8"))
    product = next((p for p in dataset["products"] if p["product_id"] == args.product_id), None)
    if product is None:
        print(f"[錯誤] 快照中沒有商品 {args.product_id}", file=sys.stderr)
        return 2

    from services.llm_service import get_gemini_config  # 讀 .env 與 Streamlit Secrets
    config = get_gemini_config()
    api_key = config["api_key"]
    if not api_key or api_key == "your_gemini_api_key_here":
        print("[失敗] 尚未設定 GEMINI_API_KEY，沒有執行抽取。", file=sys.stderr)
        return 1
    try:
        result = extract(document, product, generate=gemini_generate(api_key, config["model"]),
                         model=config["model"])
    except ExtractionError as err:
        print(f"[失敗] {err}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{document['doc_id']}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[完成] 採用 {len(result['items'])} 筆、拒收 {len(result['rejected'])} 筆 → {output}")
    for item in result["items"]:
        term = "" if item["term_years"] is None else f"（{item['term_years']} 年期）"
        flag = "  ⚠ 矛盾" if item["status"] == "conflict" else ""
        print(f"  {item['field']}{term} = {item['value']}｜第 {item['page']} 頁 {item['paragraph_id']}{flag}")
    for item in result["rejected"]:
        print(f"  [拒收] {item['reason']}：{json.dumps(item['item'], ensure_ascii=False)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
