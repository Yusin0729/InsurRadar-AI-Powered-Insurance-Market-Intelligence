"""預先建立向量索引快取，Demo 現場只需嵌入使用者問題。

用法（在專案根目錄，需已設定 GEMINI_API_KEY）：
    python -m tools.build_index                      # 使用 INSURRADAR_DATASET_PATH 或預設 Mock 快照
    python -m tools.build_index data/real/dataset.json
"""

import os
import sys

from services.c_retriever import HybridRetriever
from services.data_service import load_dataset


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else (os.getenv("INSURRADAR_DATASET_PATH") or None)
    dataset = load_dataset(path)
    retriever = HybridRetriever(dataset)
    try:
        count = retriever.build_index()
    except Exception as err:
        print(f"[失敗] {err}", file=sys.stderr)
        return 1
    print(f"已為 {dataset['dataset_id']} 建立 {count} 個片段的向量索引，快取於 {retriever._cache_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
