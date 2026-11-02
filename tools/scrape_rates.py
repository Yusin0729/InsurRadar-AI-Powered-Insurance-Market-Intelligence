"""抓取各家保險公司官網的宣告利率公告，保存原始回應快照並整理成結構化資料。

用法（在專案根目錄）：
    python -m tools.scrape_rates                          # 抓全部目標商品
    python -m tools.scrape_rates --products prd_fbo       # 只抓指定商品
    python -m tools.scrape_rates --compare data/real/dataset.json   # 另外列出與快照的差異

三家官網的宣告利率頁都是前端動態載入，頁面 HTML 裡沒有數字，
因此直接呼叫頁面背後使用的公開查詢 API（回傳 JSON），不需要 BeautifulSoup 或 Playwright。

輸出：
- data/real/raw/rates/<時間戳>/：每次抓取的原始回應（原樣位元組）與 manifest.json（網址、參數、狀態碼、SHA-256、時間）。
- data/real/processed/declared_rates.json、declared_rates.csv：整理後的逐月宣告利率。
某商品抓取失敗時，不寫入空資料，沿用上一次成功的紀錄並在輸出中標示（抓不到就退回快照）。

本工具不修改 data/real/dataset.json；抓到的數值要經 B 組校對後才放進快照。
宣告利率非保證，可能調整。

外部套件：無（僅用 Python 標準函式庫）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "real" / "raw" / "rates"
PROCESSED_DIR = PROJECT_ROOT / "data" / "real" / "processed"
TAIPEI = timezone(timedelta(hours=8))

USER_AGENT = "InsurRadar-hackathon-research/1.0 (+public declared-rate lookup)"
TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 0.5   # 每次請求之間稍作間隔，避免造成對方負擔
MAX_PAGES = 20                # 分頁上限，防止 API 異常時無限迴圈

# 目標商品：product_id 對應 data/real/dataset.json；比對名稱用來確認抓到的是同一張保單。
TARGETS: list[dict[str, str]] = [
    {"product_id": "prd_acupl", "insurer": "nanshan", "code": "ACUPL",
     "name": "南山人壽美滿臻愛美元利率變動型終身保險（定期給付型）"},
    {"product_id": "prd_u09r0", "insurer": "taiwanlife", "code": "U09R0",
     "name": "台灣人壽超美利美元利率變動型終身壽險"},
    {"product_id": "prd_fbo", "insurer": "fubon", "code": "FBO",
     "name": "富邦人壽美利大運外幣利率變動型終身壽險"},
]

# fetch(method, url, body, headers) -> (status, content)；測試時可替換。
Fetch = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


class ScrapeError(RuntimeError):
    """抓取或解析失敗；訊息會寫進輸出摘要。"""


# ---------------------------------------------------------------------------
# HTTP 與原始快照
# ---------------------------------------------------------------------------
def urllib_fetch(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ScrapeError(f"連線失敗：{url}（{err}）") from err


class SnapshotRecorder:
    """每次請求的原始回應原樣存檔，並記錄於 manifest。"""

    def __init__(self, folder: Path, fetch: Fetch, delay: float = REQUEST_DELAY_SECONDS):
        self.folder = folder
        self.fetch = fetch
        self.delay = delay
        self.entries: list[dict[str, Any]] = []

    def request_json(self, label: str, method: str, url: str, *,
                     payload: dict | None = None, headers: dict[str, str] | None = None) -> tuple[Any, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        if self.entries and self.delay:
            time.sleep(self.delay)
        captured_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
        status, content = self.fetch(method, url, body, dict(headers or {}))
        raw_name = f"{len(self.entries) + 1:03d}_{label}.json"
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / raw_name).write_bytes(content)
        self.entries.append({
            "file": raw_name, "method": method, "url": url, "payload": payload,
            "status": status, "captured_at": captured_at,
            "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
        })
        if status != 200:
            raise ScrapeError(f"{url} 回應 HTTP {status}")
        try:
            return json.loads(content.decode("utf-8-sig")), raw_name
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ScrapeError(f"{url} 回應不是 JSON：{err}") from err

    def write_manifest(self, extra: dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "manifest.json").write_text(
            json.dumps({**extra, "requests": self.entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")


# ---------------------------------------------------------------------------
# 欄位正規化
# ---------------------------------------------------------------------------
def to_period_month(text: str) -> str:
    """'115/11'（民國）或 '2026/11' → '2026-11'。"""
    match = re.fullmatch(r"\s*(\d{2,4})\s*[/\-.年]\s*(\d{1,2})\s*月?\s*", str(text))
    if not match:
        raise ScrapeError(f"無法辨識的宣告年月：{text!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if year < 1911:
        year += 1911
    if not 1 <= month <= 12:
        raise ScrapeError(f"月份超出範圍：{text!r}")
    return f"{year:04d}-{month:02d}"


def to_rate_percent(text: Any) -> float:
    """'4.35%' 或 '4.20' → 4.35；拒絕空值、非數字與不合理範圍，不做任何補值。"""
    cleaned = str(text).strip().rstrip("%").strip()
    try:
        value = float(cleaned)
    except ValueError as err:
        raise ScrapeError(f"無法辨識的宣告利率：{text!r}") from err
    if not math.isfinite(value) or not 0 < value < 20:
        raise ScrapeError(f"宣告利率數值不合理：{text!r}")
    return value


def make_record(target, *, raw_date, raw_rate, source_name, source_url, api_url, raw_file, captured_at):
    return {
        "product_id": target["product_id"], "product_code": target["code"],
        "source_product_name": source_name,
        "period_month": to_period_month(raw_date), "rate_percent": to_rate_percent(raw_rate),
        "raw_date_text": str(raw_date), "raw_rate_text": str(raw_rate),
        "source_url": source_url, "api_url": api_url,
        "raw_file": raw_file, "captured_at": captured_at,
    }


# ---------------------------------------------------------------------------
# 各家抓取邏輯
# ---------------------------------------------------------------------------
FUBON_PAGE = "https://www.fubon.com/life/product/quick/interest-rate/rate-change"
FUBON_API = "https://www.fubon.com/life/api/v1/node/search-rates"
FUBON_NODE_ID = 470            # 「利變壽險宣告利率」分頁（頁面內嵌資料 nodeId）
FUBON_TEMPLATE = "D3-1-G"


def scrape_fubon(target, rec: SnapshotRecorder) -> list[dict]:
    records, page, total_pages = [], 0, 1
    while page < min(total_pages, MAX_PAGES):
        payload = {"nodeId": FUBON_NODE_ID, "productTemplate": FUBON_TEMPLATE, "page": page, "size": 10,
                   "itemName": None, "item": target["code"],
                   "startYear": None, "startMonth": None, "endYear": None, "endMonth": None}
        data, raw = rec.request_json(f"fubon_{target['code']}_p{page}", "POST", FUBON_API,
                                     payload=payload, headers={"Content-Type": "application/json"})
        if not data.get("success") or not isinstance(data.get("data"), dict):
            raise ScrapeError(f"富邦 API 回應失敗：returnCode={data.get('returnCode')}")
        body = data["data"]
        total_pages = int(body.get("totalPages") or 0)
        for row in body.get("rateList") or []:
            if row.get("item") != target["code"]:
                continue  # 只採計險種代碼完全相符的列
            records.append(make_record(
                target, raw_date=row.get("date"), raw_rate=row.get("rate"),
                source_name=row.get("itemName"), source_url=FUBON_PAGE, api_url=FUBON_API,
                raw_file=raw, captured_at=rec.entries[-1]["captured_at"]))
        page += 1
    return records


NANSHAN_PAGE = "https://www.nanshanlife.com.tw/nanshanlife/service-rate-1"
NANSHAN_API = "https://www.nanshanlife.com.tw/nanshanlife/portal-api/DeclaredRate"


def _portal_headers() -> dict[str, str]:
    # 南山與台灣人壽（同一套網站框架）的 API 要求帶 Content-Type 與這兩個標頭，否則回 error_code 800。
    return {"Content-Type": "application/json", "X-Date": formatdate(usegmt=True),
            "X-Request-ID": str(int(time.time() * 1000))}


def scrape_nanshan(target, rec: SnapshotRecorder) -> list[dict]:
    data, _ = rec.request_json("nanshan_products", "GET", f"{NANSHAN_API}/getTypesProducts",
                               headers=_portal_headers())
    matches = {(t["type_id"], o["id"]) for t in data.get("datas") or [] if t.get("type_id") != "-1"
               for o in t.get("option_list") or [] if o.get("name") == target["name"]}
    if len(matches) != 1:
        raise ScrapeError(f"南山商品清單中找到 {len(matches)} 筆「{target['name']}」，無法確認是哪一張")
    product_type, product_id = matches.pop()

    records, page, total_pages = [], 1, 1
    while page <= min(total_pages, MAX_PAGES):
        query = urllib.parse.urlencode({"productId": product_id, "productType": product_type,
                                        "page": page, "year": "-1", "month": "-1"})
        url = f"{NANSHAN_API}/List?{query}"
        data, raw = rec.request_json(f"nanshan_{target['code']}_p{page}", "GET", url,
                                     headers=_portal_headers())
        if (data.get("header") or {}).get("code") != "0":
            raise ScrapeError(f"南山 API 回應失敗：{data.get('header')}")
        total_pages = int((data.get("page_Info") or {}).get("total_page_size") or 0)
        for row in data.get("datas") or []:
            if f"【{target['code']}】" not in (row.get("name") or ""):
                continue  # 名稱需帶有相同商品代碼
            records.append(make_record(
                target, raw_date=row.get("date"), raw_rate=row.get("rate"),
                source_name=row.get("name"), source_url=NANSHAN_PAGE, api_url=url,
                raw_file=raw, captured_at=rec.entries[-1]["captured_at"]))
        page += 1
    return records


TAIWANLIFE_PAGE = "https://www.taiwanlife.com/investment-rate-interest-rate"
TAIWANLIFE_API = "https://www.taiwanlife.com/portal-api"
TAIWANLIFE_RATE_TYPE = "A"     # 頁面設定 Content/109 的 source


def scrape_taiwanlife(target, rec: SnapshotRecorder) -> list[dict]:
    data, _ = rec.request_json("taiwanlife_products", "GET",
                               f"{TAIWANLIFE_API}/Rate/Product/{TAIWANLIFE_RATE_TYPE}",
                               headers=_portal_headers())
    serials = {row["item_serno"] for row in data.get("datas") or [] if row.get("item_name") == target["name"]}
    if len(serials) != 1:
        raise ScrapeError(f"台灣人壽商品清單中找到 {len(serials)} 筆「{target['name']}」，無法確認是哪一張")
    serial = serials.pop()

    records, page, total_pages = [], 1, 1
    url = f"{TAIWANLIFE_API}/Rate"
    while page <= min(total_pages, MAX_PAGES):
        payload = {"page_no": page, "rate_type": TAIWANLIFE_RATE_TYPE, "item_serno": serial}
        data, raw = rec.request_json(f"taiwanlife_{target['code']}_p{page}", "POST", url,
                                     payload=payload, headers=_portal_headers())
        if (data.get("header") or {}).get("code") != "0":
            raise ScrapeError(f"台灣人壽 API 回應失敗：{data.get('header')}")
        total_pages = int((data.get("page_info") or {}).get("total_page_size") or 0)
        for row in data.get("datas") or []:
            if row.get("item_name") != target["name"]:
                continue
            records.append(make_record(
                target, raw_date=row.get("date"), raw_rate=row.get("rate"),
                source_name=row.get("item_name"), source_url=TAIWANLIFE_PAGE, api_url=url,
                raw_file=raw, captured_at=rec.entries[-1]["captured_at"]))
        page += 1
    return records


SCRAPERS = {"fubon": scrape_fubon, "nanshan": scrape_nanshan, "taiwanlife": scrape_taiwanlife}


# ---------------------------------------------------------------------------
# 執行流程
# ---------------------------------------------------------------------------
def dedupe(records: list[dict]) -> list[dict]:
    """同商品同月份只保留一筆；若同月份出現兩個不同數值，視為來源異常。"""
    by_key: dict[tuple[str, str], dict] = {}
    for item in records:
        key = (item["product_id"], item["period_month"])
        if key in by_key and by_key[key]["rate_percent"] != item["rate_percent"]:
            raise ScrapeError(f"{key[0]} {key[1]} 出現兩個不同的宣告利率："
                              f"{by_key[key]['rate_percent']} 與 {item['rate_percent']}")
        by_key.setdefault(key, item)
    return sorted(by_key.values(), key=lambda r: (r["product_id"], r["period_month"]), reverse=True)


def load_previous(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"records": []}


def run(targets: list[dict], *, fetch: Fetch = urllib_fetch, raw_dir: Path = RAW_DIR,
        processed_dir: Path = PROCESSED_DIR, delay: float = REQUEST_DELAY_SECONDS) -> dict[str, Any]:
    base_id = run_id = datetime.now(TAIPEI).strftime("%Y%m%dT%H%M%S")
    suffix = 1
    while (raw_dir / run_id).exists():  # 同一秒內重跑時不覆寫前一次的原始快照
        suffix += 1
        run_id = f"{base_id}_{suffix}"
    recorder = SnapshotRecorder(raw_dir / run_id, fetch, delay)
    output_path = processed_dir / "declared_rates.json"
    previous = load_previous(output_path)

    records, status = [], {}
    for target in targets:
        try:
            found = dedupe(SCRAPERS[target["insurer"]](target, recorder))
            if not found:
                raise ScrapeError("API 回應中沒有這張商品的宣告利率")
            records += found
            status[target["product_id"]] = {"ok": True, "count": len(found),
                                             "latest": found[0]["period_month"]}
        except ScrapeError as err:
            # 抓不到就沿用上一次成功的紀錄，不寫入空值
            kept = [r for r in previous.get("records", []) if r["product_id"] == target["product_id"]]
            records += kept
            status[target["product_id"]] = {"ok": False, "error": str(err), "kept_previous": len(kept)}

    result = {"run_id": run_id, "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
              "raw_snapshot_dir": str((raw_dir / run_id).relative_to(PROJECT_ROOT))
              if (raw_dir / run_id).is_relative_to(PROJECT_ROOT) else str(raw_dir / run_id),
              "notice": "宣告利率非保證，可能調整；數值須經 B 組校對後才放入快照。",
              "status": status, "records": records}
    recorder.write_manifest({"run_id": run_id, "status": status})
    processed_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (processed_dir / "declared_rates.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["product_id", "product_code", "period_month", "rate_percent", "source_product_name",
                  "raw_date_text", "raw_rate_text", "source_url", "api_url", "captured_at", "raw_file"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return result


def compare_with_dataset(result: dict, dataset: dict) -> list[str]:
    """列出抓到的最新利率與快照現有利率的差異；只提示，不改快照。"""
    lines = []
    existing = {(r["product_id"], r["period_month"]): r["rate_percent"] for r in dataset.get("rates", [])}
    for product_id, info in result["status"].items():
        mine = [r for r in result["records"] if r["product_id"] == product_id]
        if not mine:
            lines.append(f"{product_id}：沒有可比較的抓取結果")
            continue
        latest = mine[0]
        snap = sorted((m, v) for (p, m), v in existing.items() if p == product_id)
        snap_text = "、".join(f"{m} {v}%" for m, v in snap) if snap else "無宣告利率紀錄"
        lines.append(f"{product_id}：官網最新 {latest['period_month']} {latest['rate_percent']}%；快照：{snap_text}")
        for record in mine:
            old = existing.get((product_id, record["period_month"]))
            if old is not None and old != record["rate_percent"]:
                lines.append(f"  ⚠ {record['period_month']} 快照 {old}% ≠ 官網 {record['rate_percent']}%")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抓取官網宣告利率並保存原始快照")
    parser.add_argument("--products", help="只抓這些 product_id，以逗號分隔")
    parser.add_argument("--compare", type=Path, help="與指定快照 dataset.json 比較（只列差異，不修改）")
    args = parser.parse_args(argv)

    targets = TARGETS
    if args.products:
        wanted = {p.strip() for p in args.products.split(",") if p.strip()}
        unknown = wanted - {t["product_id"] for t in TARGETS}
        if unknown:
            print(f"[錯誤] 未設定的商品：{', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        targets = [t for t in TARGETS if t["product_id"] in wanted]

    result = run(targets)
    for product_id, info in result["status"].items():
        if info["ok"]:
            print(f"[成功] {product_id}：{info['count']} 筆，最新 {info['latest']}")
        else:
            print(f"[失敗] {product_id}：{info['error']}（沿用上次紀錄 {info['kept_previous']} 筆）",
                  file=sys.stderr)
    print(f"原始快照：{result['raw_snapshot_dir']}")
    print(f"整理結果：{PROCESSED_DIR / 'declared_rates.json'}")

    if args.compare:
        path = args.compare if args.compare.is_absolute() else PROJECT_ROOT / args.compare
        dataset = json.loads(path.read_text(encoding="utf-8"))
        print("—— 與快照比較（宣告利率非保證，可能調整）——")
        for line in compare_with_dataset(result, dataset):
            print(line)
    return 0 if all(info["ok"] for info in result["status"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
