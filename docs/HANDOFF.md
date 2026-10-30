# InsurRadar E3 Markdown 匯出交接（2026-10-30）

本節為目前狀態；下方 E2 與 U 階段紀錄保留為歷史依據。

## 匯出入口與資料流

- 四頁共用側欄「分析報告匯出」→「準備 Markdown 報告」→「下載 UTF-8 Markdown」。使用 Streamlit 原生元件及既有品牌綠，不新增 PDF／Word 或套件。
- `ui/report.py` 擷取必要 Session State 副本；`services/report_service.py` 透過既有 Data Service 取得快照資料，產生 Markdown 與 UTF-8 bytes。匯出不呼叫 Gemini、不讀取 Secrets、不改寫聊天／篩選／證據彈窗。
- 內容包含產生時間（Asia/Taipei）、Dataset ID／基準時間、各自的商品與情報範圍、商品／宣告利率、快照觀察／既有 Gemini 分析、RAG 紀錄、情報摘要、引用附錄及限制聲明。
- 商品採公司／年期／年齡／關鍵字篩選，再與指定比較商品取交集；利率精確採目前月份，空範圍不回退全部商品。市場情報保留獨立公司／商品／分類／日期／關鍵字篩選，不套用側欄商品範圍。
- AI 觀察嚴格排除涉及範圍外商品或無法確認商品關聯的內容，並顯示原因。只篩選既有觀察，不依新年期／年齡／月份重新生成或宣稱適用。
- RAG 問題／回答須屬於目前 Dataset；引用須存在並屬於保存的檢索範圍，檢索原文子片段與定位須對應目前來源；保存的商品關聯須等於檢索商品聯集。限定商品範圍時，全數檢索來源須有明確商品關聯且未超出範圍。無法確認時排除並提示，不推測問題語意。
- Gemini 結果在 UI 保存時附 Dataset ID；RAG 保存 Dataset ID、既有生成時間及檢索商品關聯。快照切換清除 Gemini／RAG／報告暫存；舊的無歸屬結果不得匯出。
- 引用附錄只列目前快照的有效引用；無效 AI 引用整則排除、無效情報摘要不輸出原結論。RAG 失敗／不足只列安全狀態。缺日期、原文、版本或利率不補造。
- 複核沿用既有 `validate_insight_evidence`；Mock 及未複核結果不標示為已驗證，RAG 引用檢查不代表語意支持性或人工複核。
- 動態段落與證據原文採動態長度 fenced text 區塊，保留原文及換行；表格跳脫 Markdown／HTML 字元。不將來源或使用者輸入當可執行 HTML／圖片或連結。
- Report Service 可傳入固定產生時間；資料集合依穩定 ID 排序、RAG 保留對話順序。報告指紋包含快照、篩選與既有生成結果，變更後舊下載失效。原生下載設定 `on_click="ignore"`，不觸發整頁 rerun。

## E3 測試與限制

- E3 定向測試：20 項通過（16 服務測試、4 Streamlit AppTest）。完整回歸：`python -B -m unittest discover -s tests -v`，100 項、53.779 秒、全部通過（0 失敗、0 錯誤），包含原有 80 項及 E3 20 項。
- AppTest 驗證真實下載元件／URL、傳入的 UTF-8 bytes、MIME、檔名與 `on_click="ignore"`；未以瀏覽器實際寫入 Downloads 目錄。AppTest 不提供原生下載 click 介面。
- 未發現已知阻擋 E3 交付的問題。無真實 Gemini API 呼叫；本輪不驗證真實模型內容品質、不整合 C 組、不開始 F 階段。完成後停止，等待使用者指示。
- 未修改 Dataset Schema、Mock Dataset、E1／E2 核心驗證規則，沒有 GitHub 上傳或網站部署。

---

# InsurRadar E2 接手狀態（2026-10-30）

本節優先於下方歷史交接紀錄。E1 與 E2 已有實作；下方原文件關於「尚無 LLM／RAG」的敘述僅代表 U 階段狀態。

