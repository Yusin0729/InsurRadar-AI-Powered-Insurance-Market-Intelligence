# C 組交接：真實快照、檢索服務與前端串接

對應 D 組交接文件 `docs/HANDOFF.md`（E3）與 `docs/RETRIEVER_CONTRACT.md`。C 組交付三件事：

1. **真實快照**：B 組維護的 CSV 經 `tools/build_real_dataset.py` 轉成 `data/real/dataset.json`，並通過 D 組既有的 `validate_dataset()`。
2. **檢索服務**：`services/c_retriever.py` 的 `HybridRetriever`，實作 `BaseRetriever.search(query, top_k=3)`。
3. **前端串接**：`services/c_integration.py`，以及對 D 組前端的最小修改（已套用，見第二節）。

D 組的資料服務簽章、驗證規則、Schema、Mock 快照、RAG 驗證邏輯都**不修改**。

## 一、檔案位置

以下檔案已放入專案（`dev` 分支）：

| 檔案 | 用途 |
| --- | --- |
| `services/c_retriever.py` | 向量＋關鍵字混合檢索器 |
| `services/c_integration.py` | `get_dataset_path()`、`get_retriever(dataset)`、`describe_mode()` |
| `tools/build_real_dataset.py` | CSV → 真實快照，驗證失敗不寫檔 |
| `tools/build_index.py` | 預先建立向量索引快取 |
| `templates/real_csv/*.csv` | B 組填寫用的 8 個 CSV 欄位範本 |
| `tests/test_c_retriever.py` | 檢索器與 CSV 轉換測試（10 項） |
| `tests/test_c_integration.py` | 串接測試（8 項，含助手頁 AppTest） |

## 二、對 D 組檔案的修改（已套用，2026-11-02）

共修改 4 個檔案，都是最小變更：

| 檔案 | 變更 |
| --- | --- |
| `ui/common.py` | `load_dataset()` → `load_dataset(get_dataset_path())`。未設環境變數時傳 `None`，與原本相同，仍載入 Mock |
| `ui/assistant.py` | 問答改為 `answer_rag_query(..., retriever=get_retriever(dataset))`；對話紀錄新增 `retrieval_mode`，顯示在「檢索到的依據片段」 |
| `.env.example` | 新增 `INSURRADAR_DATASET_PATH`、`INSURRADAR_OFFLINE`、`GEMINI_EMBED_MODEL` 說明（皆為註解） |
| `requirements.txt` | 明確宣告 `numpy>=1.26`（Streamlit 本來就會安裝） |

串接規則（沿用 HANDOFF）：

- **不自動降級**：設定了 `INSURRADAR_DATASET_PATH`，但真實快照載入或驗證失敗時，頁面照舊顯示錯誤並停止，不會偷偷改回 Mock。
- **快照識別**：檢索器依 `dataset_id` 快取，切換快照時自動換新。資料內容一改就必須換新的 `dataset_id`。
- **Mock／真實分離**：向量索引只為真實快照寫入 `data/real/index/`（已被 `.gitignore` 排除）。Mock 快照的索引只放在記憶體。
- **Session State**：只在 `rag_chat_messages` 的助手訊息多一個 `retrieval_mode` 字串，其他狀態鍵不變。快照切換時清除對話的行為沿用 D 組既有邏輯。

## 三、操作流程

```powershell
# 1. B 組在試算表填好各分頁，匯出 CSV 到 data/real/csv/（檔名同 templates/real_csv/）
# 2. 轉成快照並驗證（有錯誤會列出，並且不寫檔）
python -m tools.build_real_dataset --dataset-id real_2026_11_v1 --as-of 2026-11-03T09:00:00+08:00
# 3. 預先建立向量索引（需 GEMINI_API_KEY；Demo 現場就只需嵌入問題）
python -m tools.build_index data/real/dataset.json
# 4. 用真實快照啟動（也可寫在 .env）
$env:INSURRADAR_DATASET_PATH="data/real/dataset.json"
python -B -m streamlit run app.py --server.address 127.0.0.1
# 5. 測試
python -B -m unittest discover -s tests -p "test_c_*.py" -v
python -B -m unittest discover -s tests -v
```

| 環境變數 | 預設 | 說明 |
| --- | --- | --- |
| `INSURRADAR_DATASET_PATH` | 空＝Mock | 專案相對路徑或絕對路徑 |
| `INSURRADAR_OFFLINE` | 空 | 設為 `1` 時不呼叫嵌入 API，只用關鍵字檢索（現場網路不穩時的備援） |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | 向量嵌入模型 |
| `GEMINI_API_KEY` | 無 | 沿用 D 組 `get_gemini_config()`：環境變數 > Streamlit Secrets |

