# InsurRadar — 美元利變壽險競品情報

InsurRadar 是 2026 法巴人壽校園黑客松作品，聚焦台灣「美元利率變動型終身壽險」。
它把各家保險公司官網公告的宣告利率、商品 DM 與保單條款整理成同一份可驗證的資料快照，
讓業務或商品企劃人員可以篩選、並排比較商品，並用自然語言向 AI 助手提問。
每一個數字與每一句 AI 回答都附上來源證據（網址、頁碼、條號、原文），點開就能核對；
找不到依據時，系統直接回答「資料不足」，不讓模型自行補數字。

> 宣告利率**非保證，可能調整**，也不是保證報酬或 IRR。本系統只整理公開資料，不提供投保建議、不對商品排名。

## 功能（依頁面）

| 頁面 | 功能 |
| --- | --- |
| 總覽儀表板 | 公司數、商品數、待追查事項等指標；各公司商品統計；可按鈕呼叫 Gemini 產生 AI 重點觀察（每則都附證據，引用不合法的觀察會被排除）；待追查事項（未揭露欄位、待校對資料） |
| 競品比較庫（預設首頁） | 依公司、繳費年期（躉繳為 0）、投保年齡、商品關鍵字、宣告利率月份篩選；指定月份的宣告利率比較表；商品詳情（投保規則、保障、通路）；每筆利率與規則都可開啟證據視窗 |
| 市場情報追蹤 | 新聞情報的分類／公司／日期篩選與摘要卡片。**真實快照目前沒有新聞資料**（新聞已移出本次範圍），此頁只在 Mock 快照有內容 |
| AI 分析與 RAG 助手 | 自然語言問答：檢索快照中的原文片段 → Gemini 產生回答 → 檢查引用；回答下方列出檢索到的依據與檢索模式（向量＋關鍵字／僅關鍵字）。附範例問題捷徑 |
| 側欄：分析報告匯出 | 把目前篩選範圍的商品、利率、AI 觀察、問答紀錄與引用附錄匯出成 UTF-8 Markdown |

四頁共用同一個證據視窗：顯示證據 ID、文件標題、來源類型、日期、版本、頁碼、章節、原文、人工複核狀態與來源連結。

## 架構

```text
官網宣告利率 API ─┐
商品 DM／條款 PDF ─┼─► C 組資料管線（tools/）──► 資料快照 JSON ──► 驗證（schema + 引用規則）
B 組人工校對表 ───┘                              data/real/dataset.json      services/validation.py
                                                         │
                                                         ▼
                              混合檢索器 services/c_retriever.py（Gemini 向量 0.7 + 關鍵字 0.3，
                              加上 services/c_rerank.py 的商品／月份／問題類型排序調整；
                              向量索引用 numpy 存在 data/real/index/*.npz）
                                                         │
                                                         ▼
                              Gemini RAG services/rag_service.py：只把檢索到的原文送給模型，
                              回答必須引用當次檢索到的 evidence_id，否則不顯示
                                                         │
                                                         ▼
                              Streamlit UI（app.py、ui/）：四頁、共用證據視窗、Markdown 匯出
```

- **資料快照**：一份 JSON 包含商品、投保規則、各月宣告利率、證據、觀察、待追查事項。每筆商品與利率都指向證據 ID；`dataset_id` 是快照的不可變識別，內容一改就換新 ID。
- **驗證**：`services/validation.py` 依 `schemas/dataset.schema.json` 檢查結構，並檢查引用的證據存在、證據的商品關聯一致、來源日期不晚於快照時間等。驗證失敗時頁面顯示錯誤並停止，**不會自動退回 Mock**。
- **檢索**：`HybridRetriever` 實作 D 組定義的 `BaseRetriever.search(query, top_k)`（契約見 `docs/RETRIEVER_CONTRACT.md`）。回傳的 `excerpt` 一定是快照原文的連續片段。沒有 API Key、設定離線或嵌入 API 失敗時，自動改用關鍵字檢索，不拋例外。
- **引用檢查**：送模型前檢查每筆檢索結果的證據 ID、來源與原文是否對得上目前快照；模型回應必須是固定 JSON（answer、citations、has_sufficient_evidence），引用必須屬於當次檢索結果。無引用、引用不合法或模型自稱依據不足時，只顯示固定的「資料不足」文案。引用檢查只代表來源可追溯，不代表語意正確或已人工複核。
- **Gemini 暫時性錯誤**：503 忙碌、429 額度時自動重試 2 次（約多等 4.5 秒），仍失敗則顯示中文提示，請使用者稍後再問。