## E2 目前實作

- Python／Streamlit 架構不變。`BaseRetriever.search(query, top_k=3)` 可注入 C 組實作，預設 MockRetriever；C 組真實服務尚未接入、契約尚待雙方確認。
- 呼叫 Gemini 前先檢查 evidence_id、來源、版本、定位、相關度與目前快照原文。允許經空白正規化後的連續子片段；不接受改寫原文。空結果或全數無效時不呼叫 API。
- Gemini 回應必須只有 answer（非空字串）、citations（字串陣列）、has_sufficient_evidence（布林）。缺漏／額外欄位或錯誤型別不強制轉型；引用須存在且屬於當次有效檢索。
- 無引用、無效引用或模型宣稱不足時，不展示模型的肯定回答；不足使用固定安全文案，驗證失敗不保留可展示的原始回答。
- `verification_passed` 僅表示格式與引用邊界檢查，不表示語意支持性、證據完整性、人工複核或已驗證結論。相關度分數亦不代表結論可信度。
- 聊天歷史只供顯示，每題獨立檢索；沒有多輪指代解析或 Query Rewriting。一般 rerun／切頁／開窗不重新生成；dataset_id 變更會清除 RAG 歷史。
- 問答沿用單一證據彈窗；來源以當前快照為準。不得在不更換 dataset_id 的情況下原地替換快照內容。
- Gemini 設定逐項採「環境變數 > Streamlit Secrets > 預設值」；模型預設 gemini-2.5-flash，Key 無預設值。
- Prompt 載入／格式化、檢索及 API 失敗回傳錯誤；top_k 須為正整數（不接受 bool）。

## 驗證與後續

使用替身 Retriever、Mock Gemini 回應與 AppTest 驗證，沒有呼叫真實付費 Gemini API。最終回歸結果見下方驗收補記。
真實 SDK 網路傳輸、模型可用性、金鑰權限、配額與真實生成品質尚未驗證。C 組真實檢索與資料整合尚待後續工作。
本輪未改 Dataset Schema、Mock Dataset、核心驗證規則，未部署、未上傳 GitHub、未開始 E3。

---

## E2 驗收補記（2026-10-30）

- 修正後完整回歸：`python -B -m unittest discover -s tests -v`，80 項、50.327 秒、全部通過（0 失敗、0 錯誤）；其中既有四頁／資料／證據回歸 46 項、E1 13 項、E2 21 項。
- E2 覆蓋嚴格欄位型別、引用存在／檢索邊界、資料不足肯定回答攔截、空／未知／版本或快照不一致來源、合法原文子片段、API 替身錯誤、Prompt 錯誤、top_k、中文排序與設定優先序。
- AppTest 實際輸入兩輪獨立問題，驗證 rerun、切頁、證據開窗不增加生成次數，快照切換清除對話與舊引用按鈕。舊靜態助手斷言已改為目前聊天及來源檢視的對應測試。
- 沙箱內 UI 測試曾停滯並中止；以授權本機權限重新執行 UI 測試及完整回歸後通過。DatasetWarning 為現有 Mock 快照的待補狀態；AppTest 的 ScriptRunContext 提示不是測試失敗。
- 本機 Streamlit 於 127.0.0.1:8522 啟動成功，`/_stcore/health` 回覆 HTTP 200／ok，驗證後停止服務。此項是啟動／健康檢查，UI 互動由 AppTest 驗證，未另做瀏覽器視覺驗收。
- 本輪沒有真實 Gemini API 呼叫。實際連線、模型帳號可用性／Schema 接受度、權限配額、延遲與生成品質仍未實測；語意支持性不能由引用檢查保證。
- 目前未發現未解決 P0；E2 已達本輪替身測試與本機驗證的交接標準。可交接 E3 規劃，但尚未開始 E3，等待使用者指示。

---

## 以下為 U 階段歷史交接（非目前功能清單）

# InsurRadar 專案交接（Google Antigravity）