- `--as-of` 必須晚於所有來源日期、擷取時間與複核時間，否則驗證會報 `future_source`。
- `dataset_id` 不可用 `mock_` 開頭。

## 四、CSV 欄位規則

共通規則：
- 編碼 UTF-8。
- 清單欄位用半形分號 `;` 分隔。
- 空白儲存格＝null。
- ID 不可用 `mock_` 開頭。
- 日期格式 `YYYY-MM-DD`；時間要含時區，例如 `2026-10-20T10:00:00+08:00`。

| 檔案 | 必填 | 重點 |
| --- | --- | --- |
| evidence.csv | 是 | `source_type` 只能是 `official_page`／`pdf`／`news`；`excerpt` 一字不改貼原文；`pdf` 必填 `page`；`source_url` 或 `local_path` 至少一個；`captured_at` 必填；`local_path` 用專案相對路徑，不可放在 `data/mock/`；`human_review_status` 為 `unreviewed`／`confirmed`／`rejected`，非 unreviewed 時必填複核者與時間 |
| products.csv | 是 | `benefits`、`channels` 用分號分隔；商品 `evidence_ids` 可留空，轉換時會自動併入投保規則引用的證據 |
| eligibility.csv | 是 | 一個年期一列；躉繳填 `0`；年齡不知道就留空（會顯示資料不足，不會被當成符合） |
| rates.csv | 是 | `rate_percent` 可寫 `4.1` 或 `4.1%`；`period_month` 為 `YYYY-MM`；生效期間要和月份有交集；同商品、同月份、同條件不可重複 |
| news.csv | 否 | `category` 為 `regulatory`／`product`／`interest_rate`／`market`；`generation_mode` 為 `human` 或 `llm` |
| news_points.csv | 否 | 新聞摘要逐點，每點附引用 |
| insights.csv | 否 | AI 或人工觀察，`evidence_ids` 至少一個 |
| investigations.csv | 否 | 待追查事項；`priority` 為 high／medium／low，`status` 為 open／in_review／resolved |

證據的 `product_ids` 必須和引用它的商品或利率一致，否則驗證會報 `evidence_scope_mismatch`。

## 五、C 組對 RETRIEVER_CONTRACT 的確認

C 組確認接受 D 組提出的契約（第一～七節），實作如下：

- 回傳第三節所有欄位，另附 `dataset_id`。D 組 `validate_retrieved_items()` 會檢查這個值必須等於目前快照。
- 來源欄位一律從快照原樣複製。`excerpt` 是快照原文的連續子片段：長原文切成約 220 字、重疊 60 字的片段，同一證據只回傳最相關的一段。
- `score` 為 0～1 的 float，計算方式是 0.7×向量相似度＋0.3×關鍵字分數（沿用 `MockRetriever`）。低於 0.35 不回傳，避免把無關原文送給模型。關鍵字模式直接使用 `MockRetriever` 的分數。
- 不會回傳快照中不存在的證據 ID，也不會回傳原文為空的證據。
- 空問題回傳 `[]`。`top_k` 不是正整數時拋出 `ValueError`，與 `MockRetriever` 一致。
- 索引在第一次提問時才建立，開頁面不會呼叫 API。沒有 API Key、離線或嵌入失敗時，改用關鍵字檢索，不拋例外。`describe_mode()` 標示實際使用的模式，UI 會顯示。
- 不做多輪指代解析、Query Rewriting，也不使用外部向量庫或 Agent 框架。

## 六、驗證紀錄（2026-11-02）

在 `dev` 分支（HEAD `6ffc5a4`）放入 C 組檔案並套用第二節修改後執行：

- `test_c_*.py`：18 項全部通過。
- 完整回歸 `python -B -m unittest discover -s tests`：**118 項全部通過**（D 組原有 100 項＋C 組 18 項），耗時 120.6 秒。
- 測試環境：Python 3.14 隔離虛擬環境，Streamlit 1.66.1。專案指定的 Python 3.11 在這台機器上沒有安裝，因此**沒有在 3.11 上執行**。

未驗證項目：
- 真實 Gemini 嵌入 API：測試只使用假的嵌入函式與離線模式。
- 真實 CSV 資料：B 組尚未提供，測試只使用臨時假資料。
- 瀏覽器實際操作：只用 AppTest 驗證助手頁。

## 七、注意

- `.gitignore` 已排除 `/data/real/`，真實快照與索引不會進 Git。這個 repo 是公開的，部署到雲端時須另外決定快照的提供方式。
- 引用檢查只代表來源可追溯，不代表語意支持或人工複核。觀察要顯示為已驗證，仍需 B 組在 evidence 與 insights 填寫複核紀錄。
- D 組 E3 報告匯出沿用 `rag_chat_messages`。新增的 `retrieval_mode` 欄位目前不會寫進報告；如果要寫進去，由 D 組決定。
