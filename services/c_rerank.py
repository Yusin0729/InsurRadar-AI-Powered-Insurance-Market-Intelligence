"""C 組檢索排序調整：關鍵字計分＋依問題內容加權，供 services/c_retriever.py 使用。

D 的 MockRetriever 把分數上限設為 1.0，證據一多就大量同分，排序幾乎隨機；
加入 PDF 條款段落後，關鍵字備援命中率從 7/10 降到 4/10。本模組在不修改 D 檔案的前提下：

1. 關鍵字計分不設上限，並依原文長度折減，避免長段落靠字數累積分數，最後再正規化到 0～1。
2. 依商品加權：問題提到商品簡稱、代碼或公司（如 超美利、ACUPL、富邦），該商品的證據優先。
3. 日期換算：西元與民國年月互通（2026年8月 ⇄ 115/8），問題指定月份時，該月的宣告利率優先。
4. 宣告利率去重：問題沒指定月份時，同一商品只保留最新一期，避免 12 筆月份利率擠滿前幾名；
   問題不是在問宣告利率（如問預定利率、投保年齡）時，月份利率整體降權。
5. 依問題類型加權：投保年齡、最低保額、預定利率、DM 假設利率、解約金、費率等，原文相符者優先。
6. 人工整理的證據（B 組 E 系列、官網利率）略優先於自動切出的 PDF 段落。
7. 跨商品問題（哪些、各家、比較…且沒有指定單一商品）時，前幾名輪流放入不同商品的最佳證據，
   避免回答只涵蓋其中兩家。

所有調整只改變排序與分數（仍在 0～1），不改寫任何原文。
外部套件：無。
"""

from __future__ import annotations

import re
from typing import Any

from services.retriever import MockRetriever

# 加權係數：1.0 為不調整，數值越小越後面
OTHER_PRODUCT = 0.4       # 問題指定商品，但證據屬於其他商品
NO_PRODUCT = 0.8          # 問題指定商品，但證據沒有關聯商品
OTHER_RATE_MONTH = 0.4    # 非目標月份（或非最新一期）的宣告利率
RATE_NOT_ASKED = 0.5      # 問題不是在問宣告利率時的月份利率
INTENT_MISS = 0.7         # 問題有明確類型，但原文沒有對應字詞
PDF_PARAGRAPH = 0.85      # 自動切出的 PDF 段落
LENGTH_SCALE = 400        # 關鍵字分數依原文長度折減：raw / (1 + 長度 / LENGTH_SCALE)
MIN_RAW_KEYWORD = 0.15    # 與 MockRetriever 相同的最低門檻

# 問題類型：(問題中的字詞, 原文需含的字詞)
INTENTS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("假設", "試算"), ("假設",)),
    (("預定利率",), ("預定利率",)),
    (("投保年齡", "幾歲", "年齡"), ("投保年齡", "歲")),
    (("最低保額", "最低投保", "最低"), ("最低",)),
    (("繳費年期", "年期"), ("年期",)),
    (("解約金", "現金價值"), ("解約金", "現金價值")),
    (("費率", "保費"), ("費率", "保費", "保險費")),
    (("基本保險金額", "基本保額"), ("基本保險金額", "基本保額")),
]
MULTI_PRODUCT_WORDS = ("哪些", "各家", "各公司", "每家", "每一家", "比較", "所有", "分別")
_PDF_PARAGRAPH_ID = re.compile(r"^doc_.+_p\d{3}_\d{3}$")
_CODE = re.compile(r"[A-Z][A-Z0-9]{2,}")
_NAME_STOP = ("美元", "外幣", "利率", "終身", "（", "(")


# ---------------------------------------------------------------------------
# 問題分析
# ---------------------------------------------------------------------------
def product_aliases(dataset: dict[str, Any]) -> dict[str, set[str]]:
    """從快照的商品名稱推出可辨識的別名：代碼、公司名、商品簡稱。"""
    aliases: dict[str, set[str]] = {}
    for product in dataset.get("products", []):
        name = product.get("product_name") or ""
        company = product.get("company_name") or ""
        names = set(_CODE.findall(name))
        short_company = company.replace("人壽", "")
        names.update(n for n in (company, short_company) if len(n) >= 2)
        rest = name
        for prefix in (company, short_company):
            if prefix and rest.startswith(prefix):
                rest = rest[len(prefix):]
        cut = min([rest.find(s) for s in _NAME_STOP if s in rest] or [len(rest)])
        if cut >= 2:
            names.add(rest[:cut])
        aliases[product["product_id"]] = {n.lower() for n in names}
    return aliases


def target_month(query: str) -> str | None:
    """問題中的年月 → YYYY-MM；支援 2026年8月、2026/8、民國115年8月、115/8。"""
    m = re.search(r"(\d{2,4})\s*[年/\-.]\s*(\d{1,2})\s*月?", query)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    if year < 1000:            # 民國年
        year += 1911
    if not 2000 <= year <= 2100:
        return None
    return f"{year:04d}-{month:02d}"