交接日期：2026-10-30（Asia/Taipei）。本文件以目前程式碼、README 與本次對話中實際執行紀錄為依據；本次只建立本文件，未重跑測試、啟動服務或開始 E1。

## 1. 專案概況與分工

2026 法巴人壽校園黑客松專案，聚焦美元利率變動型終身壽險，提供競品比較、市場情報、可追溯觀察及證據檢視；真正 LLM／RAG 尚未串接。技術為 Python + Streamlit + JSON，保持現有架構，不改用 React、Next.js 或 FastAPI。

- 四頁：總覽儀表板、競品比較庫（預設首頁）、市場情報追蹤、AI 分析與 RAG 助手。
- 四頁共用單一 `st.dialog` 證據彈窗。
- D 組：AI 與前端，負責觀察／摘要／問答的生成與引用、Streamlit UI、來源檢視與匯出。
- C 組：資料工程，負責擷取、PDF 解析、結構化抽取、商品／利率／新聞資料與向量庫；預計交付真實資料及檢索介面，尚未接入。
- A 負責範圍、提示詞與 Demo；B 負責資料及原文校對。角色依根目錄 `企劃書.md`、`我的工作內容.md`，其中日期及勾選清單是早期規劃，不能當完成紀錄。
- 優先遵守 `AGENTS.md`：修改前說明檔案與原因；執行、測試、安裝及檔案修改需使用者授權。交接不代表後續開發已獲授權。

## 2. 已完成的開發

| 階段 | 可由程式碼確認的實作 | 驗證依據 |
| --- | --- | --- |
| 1A | Mock 快照、Schema、集中資料服務、結構／關聯／引用驗證 | 現有檔案與後續累積回歸 |
| 1B | 四頁導覽、公司／年期／年齡／文字／月份篩選、比較及商品詳情 | README 記錄 19 項通過，納入後續回歸 |
| 1C | 完整快照 KPI、預寫 Mock 觀察、情報摘要、待查及共用證據窗 | README 記錄 33 項通過，納入後續回歸 |
| 1D | 市場獨立篩選、逐點來源、靜態範例問答與停用輸入 | README 記錄 46 項通過，U 階段再回歸 |
| U | 品牌綠主題、深綠側欄、白色卡片、資訊展開區、助手 55：45 及窄版堆疊、原文優先證據窗 | 本次對話上輪實際執行 46 項全部通過；另有瀏覽器確認，詳見第 6 節 |

U 色票是專案自訂設計方向，非官方品牌色碼或官方網站宣稱。Mock 標示、缺漏警示及四類複核狀態仍保留。未完成即時新聞分析、LLM、RAG、PDF 原檔渲染、匯出及真實資料整合。

## 3. 實際結構、模組及啟動

```text
專案根目錄/
├─ AGENTS.md
├─ README.md
├─ 企劃書.md
├─ 我的工作內容.md
├─ app.py
├─ requirements.txt
├─ .streamlit/config.toml
├─ docs/HANDOFF.md                 # 本次新增
├─ schemas/dataset.schema.json
├─ data/mock/dataset.json
├─ services/
│  ├─ data_service.py
│  └─ validation.py
├─ ui/
│  ├─ common.py
│  ├─ dashboard.py
│  ├─ comparison.py
│  ├─ market.py
│  ├─ assistant.py
│  ├─ evidence.py
│  ├─ placeholders.py
│  └─ styles.css
├─ tests/test_app.py
└─ REFERENCES/stitch_insurradar/
   ├─ dashboard/                   # code.html、screen.png
   ├─ ai_rag/                      # code.html、screen.png
   ├─ _1/、_2/、_3/                # 各含 code.html、screen.png
   └─ insurradar_intelligence_engine/DESIGN.md
```

省略執行產生的 `__pycache__`。目前沒有 `docs/project_plan.md`、`prompts/`、LLM 模組或真實資料目錄。`AGENTS.md` 所指企劃路徑不存在，現有需求背景請改讀根目錄 `企劃書.md`；README 停留 1D，U 外觀以程式碼及本文件為準。

