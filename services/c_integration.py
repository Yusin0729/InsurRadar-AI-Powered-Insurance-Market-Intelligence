"""C 組與 D 組前端的串接點：決定載入哪一份快照、提供 RAG 使用的檢索器。

D 組只需在兩處呼叫本模組（ui/common.py、ui/assistant.py 已接上）：
1. ui/common.py 的 load_app_dataset()：load_dataset(get_dataset_path())
2. ui/assistant.py 的問答：answer_rag_query(..., retriever=get_retriever(dataset))

規則：
- 未設定 INSURRADAR_DATASET_PATH 時回傳 None，data_service 照舊載入 Mock 快照；
  不自動切換、也不在載入失敗時降級成 Mock（沿用 HANDOFF 的「不自動降級」）。
- 檢索器依 dataset_id 快取在記憶體；dataset_id 是快照的不可變識別，內容變更必須換 ID。
- 向量索引只為真實快照寫入 data/real/index/（已被 .gitignore 排除）；
  Mock 快照不寫檔，避免 Mock 與真實資料路徑混用。

外部套件：python-dotenv（讀 .env）；HybridRetriever 另用 numpy、google-genai。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from dotenv import load_dotenv

from services.c_retriever import INDEX_DIR, HybridRetriever, describe_mode  # noqa: F401（供 UI 匯入）

load_dotenv()  # 讓 .env 中的 INSURRADAR_DATASET_PATH 也生效（llm_service 亦同）

DATASET_PATH_ENV = "INSURRADAR_DATASET_PATH"

_lock = threading.Lock()
_retrievers: dict[str, HybridRetriever] = {}


def get_dataset_path() -> str | None:
    """回傳環境變數指定的快照路徑；未設定或空白時回傳 None（＝預設 Mock）。"""
    value = (os.getenv(DATASET_PATH_ENV) or "").strip()
    return value or None


def get_retriever(dataset: dict[str, Any]) -> HybridRetriever:
    """取得目前快照的檢索器；同一 dataset_id 重複使用，避免每題重建索引。"""
    dataset_id = dataset.get("dataset_id") or ""
    with _lock:
        retriever = _retrievers.get(dataset_id)
        # Streamlit 每次 rerun 都會重新載入快照物件，因此只比對 dataset_id。
        if retriever is None:
            index_dir = INDEX_DIR if dataset.get("data_mode") == "real" else None
            retriever = HybridRetriever(dataset, index_dir=index_dir)
            _retrievers.clear()  # 只保留目前快照，切換快照時釋放舊索引
            _retrievers[dataset_id] = retriever
        return retriever


def reset_retrievers() -> None:
    """清除快取（測試或切換快照後使用）。"""
    with _lock:
        _retrievers.clear()
