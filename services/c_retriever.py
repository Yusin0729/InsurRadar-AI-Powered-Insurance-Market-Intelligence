"""InsurRadar C 組檢索服務：Gemini 向量檢索＋關鍵字混合，依 RETRIEVER_CONTRACT 實作 BaseRetriever。

設計重點：
1. 只索引目前快照 evidence 的 excerpt；長原文切成「原文的連續子字串」片段，
   因此回傳的 excerpt 一定能通過 rag_service.validate_retrieved_items 的子片段檢查。
   條款／DM 的 PDF 段落由 tools/add_pdf_evidence.py 先登記為 evidence，再一併索引。
2. 來源欄位（標題、網址、頁碼、版本、商品關聯…）一律從快照原樣複製，不自行改寫。
3. 文件向量依 dataset_id＋片段內容快取於 data/real/index/，Demo 現場只需嵌入使用者問題。
4. 沒有 API Key、離線（INSURRADAR_OFFLINE=1）或嵌入失敗時，自動退回關鍵字比對，
   不中斷問答；last_mode 記錄本次實際使用的模式，供 UI 顯示。
5. 關鍵字計分與排序調整（商品、月份、問題類型加權）在 services/c_rerank.py，
   向量與關鍵字兩種模式都套用。

外部套件：numpy（已在 requirements.txt 宣告）、google-genai（專案既有）。
前端透過 services/c_integration.py 取得本檢索器，不直接建立。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from services.c_rerank import Reranker
from services.retriever import BaseRetriever, MockRetriever

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_DIR = PROJECT_ROOT / "data" / "real" / "index"
DEFAULT_EMBED_MODEL = "gemini-embedding-001"

CHUNK_SIZE = 220      # 每段最多字元數（繁中約 1～2 個條款句）
CHUNK_OVERLAP = 60    # 相鄰片段重疊，避免關鍵句被切斷
EMBED_BATCH = 50
VECTOR_WEIGHT = 0.7   # 混合分數：向量相似度權重；其餘為關鍵字分數
MIN_SCORE = 0.35      # 低於此分數視為不相關，避免把無關原文送給模型

Embedder = Callable[[Sequence[str], str], list[list[float]]]


# ---------------------------------------------------------------------------
# 切段：每段都是原文的連續子字串
# ---------------------------------------------------------------------------
_BREAKS = "。！？；\n"


def split_excerpt(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """將原文切成重疊片段；每段皆為 text 的連續切片，盡量在句號等標點處斷開。"""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind(mark, start + size // 2, end) for mark in _BREAKS)
            if cut != -1:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---------------------------------------------------------------------------
# 嵌入：預設使用 Gemini；測試時可注入假的 embedder
# ---------------------------------------------------------------------------
_TRANSIENT_MARKERS = ("429", "500", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")


def is_transient_error(err: Exception) -> bool:
    """免費金鑰常見的 429（額度）、503（忙碌）等暫時性錯誤才重試；金鑰錯誤等直接失敗。"""
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    if isinstance(code, int):
        return code in (429, 500, 503, 504)
    return any(marker in str(err) for marker in _TRANSIENT_MARKERS)


def call_with_retry(call: Callable[[], Any], *, attempts: int = 1, base_delay: float = 2.0,
                    max_delay: float = 60.0, sleep: Callable[[float], None] | None = None) -> Any:
    """暫時性錯誤時以指數退避重試；attempts＝總嘗試次數，1 表示不重試。"""
    import time

    sleep = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as err:
            if attempt == attempts or not is_transient_error(err):
                raise
            sleep(min(base_delay * 2 ** (attempt - 1), max_delay))
    raise AssertionError("unreachable")


def gemini_embedder(api_key: str, model: str = DEFAULT_EMBED_MODEL, *, attempts: int = 1) -> Embedder:
    """attempts：每批的嘗試次數。線上問答維持 1（失敗就改關鍵字，不讓使用者久等）；
    tools/build_index.py 預先建索引時使用較多次數。"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    def embed(texts: Sequence[str], task_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = list(texts[i:i + EMBED_BATCH])
            result = call_with_retry(
                lambda: client.models.embed_content(
                    model=model,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type=task_type),
                ),
                attempts=attempts,
            )
            vectors.extend(list(item.values) for item in result.embeddings)
        if len(vectors) != len(texts):
            raise RuntimeError("嵌入結果筆數與輸入不符。")
        return vectors

    return embed