| 模組 | 主要入口與責任 |
| --- | --- |
| `app.py` | `main()`：頁面設定、每輪證據狀態清理、載入、共用篩選、`st.navigation`、頁面執行後單一開窗調度 |
| `ui/common.py` | `load_app_dataset()`、`render_shell()`、`render_sidebar_filters()`、`get_filtered_products()`、`reset_filters()`：載入、主題、快照切換及比較條件 |
| `ui/comparison.py` | `product_rows()`、`comparison_rows()`、`render_rate_table()`、`sync_selection()`、`render_product_detail()`、`render_comparison()`：資料呈現及穩定 ID 選取 |
| `ui/dashboard.py` | `dashboard_data()`：完整快照統計；`ordered_news()`：依已有日期排序；`render_dashboard()`：觀察／情報／公司統計／待查 |
| `ui/market.py` | `filtered_news()`、`render_market_filters()`、`render_market_card()`、`render_market()`：服務篩選後再比對摘要關鍵字 |
| `ui/assistant.py` | `render_assistant()`：完整快照觀察／待查及既有觀察原文的靜態問答；`save_assistant_example()` 保存選取 |
| `ui/evidence.py` | 證據按鈕、四類狀態呈現、單一視窗與 fragment 切換，詳見第 5 節 |
| `ui/placeholders.py` | 舊模組名稱轉接；正式導覽直接使用 market／assistant |
| 主題／CSS | `.streamlit/config.toml` 為主；`render_shell()` 以 `st.html(Path(...))` 載入 `ui/styles.css` |

在專案根目錄啟動（以下是接手後可用的指令，本次未執行）：

```powershell
python -B -m streamlit run app.py --server.address 127.0.0.1
# 指定其他本機連接埠時加 --server.port 8511
```

正式導覽宣告 `/dashboard`、`comparison`（default=True，瀏覽器實際首頁為 `/`）、`/market`、`/assistant`；沿用 `st.Page`，不要重新手寫路由。

版本核對：`requirements.txt` 宣告 `streamlit==1.66.1`、`jsonschema>=4.18,<5`。本機 Python311 的安裝中繼資料／標頭唯讀核對為 Python **3.11.9**、Streamlit **1.66.1**、jsonschema **4.26.0**；不代表另一台機器已具備相同環境。未宣告 LLM SDK、dotenv、向量庫或額外測試套件；測試使用標準 `unittest` 與 Streamlit AppTest。目前展示不需 API Key。新增依賴需另獲授權；金鑰只用環境／Secrets，不寫進程式或交接文件。

## 4. 資料契約與 C 組接入

Schema 為 JSON Schema Draft 2020-12，`schema_version=1.0.0`；根層包含 `dataset_id`、`data_mode`、`mock_notice`、含時區的 `as_of` 與六類集合：

| 集合 | 主鍵 | 用途 |
| --- | --- | --- |
| products | product_id | 公司、商品、USD、投保規則、保障、通路、日期、來源 |
| rates | rate_id | 商品指定月份的 declared 宣告利率、適用條件、生效區間及引用 |
| news | news_id | 分類、發布者、日期、公司／商品關聯、逐點摘要及引用 |
| insights | insight_id | 觀察文字、商品及證據關聯、生成模式、結論複核 |
| evidence | evidence_id | 原文、來源類型／標題／日期／版本／頁碼／章節、複核紀錄 |
| investigations | investigation_id | 原因、缺漏欄位、建議處理、優先級及狀態 |

目前快照為 `mock_dataset_2026_10_v1`、基準 `2026-10-30T09:00:00+08:00`。`dataset_id` 識別快照，變更時觸發 UI 狀態重設；`product_id` 用於關聯與選取，不能以表格列號取代；`evidence_id` 是可查回原文的穩定識別碼，非頁面序號。各類主鍵不得重複，所有引用須存在。

`services/data_service.py` 現有公開介面（均傳入 dataset；查詢回傳深複本，不寫回）：

