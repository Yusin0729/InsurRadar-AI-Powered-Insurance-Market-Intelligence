"""InsurRadar 檢索服務抽象介面與 Mock 檢索實作。

主要用途與功能：
1. 定義 BaseRetriever 抽象基底介面，規範 search(query, top_k) 輸入與輸出契約，供未來 C 組向量檢索無縫抽換。
2. 實作 MockRetriever 雛型：支援中文子字串與詞彙重疊匹配，不依賴外部向量庫或大型斷詞套件。
3. 嚴格將檢索結果格式化為符合資料契約之標準依據結構，供 RAG 系統精確引用與開窗。

外部套件：
- 本模組僅使用 Python 標準庫（abc, re, typing），無第三方套件依賴。
"""

from abc import ABC, abstractmethod
import re
from typing import Any


class BaseRetriever(ABC):
    """檢索服務抽象基底類別（供未來 C 組向量資料庫檢索實作繼承）。"""

    @abstractmethod
    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """根據查詢字串檢索相關依據片段。

        參數：
            query (str): 使用者輸入的問題或查詢字串。
            top_k (int): 最多回傳之依據數量，預設為 3。

        回傳：
            list[dict]: 檢索到的候選依據清單，每項必須包含：
                - evidence_id (str): 依據識別碼（存在於資料快照 evidence 中）。
                - source_title (str): 依據文件標題。
                - document_version (str | None): 版本代號。
                - page (int | None): 頁碼。
                - section (str | None): 章節。
                - excerpt (str): 依據精華原文。
                - score (float): 相關度分數（0.0 ~ 1.0）。
                - source_type (str): 來源類型（"pdf" | "official_page" | "news" | "mock"）。
                - source_url (str | None): 外部來源連結。
                - product_ids (list[str]): 關聯商品 ID。
        """
        pass


class MockRetriever(BaseRetriever):
    """基於中文關鍵字與子字串比對的 Mock 檢索器。

    說明：本實作專供 E2 測試檢索流程，不具備語意向量或語意嵌入能力。
    未來 C 組完成向量檢索後，可直接實作 BaseRetriever 並替換本類別。
    """

    def __init__(self, dataset: dict[str, Any]):
        self.dataset = dataset
        self.evidence_list = dataset.get("evidence", [])
        # 建立商品與公司名稱對照，輔助檢索匹配
        self.product_lookup = {p["product_id"]: p for p in dataset.get("products", [])}
        self.company_names = {p["company_name"] for p in dataset.get("products", [])}

    STOP_WORDS = {"之", "的", "與", "在", "或", "及", "等", "了", "有", "是", "為", "個", "一", "可"}

    def _extract_query_tokens(self, query: str) -> set[str]:
        """自查詢字串提取單詞與 2~4 字的中文子字串，過濾常見單字虛詞。"""
        cleaned = query.strip().lower()
        tokens = set()

        # 1. 依空白切分之一般詞彙與數字（如 3.1%、mock_evidence_001）
        for part in re.split(r"\s+", cleaned):
            if part and part not in self.STOP_WORDS:
                tokens.add(part)

        # 2. 提取連續中文字串並產生 2~4 字的滑動窗口子字串
        chinese_blocks = re.findall(r"[\u4e00-\u9fa5]+", cleaned)
        for block in chinese_blocks:
            if len(block) >= 2:
                tokens.add(block)
                # 產生 2 字與 3 字元 ngram
                for i in range(len(block) - 1):
                    sub2 = block[i : i + 2]
                    if sub2 not in self.STOP_WORDS:
                        tokens.add(sub2)
                    if i + 3 <= len(block):
                        sub3 = block[i : i + 3]
                        tokens.add(sub3)
            elif len(block) == 1 and len(cleaned) == 1:
                # 僅在整個查詢僅有 1 個中文字時才加入，避免在長句中誤殺
                if block not in self.STOP_WORDS:
                    tokens.add(block)

        return tokens

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """執行關鍵字與子字串權重檢索，回傳按相關度排序之結果。"""
        if type(top_k) is not int or top_k < 1:
            raise ValueError("top_k 必須為正整數。")
        if not query or not query.strip() or not self.evidence_list:
            return []

        query_tokens = self._extract_query_tokens(query)
        if not query_tokens:
            return []

        raw_query_lower = query.strip().lower()
        scored_results = []

        for item in self.evidence_list:
            if not isinstance(item.get("excerpt"), str) or not item["excerpt"].strip():
                continue
            eid = item["evidence_id"]
            title = (item.get("source_title") or "").lower()
            excerpt = (item.get("excerpt") or "").lower()
            section = (item.get("section") or "").lower()
            pids = item.get("product_ids") or []

            # 關聯商品與公司文字
            assoc_texts = []
            for pid in pids:
                prod = self.product_lookup.get(pid)
                if prod:
                    assoc_texts.append(prod.get("company_name", "").lower())
                    assoc_texts.append(prod.get("product_name", "").lower())
            searchable_blob = f"{title} {excerpt} {section} {' '.join(assoc_texts)}"

            # 計算匹配分數
            score = 0.0
            # 若原始查詢整串出現於原文或標題，給予高分權重
            if raw_query_lower in searchable_blob:
                score += 0.5

            # 統計命中之 token 比例
            matched_count = 0
            for token in query_tokens:
                if token in searchable_blob:
                    # 長詞命中權重較高
                    weight = 0.2 if len(token) >= 3 else 0.1
                    score += weight
                    matched_count += 1

            if score >= 0.15:
                normalized_score = min(round(score, 3), 1.0)
                scored_results.append({
                    "evidence_id": eid,
                    "source_title": item.get("source_title", "未命名依據"),
                    "document_version": item.get("document_version"),
                    "page": item.get("page"),
                    "section": item.get("section"),
                    "excerpt": item.get("excerpt", ""),
                    "score": normalized_score,
                    "source_type": item.get("source_type", "mock"),
                    "source_url": item.get("source_url"),
                    "product_ids": pids,
                })

        # 按分數高至低排序，同分者依 evidence_id 排序保證穩定
        scored_results.sort(key=lambda x: (x["score"], x["evidence_id"]), reverse=True)
        return scored_results[:top_k]