## 安裝

需要 Python 3.11（開發環境 3.11.9）。在專案根目錄：

```powershell
python -m venv .venv
.venv\Scripts\activate          # Git Bash：source .venv/Scripts/activate
python -m pip install -r requirements.txt
```

主要套件：`streamlit==1.66.1`（固定版本，測試用到它的 AppTest 私有屬性）、`jsonschema`、`google-genai`、`python-dotenv`、`numpy`、`pdfplumber`。

## 環境變數（`.env`）

複製 `.env.example` 為 `.env` 再填值。`.env` 已被 `.gitignore` 排除，**不要把金鑰提交到 Git**。

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | AI 功能需要 | Google Gemini API 金鑰。沒有金鑰時頁面照常顯示，AI 觀察與問答會提示尚未設定；檢索改用關鍵字 |
| `GEMINI_MODEL` | 建議設定 | 程式預設 `gemini-3.5-flash-lite`，建議仍在 `.env` 明確設定（環境變數優先）。新申請的免費金鑰不能用舊的 `gemini-2.5-flash`（回 404） |
| `INSURRADAR_DATASET_PATH` | 否 | 快照路徑（專案相對或絕對）。留空＝載入 Mock 快照；設為 `data/real/dataset.json` 載入真實快照 |
| `INSURRADAR_OFFLINE` | 否 | 設為 `1` 時不呼叫嵌入 API，只用關鍵字檢索（現場網路不穩時的備援；問答本身仍需 Gemini） |
| `GEMINI_EMBED_MODEL` | 否 | 向量嵌入模型，預設 `gemini-embedding-001` |
| `INSURRADAR_DEMO_CACHE` | 否 | 設為 `1` 時，已快取的 Demo 題目直接顯示預先產生的回答、不呼叫 Gemini；未設定時只在 503／429 失敗時自動改用快取（見下方「Demo 問答快取」） |

設定讀取順序：環境變數 > `.env` > Streamlit Secrets > 預設值。

## 啟動

```powershell
python -B -m streamlit run app.py --server.address 127.0.0.1
```

- **Mock 快照**（預設）：`data/mock/dataset.json`，虛構的 5 家公司、6 項商品，頁面會標示 Mock／模擬資料。clone 後不需任何資料即可執行。
- **真實快照**：在 `.env` 設 `INSURRADAR_DATASET_PATH=data/real/dataset.json`。**`data/real/` 不在公開 repo 中**（`.gitignore` 排除，內含官網原始回應、PDF 與索引），需另外取得整個資料夾放到專案內。
- 真實快照第一次提問時會讀取 `data/real/index/` 的向量索引快取；沒有快取時會現場嵌入全部片段（免費金鑰約 3 分鐘），建議先執行下方的 `build_index`。

## C 組資料管線（依序執行）

以下指令都在專案根目錄、已啟用 venv 時執行；Windows 終端機中文亂碼時先設 `PYTHONIOENCODING=utf-8`。各步驟細節見 `docs/C_DATA_GUIDE.md`。

```powershell
# 1. B 組人工校對表（CSV，欄位範本在 templates/real_csv/）→ 快照；驗證失敗不寫檔
python -m tools.build_real_dataset --dataset-id real_YYYY_MM_DD_vN --as-of 2026-11-05T18:00:00+08:00

# 2. 抓官網宣告利率（三家官網背後的查詢 API），列出與快照的差異；不改快照，數值由 B 校對後再放入
python -m tools.scrape_rates --compare data/real/dataset.json

# 3. 解析 DM／條款 PDF 成段落（含頁碼、條號），並檢查快照中的 PDF 證據原文落在第幾頁
python -m tools.parse_pdf data/real/docs/TaiwanLife_term_034.pdf --doc-id doc_taiwanlife_term_034 --source-url <PDF網址> --locate data/real/dataset.json

# 4. 用 Gemini 從段落抽取結構化欄位（每個值附逐字引句，引句對不上原文就拒收）
python -m tools.extract_fields --all

# 5. 抽取結果與 B 人工表自動比對，輸出 data/real/processed/compare/compare_report.csv
python -m tools.compare_extracted

# 6. 把 PDF 段落登記成快照證據（必須換新的 dataset_id）
python -m tools.add_pdf_evidence data/real/dataset.json --dataset-id real_YYYY_MM_DD_vN --as-of <時間> --backup <備份路徑>

# 7. 預先建立向量索引快取（需 GEMINI_API_KEY）
python -m tools.build_index data/real/dataset.json

# 8. 檢索評估（hit@3、MRR）
python -m tools.eval_retrieval --offline
python -m tools.eval_retrieval --questions tests/data/retrieval_questions_holdout.json

# 9. 重新產生 Demo 問答快取（dataset_id 一改就必須重跑，見下方「Demo 問答快取」）
python -B -m tools.build_demo_cache
```