```text
load_schema() -> dict
load_dataset(dataset_path: str | Path | None = None) -> dict
get_products(dataset, *, company_ids=None, payment_term_years=None,
             issue_age=None, keyword=None) -> list[dict]
get_declared_rates(dataset, *, product_ids=None, period_month=None) -> list[dict]
get_news(dataset, *, category=None, company_ids=None, product_ids=None,
         date_from=None, date_to=None) -> list[dict]
get_insights(dataset, *, product_ids=None) -> list[dict]
get_evidence(dataset, evidence_id) -> dict | None
get_investigations(dataset, *, product_ids=None, priority=None, status=None) -> list[dict]
```

- 預設載入 `data/mock/dataset.json`；自訂路徑須明確傳入，相對路徑以專案根目錄解析。失敗不自動降級或補造資料。UI 只經服務讀取，不自行開 JSON。
- 例外：`DatasetLoadError`、其子類 `DatasetValidationError(report)`；合法待補以 `DatasetWarning` 提醒。
- ID 篩選的 `None` 表示不限；空清單不是不限。UI 在公司等未選情況主動轉成 `None`。`get_insights` 篩選要求該觀察的非空商品集合全部在範圍內；待查以商品交集篩選。
- 年期 0 是躉繳、年齡 0 有效；年期與年齡必須命中同一條規則，未知年齡不視為符合。不可推斷其他文字核保條件。
- 利率精確比對月份，每個適用條件各列一筆；缺值保留 null，不補零、不沿用其他月份、不排名、不視為保證報酬或 IRR。
- 新聞條件間 AND、ID 清單內 OR，日期按快照時區含起迄日；有日期條件就排除日期未知者。UI 關鍵字另比對標題、發布者與摘要。未知日期單獨呈現。

`validation.py`：`validate_dataset(dataset, schema)` 回傳 `errors`、`warnings`、`counts`；errors 阻止載入，warnings 保留合法缺值。檢查 Schema／格式、主鍵、商品與證據關聯、投保規則及摘要引用範圍、年齡／日期順序、利率唯一性與月份交集、非有限數值、來源／複核時間不得晚於快照、複核狀態與人員／時間一致、路徑安全及 Mock／real 一致性。JSON 讀取另拒絕重複欄位、NaN／Infinity。

Mock／真實隔離：每筆 `is_mock`、ID 的 `mock_` 前綴、`generation_mode`、來源類型與快照模式須一致。Mock 來源網址必須 null，Mock 本機證據路徑須在 `data/mock/`；真實資料不能用此路徑或混入模擬生成內容。真實模式 `mock_notice` 為 null。`local_path` 僅是定位資訊，目前不讀檔或渲染 PDF。

**C 組預定接入，尚非既定 API：** 由獨立轉接層將 C 組輸出映射為現有快照，先驗證再交給 UI，保留既有服務簽章及 Mock 路徑。`search()` 尚不存在；參數、回傳格式、top-k、篩選、錯誤／逾時、傳輸方式及 evidence_id 對應均待與 C 組確認。建議結果至少能映射證據 ID、原文與來源定位；此為協作建議，不能當已確認契約。不直接把檢索分數當成結論已驗證。

## 5. AI、證據與 Session State

目前 Dashboard／助手從 `get_insights()` 取預寫 Mock 觀察，未呼叫 LLM；新聞摘要及待查也直接讀快照。助手的固定問題與回答只是所選觀察 `statement` 原文，引用沿用 `evidence_ids`，輸入框 `disabled=True`，沒有真正聊天歷史、檢索或即時推論。

四類狀態須分開：

1. 引用存在／有效：非空、無重複、每個 evidence_id 存在。
2. 證據完整：來源標題、日期、版本、原文、網址或檔案定位，以及依來源需要的頁碼／章節、真實資料擷取時間齊備；不等於語意支持結論。
3. 證據人工複核：每筆 confirmed 且有複核者與時間。
4. AI 結論複核：觀察本身 confirmed 且另有結論複核者與時間；不能由證據狀態自動推導。