def month_variants(month: str) -> list[str]:
    """YYYY-MM → 原文可能的寫法（官網有的用民國 115/8，有的用 2026/08）。"""
    year, mon = (int(x) for x in month.split("-"))
    roc = year - 1911
    return [f"{roc}/{mon}", f"{roc}年{mon}月", f"{year}/{mon:02d}", f"{year}年{mon}月"]


class QueryProfile:
    def __init__(self, query: str, aliases: dict[str, set[str]]):
        lowered = query.lower()
        self.products = {pid for pid, names in aliases.items() if any(n in lowered for n in names)}
        self.month = target_month(query)
        self.asks_rate = "宣告利率" in query and not any(w in query for w in ("假設", "試算", "DM", "dm"))
        self.intents = [needles for words, needles in INTENTS if any(w in query for w in words)]
        self.multi_product = len(self.products) != 1 and any(w in query for w in MULTI_PRODUCT_WORDS)
        extra = month_variants(self.month) if self.month else []
        self.keyword_query = " ".join([query.strip(), *extra])


# ---------------------------------------------------------------------------
# 排序調整
# ---------------------------------------------------------------------------
class Reranker:
    def __init__(self, dataset: dict[str, Any], evidence: dict[str, dict[str, Any]]):
        self.evidence = evidence
        self.aliases = product_aliases(dataset)
        self.tokenizer = MockRetriever(dataset)  # 只借用 D 的斷詞，不使用其分數
        products = {p["product_id"]: p for p in dataset.get("products", [])}
        self.blobs = {}
        for eid, ev in evidence.items():
            assoc = " ".join(f"{products[p].get('company_name', '')} {products[p].get('product_name', '')}"
                             for p in ev.get("product_ids") or [] if p in products)
            self.blobs[eid] = f"{ev.get('source_title') or ''} {ev['excerpt']} {ev.get('section') or ''} {assoc}".lower()

        # 宣告利率：evidence_id → (商品, 月份)；每個商品的最新月份
        self.rate_month: dict[str, tuple[str, str]] = {}
        for rate in dataset.get("rates", []):
            month, pid = rate.get("period_month"), rate.get("product_id")
            if rate.get("rate_type") != "declared" or not month or not pid:
                continue
            for eid in rate.get("evidence_ids") or []:
                if eid in evidence:
                    self.rate_month[eid] = (pid, month)
        self.latest: dict[str, str] = {}
        for pid, month in self.rate_month.values():
            self.latest[pid] = max(self.latest.get(pid, ""), month)

    def profile(self, query: str) -> QueryProfile:
        return QueryProfile(query, self.aliases)

    def keyword_scores(self, profile: QueryProfile) -> dict[str, float]:
        """不設上限的關鍵字分數，依長度折減後正規化到 0～1；低於門檻者不列入。"""
        query = profile.keyword_query.lower()
        tokens = self.tokenizer._extract_query_tokens(query)
        if not tokens:
            return {}
        raw: dict[str, float] = {}
        for eid, blob in self.blobs.items():
            score = 0.5 if query in blob else 0.0
            score += sum((0.2 if len(t) >= 3 else 0.1) for t in tokens if t in blob)
            if score >= MIN_RAW_KEYWORD:
                raw[eid] = score / (1 + len(self.evidence[eid]["excerpt"]) / LENGTH_SCALE)
        top = max(raw.values(), default=0.0)
        return {eid: s / top for eid, s in raw.items()} if top > 0 else {}

    def factor(self, eid: str, profile: QueryProfile) -> float:
        ev = self.evidence[eid]
        factor = 1.0
        pids = set(ev.get("product_ids") or [])
        if profile.products:
            if not pids:
                factor *= NO_PRODUCT
            elif not pids & profile.products:
                factor *= OTHER_PRODUCT

        if eid in self.rate_month:
            pid, month = self.rate_month[eid]
            wanted = profile.month or self.latest.get(pid)
            if month != wanted:
                factor *= OTHER_RATE_MONTH
            if not profile.asks_rate:
                factor *= RATE_NOT_ASKED

        if profile.intents:
            text = f"{ev['excerpt']} {ev.get('section') or ''}"
            if not any(any(n in text for n in needles) for needles in profile.intents):
                factor *= INTENT_MISS

        if _PDF_PARAGRAPH_ID.match(eid) and ev.get("source_type") == "pdf":
            factor *= PDF_PARAGRAPH
        return factor

    def diversify(self, ranked: list[str], profile: QueryProfile, top_k: int) -> list[str]:
        """跨商品問題：先依序放入各商品的最高分證據，再依原排序補滿；其他問題原樣回傳。"""
        if not profile.multi_product or top_k < 2:
            return ranked[:top_k]
        wanted = profile.products or set(self.aliases)
        firsts, seen = [], set()
        for eid in ranked:
            pids = set(self.evidence[eid].get("product_ids") or []) & wanted
            if pids and not pids & seen:
                firsts.append(eid)
                seen |= pids
        picked = firsts[:top_k]
        picked += [eid for eid in ranked if eid not in picked][: top_k - len(picked)]
        order = {eid: i for i, eid in enumerate(ranked)}
        return sorted(picked, key=order.get)