目前真實快照 `real_2026_11_05_v4` 的檢索評估（k=3）：

| 題組 | 只用關鍵字 | 向量＋關鍵字 |
| --- | --- | --- |
| 原 10 題 | 10/10，MRR 0.90 | 10/10，MRR 1.00 |
| 保留題 10 題（改排序前寫定） | 9/10，MRR 0.80 | 10/10，MRR 0.93 |

## 測試

```powershell
python -B -m unittest discover -s tests -v
```

使用標準 `unittest` 與 Streamlit AppTest，不需 pytest。測試不呼叫真實 Gemini API（用假的模型回應、假的嵌入或離線模式），PDF 測試用程式產生的 PDF。

## 資料來源與限制

- **範圍**：真實快照 `real_2026_11_05_v4`（基準時間 2026-11-05 18:00 +08:00）只有 3 家公司、3 項商品：南山人壽美滿臻愛 ACUPL、台灣人壽超美利 U09R0、富邦人壽美利大運 FBO。
- **宣告利率**：31 筆，從三家官網的宣告利率查詢頁抓取。台灣人壽與富邦為 2025-12～2026-11（各 12 個月），南山為 2026-05～2026-11（7 個月）。宣告利率非保證，可能調整。
- **證據**：428 筆，其中官網頁 34 筆、人工整理的 DM 證據 13 筆、PDF 自動切段 381 筆（南山 DM 43 段、台灣人壽條款 338 段）。
- **富邦沒有解析 PDF**：富邦的資料只有官網頁與 B 組人工整理的 DM 證據。
- **PDF 段落證據沒有來源日期**，台灣人壽條款版本未知；不填猜測值，因此頁首「資料待補／複核提示」有 457 項，多為此類。
- **LLM 抽取結果未經人工複核**（`review_status=unreviewed`），只用於與 B 人工表比對，不直接寫入快照。
- **新聞不在本次範圍**：快照的 `news` 與 `insights` 為空；市場情報頁在真實模式沒有內容。
- **向量庫**：用 numpy 存在本機 `.npz`，沒有使用 Chroma 等外部向量資料庫；資料量小（455 個片段），不需要。
- 官網查詢 API 不是公開文件化的介面，網站改版時抓取可能失效，工具會回報失敗並沿用上一次成功的紀錄。
- 引用檢查只代表來源可追溯，不代表結論已經人工驗證。

## 用語規則

介面、Prompt、資料與簡報都不使用「報酬率」「最好」「推薦」等字眼，不對商品排名或給投保建議。
回答中提到宣告利率時註明「非保證，可能調整」；DM 試算範例中的假設宣告利率與依它算出的金額標示為「非保證試算」，不與保證金額混用。

## 前端與 Demo 操作（由 D 補充）

### 啟動（真實資料模式）

```bash
# .env 需有 GEMINI_API_KEY、GEMINI_MODEL=gemini-3.5-flash-lite
# 以及 INSURRADAR_DATASET_PATH=data/real/dataset.json
.venv/Scripts/python.exe -B -m streamlit run app.py --server.address 127.0.0.1
```

頁首顯示「真實資料快照」及 Dataset ID `real_2026_11_05_v4` 即為真實模式；未設定 `INSURRADAR_DATASET_PATH` 時載入 Mock 快照。

### Demo 問答快取（建議現場開啟）

免費 Gemini 金鑰可能在現場回 503／429。事先產生快取：

```bash
INSURRADAR_DATASET_PATH=data/real/dataset.json .venv/Scripts/python.exe -B -m tools.build_demo_cache
```

（PowerShell：`.env` 已設 `INSURRADAR_DATASET_PATH` 時直接執行 `python -B -m tools.build_demo_cache`；單題重產加 `--refresh "題目"`。）