def default_embedder(attempts: int = 1) -> Embedder | None:
    """依環境設定建立 Gemini 嵌入函式；離線模式或沒有 API Key 時回傳 None。"""
    if os.getenv("INSURRADAR_OFFLINE") == "1":
        return None
    from services.llm_service import get_gemini_config  # 會先載入 .env，也支援 Streamlit Secrets

    api_key = get_gemini_config()["api_key"]
    if not api_key or api_key == "your_gemini_api_key_here":
        return None
    return gemini_embedder(api_key, os.getenv("GEMINI_EMBED_MODEL", DEFAULT_EMBED_MODEL), attempts=attempts)


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# ---------------------------------------------------------------------------
# 檢索器
# ---------------------------------------------------------------------------
class HybridRetriever(BaseRetriever):
    """向量＋關鍵字混合檢索；回傳格式符合 docs/RETRIEVER_CONTRACT.md 第三節。"""

    def __init__(
        self,
        dataset: dict[str, Any],
        *,
        embedder: Embedder | None = None,
        index_dir: Path | None = INDEX_DIR,
        vector_weight: float = VECTOR_WEIGHT,
        min_score: float = MIN_SCORE,
    ):
        self.dataset = dataset
        self.dataset_id = dataset.get("dataset_id")
        self.keyword = MockRetriever(dataset)
        self.vector_weight = vector_weight
        self.min_score = min_score
        self.index_dir = index_dir
        self.last_mode = "keyword"
        self.last_error: str | None = None

        self.evidence = {
            e["evidence_id"]: e for e in dataset.get("evidence", [])
            if isinstance(e.get("excerpt"), str) and e["excerpt"].strip()
        }
        self.reranker = Reranker(dataset, self.evidence)
        self.chunks: list[tuple[str, str]] = [
            (eid, piece) for eid, item in self.evidence.items() for piece in split_excerpt(item["excerpt"])
        ]

        if embedder is None:
            embedder = default_embedder()
        self.embedder = embedder
        self.matrix: np.ndarray | None = None
        self._index_tried = False  # 索引延後到第一次檢索才建立，開頁面不會呼叫 API

    def _ensure_index(self) -> None:
        if self._index_tried or not self.embedder or not self.chunks:
            return
        self._index_tried = True
        try:
            self.matrix = self._load_or_build_matrix()
        except Exception as err:  # 建索引失敗時仍可用關鍵字檢索
            self.last_error = f"向量索引建立失敗，改用關鍵字檢索：{err}"
            self.matrix = None

    # -- 索引快取 ------------------------------------------------------------
    def _fingerprint(self) -> str:
        digest = hashlib.sha256()
        for eid, piece in self.chunks:
            digest.update(eid.encode("utf-8") + b"\0" + piece.encode("utf-8") + b"\0")
        return digest.hexdigest()[:16]

    def _cache_path(self) -> Path | None:
        if self.index_dir is None or not self.dataset_id:
            return None
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.dataset_id)
        return Path(self.index_dir) / f"{safe_id}_{self._fingerprint()}.npz"

    def _load_or_build_matrix(self) -> np.ndarray:
        path = self._cache_path()
        if path and path.exists():
            with np.load(path, allow_pickle=False) as stored:
                matrix = stored["vectors"]
            if matrix.shape[0] == len(self.chunks):
                return matrix
        vectors = self.embedder([piece for _, piece in self.chunks], "RETRIEVAL_DOCUMENT")
        matrix = _normalize(np.asarray(vectors, dtype=np.float32))
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, vectors=matrix)
        return matrix

    def build_index(self) -> int:
        """預先建立並快取向量索引（供 tools/build_index.py 呼叫），回傳片段數。"""
        if not self.embedder:
            raise RuntimeError("沒有可用的嵌入服務（請設定 GEMINI_API_KEY 且未啟用離線模式）。")
        self.matrix = self._load_or_build_matrix()
        self._index_tried = True
        return len(self.chunks)

    # -- 檢索 ----------------------------------------------------------------
    def _format(self, eid: str, excerpt: str, score: float) -> dict[str, Any]:
        src = self.evidence[eid]
        return {
            "evidence_id": eid,
            "source_title": src.get("source_title"),
            "document_version": src.get("document_version"),
            "page": src.get("page"),
            "section": src.get("section"),
            "excerpt": excerpt,
            "score": float(min(max(round(score, 4), 0.0), 1.0)),
            "source_type": src.get("source_type"),
            "source_url": src.get("source_url"),
            "product_ids": list(src.get("product_ids") or []),
            "dataset_id": self.dataset_id,
        }

    def _best_chunk(self, eid: str, query: str) -> str:
        """關鍵字模式下，挑出與問題重疊最多的片段，避免把整份長原文送給模型。"""
        grams = {query[i:i + 2] for i in range(len(query) - 1)} or {query}
        pieces = [piece for chunk_eid, piece in self.chunks if chunk_eid == eid]
        return max(pieces, key=lambda p: sum(g in p for g in grams)) if pieces else self.evidence[eid]["excerpt"].strip()

    def _keyword_results(self, keyword_scores: dict[str, float], profile, query: str,
                         top_k: int) -> list[dict[str, Any]]:
        ranked = sorted(((score * self.reranker.factor(eid, profile), eid) for eid, score in keyword_scores.items()),
                        key=lambda x: (-x[0], x[1]))
        scores = {eid: score for score, eid in ranked}
        picked = self.reranker.diversify([eid for _, eid in ranked], profile, top_k)
        return [self._format(eid, self._best_chunk(eid, query.strip()), scores[eid]) for eid in picked]

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if type(top_k) is not int or top_k < 1:
            raise ValueError("top_k 必須為正整數。")
        if not isinstance(query, str) or not query.strip() or not self.evidence:
            return []

        self._ensure_index()
        profile = self.reranker.profile(query)
        keyword_scores = self.reranker.keyword_scores(profile)

        if self.matrix is None:
            self.last_mode = "keyword"
            return self._keyword_results(keyword_scores, profile, query, top_k)

        try:
            query_vec = np.asarray(self.embedder([query.strip()], "RETRIEVAL_QUERY")[0], dtype=np.float32)
        except Exception as err:
            self.last_mode = "keyword"
            self.last_error = f"問題嵌入失敗，改用關鍵字檢索：{err}"
            return self._keyword_results(keyword_scores, profile, query, top_k)

        self.last_mode = "hybrid"
        self.last_error = None
        norm = float(np.linalg.norm(query_vec)) or 1.0
        similarities = self.matrix @ (query_vec / norm)

        best: dict[str, tuple[float, str]] = {}
        for (eid, piece), sim in zip(self.chunks, similarities.tolist()):
            if not math.isfinite(sim):
                continue
            vector_score = max(0.0, min(1.0, sim))
            score = self.vector_weight * vector_score + (1 - self.vector_weight) * keyword_scores.get(eid, 0.0)
            if eid not in best or score > best[eid][0]:
                best[eid] = (score, piece)

        # 相關度門檻用調整前的分數判斷；排序與回傳分數套用商品／月份／類型加權
        ranked = sorted(((score * self.reranker.factor(eid, profile), eid, piece)
                         for eid, (score, piece) in best.items() if score >= self.min_score),
                        key=lambda x: (-x[0], x[1]))
        found = {eid: (score, piece) for score, eid, piece in ranked}
        picked = self.reranker.diversify([eid for _, eid, _ in ranked], profile, top_k)
        return [self._format(eid, found[eid][1], found[eid][0]) for eid in picked]


def describe_mode(retriever: BaseRetriever) -> str:
    """給 UI 顯示的一行說明。"""
    mode = getattr(retriever, "last_mode", None)
    if mode == "hybrid":
        return "檢索模式：向量＋關鍵字混合"
    if mode == "keyword":
        return "檢索模式：關鍵字（未使用向量）"
    return "檢索模式：Mock"


if __name__ == "__main__":  # 簡易手動測試：python -m services.c_retriever "問題"
    import sys

    from services.data_service import load_dataset

    data = load_dataset(os.getenv("INSURRADAR_DATASET_PATH") or None)
    retriever = HybridRetriever(data)
    question = " ".join(sys.argv[1:]) or "宣告利率"
    for hit in retriever.search(question, top_k=5):
        print(json.dumps({k: hit[k] for k in ("evidence_id", "score", "excerpt")}, ensure_ascii=False))
    print(describe_mode(retriever), retriever.last_error or "")