`validate_insight_evidence(insight, dataset)` 回傳上述狀態、缺漏清單與 `can_display_as_verified`；只有非 Mock、完整、證據全數人工確認且結論人工確認才可顯示已驗證。任何 Mock 觀察／來源仍只能標模擬紀錄。此函式可檢查新觀察，但不取代完整快照／結構驗證。

證據調用方式：頁面使用 `evidence_button(dataset, evidence_ids, source_kind=..., source_id=..., label=...)` 提出一次性要求；`app.py` 每次完整執行先 `begin_evidence_run()`，頁面後僅呼叫一次 `render_requested_evidence(dataset)`。視窗內切換只 rerun fragment。`render_insight_review()` 呈現觀察狀態；一般來源使用 `render_source_review()`，AI 結論為不適用。`assistant_example` 來源窗只驗證來源，回答結論狀態仍在原回答旁。不存在的 ID 不靜默刪除；空引用、缺版本、缺原文會提示。

| 狀態鍵 | 意義／注意事項 |
| --- | --- |
| active_dataset_id | 快照識別；變更時重設比較、證據、market_*、assistant_* |
| filter_company_ids、filter_payment_term_years | 公司清單／年期；重設由 reset_filters 處理 |
| filter_issue_age_enabled、filter_issue_age | 年齡是否啟用及值，保留 0 歲 |
| filter_keyword、filter_period_month | 商品搜尋及精確月份 |
| comparison_product_ids、selected_product_id | 比較集合及詳情商品；失效選取清除 |
| selected_evidence_ids | 詳情商品、規則及當月利率的去重引用；Dashboard 不覆寫 |
| market_keyword、market_category | 市場獨立搜尋／分類 |
| market_company_ids、market_product_ids | 市場公司／商品條件 |
| market_date_from、market_date_to | date 或 None；市場重設不影響比較 |
| assistant_example_id | 靜態範例的原始 insight_id |
| evidence_dialog_context、active_evidence_id | 當次開窗快照、來源、證據清單及目前證據 |
| _evidence_open_requested、_evidence_choice_id | 一次性開窗要求及視窗暫存選項 |

`_selected_product_id`、`_comparison_product_ids`、`_market_*`、`_assistant_example_id` 是 widget 暫存鍵；跨頁邏輯讀無底線持久鍵，透過現有 callbacks 同步。`assistant_chat_input` 為停用 widget。證據按鈕 key 為 `evidence_open_{source_kind}_{source_id}`，新增入口須避免重複。

證據關閉按鈕／右上角／Esc／遮罩只清證據狀態；切頁及完整 rerun 清舊窗，不改比較／市場選取。只保證同一 session，整頁重新整理／重新連線不保證保存。

## 6. 測試、環境與限制

- 最近一次實際完整測試：2026-10-30 的 U 階段，`python -B -m unittest discover -s tests -p test_app.py -q`，**46 項、39.055 秒、OK**；本次只讀紀錄，未重跑。當前檔案亦有 46 個 test 方法，未修改測試。
- U 回歸後最後僅將 `initial_sidebar_state` 從 expanded 改為 auto；此項另有語法與後續瀏覽器檢查，未再跑完整 46 項。精確而言，46 項通過的是此最後側欄設定調整之前的 U 版本。
- 測試覆蓋資料驗證、四頁導覽、篩選／重設、0 歲／躉繳、月份與缺值、穩定 ID、空資料、Mock 防誤標、來源引用、證據操作、切頁保留與快照重設。
- U 瀏覽器：1440px 桌面確認四頁及來源窗；1000px 助手兩欄改上下；390px 助手文字換行無整頁橫向溢出、比較表實際水平捲動、來源窗原文換行；商品證據從 mock_evidence_001 切至 mock_evidence_007，及按鈕／Esc 關窗。未逐一重測所有來源及關閉方式；右上角／遮罩等完整生命週期另有 README 的 1C 歷史紀錄。
- U 前後資料服務、驗證模組、Schema、Mock Dataset 四檔 SHA-256 一致。此交接不另修改它們。
- 最近驗證服務使用 127.0.0.1:8511；1D 曾使用 8510。服務是否仍在執行須接手時確認，不能把連接埠當部署設定。本次未啟動或探測服務。
- Windows 沙箱曾阻擋本機 socket，U 啟動與 AppTest 已在獲授權的沙箱外執行；不能因此改動驗證規則或系統網路安全設定。
- AppTest 的 callable 頁面切換使用集中 helper 與 Streamlit 1.66.1 私有屬性，升級需回歸。U CSS 使用自訂 st-key 容器及少量 data-testid，須檢查欄位堆疊／KPI；原生長表格仍需水平捲動，長證據窗需垂直捲動。
- 未驗證：其他瀏覽器／裝置全覆蓋、完整鍵盤及無障礙合規、真實來源網址連線（Mock 網址為 null）、真實 PDF、API／RAG／部署。無法將模擬複核稱為真實驗證。
- **Git：目前根目錄沒有 .git；`git status --short` 實際回報「not a git repository」。因此無法判定未提交變更，不能宣稱工作目錄乾淨，也沒有可交接的 commit／branch。** 搬移時須完整保留目前檔案，後續由使用者確認正確儲存庫位置。