- 只收錄真實執行且通過引用驗證的回答（success／資料不足），存於 `data/real/demo_cache/demo_answers.json`（不進版本庫）。
- 即時呼叫因 503／429 等暫時性錯誤失敗時，自動改顯示同一快照、同一題目的快取回答。
- 現場設定 `INSURRADAR_DEMO_CACHE=1` 可讓已快取題目直接使用快取、不呼叫 API；未快取題目照常即時回答。
- 快取回答一律標示「🗂️ 快取回答（預先產生於 …）」，引用仍可開啟證據原文。題目須一字不差、快照 ID 須相同才會命中；快照改版後需重新產生。

### 離線模式

`INSURRADAR_OFFLINE=1`：檢索不呼叫嵌入 API，只用關鍵字比對（網路不穩時使用）。搭配 `INSURRADAR_DEMO_CACHE=1`，已快取題目可在完全不呼叫 Gemini 的情況下展示。

### Demo 流程（依頁面）

1. **競品比較庫**（預設首頁）：側欄篩選公司／繳費年期／投保年齡，查看商品清單與指定月份宣告利率比較表；點「檢視商品與利率證據」開啟原文證據彈窗。宣告利率非保證，可能調整，本頁不做排名。
2. **總覽儀表板**：商品與公司統計、待追查事項（點「檢視待查證據」）。真實快照目前沒有 AI 觀察與市場情報，這兩區會顯示「目前沒有…」。
3. **AI 分析與 RAG 助手**：點快速範例問題或輸入下列已快取題目（須一字不差）：
   - 美利大運的宣告利率是多少？
   - 哪些保險商品提供 6 年期繳費方案？
   - 各家美元保單投保年齡限制為何？
   - 超美利第 10 年末的保證解約金是多少？
   - 南山美滿臻愛最新一期的宣告利率是多少？
   - 超美利的宣告利率是多少？
   - 南山美滿臻愛的保單貸款利率是多少？（展示「資料不足」時系統不硬答）
   - 哪一家最好？（同上，系統不排名）

   「哪些保險商品提供 6 年期繳費方案？」的快取回答多了一句「增值回饋分享金為2.25％」（資料問題 B15），Demo 建議跳過這題，或口頭說明該數字待校對。
   回答下方點「檢視回答依據」開啟引用原文；展開「檢索到的依據片段」可看檢索結果。
   快取回答的核對結果見 `docs/BUG_LIST_MVP.md`「Demo 快取核對」。
4. **分析報告匯出**（側欄）：「準備 Markdown 報告」→「下載 UTF-8 Markdown」。

### Demo 時請避免

- **市場情報追蹤**頁：真實快照沒有新聞資料，頁面為空。
- 頁首「資料待補／複核提示」展開區（內含 457 項）：內容是逐筆待補清單，現場展開會很長。
- 臨時輸入未快取的題目：若 Gemini 忙碌，會顯示忙碌提示而不是答案。

本專案**尚未部署**到雲端；部署選項（真實資料不在公開 repo 的處理方式）見 `docs/HANDOFF.md`，由 A 決定。

## 相關文件

| 文件 | 內容 |
| --- | --- |
| `docs/C_DATA_GUIDE.md` | C 組資料管線、CSV 欄位規則、檢索與排序調整、評估結果 |
| `docs/RETRIEVER_CONTRACT.md` | D 組定義的檢索器介面契約 |
| `docs/HANDOFF.md` | D 組前端、RAG 與報告匯出的交接紀錄（含 1A～E3 歷史） |
| `docs/BUG_LIST_MVP.md` | MVP 驗收 bug 清單與已知限制 |
| `docs/handoff/C_to_D.md` | C 組交給 D 組的複核清單與 Demo 問題 |
| `AGENTS.md` | 協作規則 |

## 專案結構

```text
app.py                 入口與四頁導覽
ui/                    各頁、共用篩選、證據視窗、報告匯出（D 組）
services/              資料載入與驗證、RAG、Gemini 呼叫（D 組）；c_retriever／c_rerank／c_integration（C 組）
prompts/               RAG 問答、AI 觀察、欄位抽取的 Prompt
schemas/               快照 JSON Schema
tools/                 C 組資料管線工具
templates/real_csv/    B 組填寫用 CSV 範本
data/mock/             Mock 快照（公開）
data/real/             真實快照、原始資料、索引（不公開）
tests/                 unittest 測試；tests/data/ 為檢索評估題目
```
