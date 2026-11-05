"""預先建立向量索引快取，Demo 現場只需嵌入使用者問題。

用法（在專案根目錄，需已設定 GEMINI_API_KEY）：
    python -m tools.build_index                      # 使用 INSURRADAR_DATASET_PATH 或預設 Mock 快照
    python -m tools.build_index data/real/dataset.json

索引對象為快照 evidence 的原文（含 tools/add_pdf_evidence.py 加入的 PDF 段落）。
免費金鑰常遇到 429／503，這裡每批最多嘗試 BUILD_ATTEMPTS 次並指數退避。
"""

import collections
import os
import sys

from services.c_retriever import HybridRetriever, default_embedder
from services.data_service import load_dataset

BUILD_ATTEMPTS = 8  # 退避 2、4、8…最長 60 秒，總等待約 3 分鐘


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else (os.getenv("INSURRADAR_DATASET_PATH") or None)
    dataset = load_dataset(path)
    retriever = HybridRetriever(dataset, embedder=default_embedder(attempts=BUILD_ATTEMPTS))
    by_type = collections.Counter(retriever.evidence[eid].get("source_type") for eid, _ in retriever.chunks)
    print(f"{dataset['dataset_id']}：{len(retriever.evidence)} 筆證據、{len(retriever.chunks)} 個片段"
          f"（{'、'.join(f'{k} {v}' for k, v in sorted(by_type.items()))}）；新聞 {len(dataset.get('news', []))} 則")
    try:
        count = retriever.build_index()
    except Exception as err:
        print(f"[失敗] {err}", file=sys.stderr)
        return 1
    print(f"已為 {dataset['dataset_id']} 建立 {count} 個片段的向量索引，快取於 {retriever._cache_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
