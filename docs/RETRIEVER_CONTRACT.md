# InsurRadar 檢索服務介面契約 (Retriever Contract)

本文件為 D 組提出、待 C 組確認的契約；目前尚未接入真實檢索服務。

本文件定義 D 組（AI 與前端）與 C 組（資料工程與向量檢索）之間的標準檢索服務介面規格。

目前 D 組已於 `services/retriever.py` 實作 `BaseRetriever` 抽象介面及供測試使用的 `MockRetriever`。未來 C 組交付向量資料庫或檢索 API 時，僅需實作符合本契約之類別即可無縫接入。

---

## 一、介面定義

```python
from abc import ABC, abstractmethod
from typing import Any

class BaseRetriever(ABC):
    @abstractmethod
    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """檢索與使用者問題相關的依據片段。"""
        pass
```

---

## 二、輸入參數

| 參數名 | 型別 | 必填 | 說明 |
| :--- | :--- | :--- | :--- |
| `query` | `str` | 是 | 使用者提出的自然語言問題（繁體中文），例如「富邦人壽美利富貴宣告利率多少？」或「有哪些商品繳費年期為六年？」。 |
| `top_k` | `int` | 否 | 最多回傳之依據數量，預設為 `3`。必須為正整數，`0`、負數、布林、字串及小數皆無效。 |

---

## 三、回傳結構與欄位規格

`search()` 必須回傳 `list[dict]`，陣列長度不大於 `top_k`。陣列中每個物件必須具備以下欄位：

| 欄位名稱 | 型別 | 必填 | 說明與契約限制 |
| :--- | :--- | :--- | :--- |
| `evidence_id` | `str` | 是 | **唯一識別鍵**：必須對應資料集中 `evidence` 集合之 `evidence_id`（例如 `mock_evidence_001`）。此鍵為前端呼叫共用依據彈窗的核心憑證。 |
| `source_title` | `str` | 是 | 來源文件標題（例如《富邦人壽美利富貴美元利率變動型終身壽險條款》）。 |
| `document_version`| `str \| None` | 否 | 文件版本或代碼（例如 `v1.2`、`2026-10-A`），若無則為 `None`。 |
| `page` | `int \| None` | 否 | 引用原文所在頁碼（整數，>=1），非 PDF 來源可為 `None`。 |
| `section` | `str \| None` | 否 | 引用原文所在章節名稱或條號（例如「第 8 條 宣告利率之計算」）。 |
| `excerpt` | `str` | 是 | 檢索出之原始文字片段（供 LLM 閱讀及前端預覽，不可為空字串）。 |
| `score` | `float` | 是 | 檢索相關度分數，**數值範圍必須正規化為 `0.0 ~ 1.0`**（1.0 表示最為相關）。 |
| `source_type` | `str` | 是 | 來源種類，必須為 `["mock", "official_page", "pdf", "news"]` 之一。 |
| `source_url` | `str \| None` | 否 | 原始外部連結。若為 Mock 資料或未公開網址，請填 `None`。 |
| `product_ids` | `list[str]` | 是 | 該依據關聯之商品 ID 清單（若無特定商品關聯，則為空清單 `[]`）。 |

---

## 四、與前端共用依據彈窗的對應方式

1. 前端 RAG 回答卡片中，使用者點擊「檢視回答依據」按鈕時，系統透過 `ui/evidence.py` 的 `evidence_button(...)` 將回答中的 `citations` 傳入。
2. 系統直接以 `evidence_id` 自資料集中取出完整審核狀態、原文、版本及頁碼，並於單一 `st.dialog` 中呈現。
3. **重要限制**：檢索服務回傳的 `evidence_id` 必須確保能於資料集中查獲，**嚴禁回傳資料集不存在的幽靈 ID**。若 RAG 回答引用了未包含於當次 `search()` 回傳的依據，系統將直接判定該次回答驗證失敗。

---

## 五、空結果與錯誤處理

1. **查無相關依據**：
   - 當庫中沒有與 `query` 相關之內容時，`search()` 應安全回傳空清單 `[]`，不得引發例外崩潰。
   - 前端接收到空清單時，將主動向使用者提示「目前未檢索到足夠依據」，且不會發送無意義的 LLM API 請求。
2. **檢索逾時或服務異常**：
   - 若向量資料庫連線失敗或逾時，建議拋出標準 `RuntimeError` 或自訂例外。
   - 前端 RAG 服務將安全捕獲異常，向使用者呈現「檢索服務暫時無法連線」，不顯示偽造的成功回答。

---

## 六、未來 C 組替換 MockRetriever 指引

C 組完成向量檢索模組後，無須修改前端 UI 或驗證邏輯，僅需完成以下兩步驟：

1. **實作繼承類別**：
   ```python
   # services/retriever.py 或 services/c_retriever.py
   from services.retriever import BaseRetriever

   class VectorDBRetriever(BaseRetriever):
       def __init__(self, vector_store, embedding_client):
           self.store = vector_store
           self.client = embedding_client

       def search(self, query: str, top_k: int = 3) -> list[dict]:
           # 呼叫向量檢索並格式化為本文件第三節之標準 dict 清單
           ...
   ```
2. **在 RAG 服務中依賴注入**：
   在 `services/rag_service.py` 的 `answer_rag_query(..., retriever=VectorDBRetriever(...))` 傳入實例即可完成無縫切換。


## 七、E2 執行期檢查（2026-10-30）

- RAG 接收 list[dict]，忽略無效候選，去除重複 evidence_id，最多採用 top_k 筆；非 list 視為檢索服務錯誤。
- 每筆須有當前快照存在的 evidence_id、非空 excerpt 及有限且介於 0～1 的 score（不接受 bool）。
- excerpt 經空白正規化後必須是快照原文的連續子片段；允許完整原文及合法子片段，不接受摘要改寫或其他版本文字。
- source_title、source_type、source_url、document_version、page、section、product_ids，以及額外提供的 local_path、source_date、captured_at 必須與快照一致。可選來源欄位省略時由快照補齊；若提供 dataset_id，必須等於當前快照。
- 快照來源須有標題及 URL 或本機定位，source_type 合法，Mock 狀態須與快照一致。空原文不得僅憑標題送給模型。
- Prompt 與檢索預覽保留合法子片段，彈窗由相同 evidence_id 顯示快照原文及定位；不以 Retriever 傳來的不同來源覆蓋快照。
- 沒有有效候選時回傳資料不足，不呼叫 Gemini。部分候選無效時只送入剩餘有效候選。
- dataset_id 是快照不可變識別，來源內容或版本更換時必須更換 dataset_id。若相同文字與定位跨快照完全相同，未帶 dataset_id 的結果無法辨識其產生時間，但其內容仍須符合目前快照。
- 引用存在與範圍檢查不等於語意或人工複核；模型回答即使通過檢查仍須標示待人工複核。
- 本契約不要求多輪指代解析、Query Rewriting、向量庫或 Agent 框架。