## 7. 尚未完成

| 階段 | 待辦 |
| --- | --- |
| E1 | 真正 LLM 分析、獨立 Prompt、結構化 JSON 輸出、生成結果結構及引用驗證、逾時／錯誤處理 |
| E2 | 真正問答、C 組 search() 串接、檢索結果到證據的映射、回答引用 |
| E3 | 先做 Markdown／列印友善報告，再評估其他格式；目前無匯出功能 |
| C 組 | 真實資料轉接、來源定位與穩定 ID、檢索服務及契約確認 |
| F | C／D 端到端測試、Demo 快取／離線備援、部署與交付文件／影片 |

異動偵測只有兩期真實資料到齊才考慮。不得以假成長率、假 LLM 回答或假檢索統計填補未完成區域。

## 8. Antigravity 開始 E1 的建議

1. 先讀 `AGENTS.md`、本文件、README 的資料／Session State 交接，再讀 `services/data_service.py`、`services/validation.py`、Schema 的 insight／evidence 定義、`ui/evidence.py`、`ui/assistant.py`、`ui/dashboard.py` 及相關測試。需求背景查根目錄企劃書，無須重做整份 Schema 或 Stitch 分析。
2. 先向使用者提出具體 E1 檔案範圍與測試計畫再實作。建議新增 `services/llm_service.py`（呼叫／解析／錯誤處理）及 `prompts/analysis.txt`（獨立 Prompt）；可另增針對性的 `tests/test_llm_service.py`。這些只是建議名稱，尚不存在；供應商、模型、SDK、費用及設定方式尚未選定。
3. 以現有已確認結構化商品／利率與可追溯原文作輸入，輸出對齊既有 insight 欄位。先驗證 JSON 結構，再使用既有引用驗證；不存在／不完整引用不可顯示為已驗證，不能自動寫入人工複核資訊。
4. 先確認 Mock 上實際 LLM 呼叫的模式：現有快照規則要求 Mock generation_mode=mock，不能把新 LLM 結果冒充原始 Mock 或直接混寫快照。應另行設計明確標示的暫存結果與驗證接點並取得同意，不能自行放寬 Schema／驗證。
5. 保留資料服務簽章、穩定 ID、四頁導覽、篩選規則、所有 Session State／callback、單一 dialog 調度、原文與缺漏提示及四類狀態。預設 Mock 展示仍應不需 Key 可操作；沒有金鑰／API 失敗時不偽裝成已生成結果。真正 RAG、匯出及資料整合仍是後續階段。
6. 測試優先模擬 API 邊界：有效 JSON、格式錯誤、未知／空引用、逾時、無金鑰、Mock 不誤標及不破壞原有互動；獲授權後才執行測試或付費 API。完成階段後停止，等待使用者決定下一階段。
