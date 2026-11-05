# D → C 交接（2026-11-05 晚）

C 組你好，以下三件事請處理。D 已完成 Demo 問答快取、真實模式文字修正與部署說明（詳見 `docs/HANDOFF.md` 最上方一節）。

## (a) README 請加入下列一節

請把下面整段原樣放進 README 的「## 前端與 Demo 操作（由 D 補充）」（可調整標題層級以配合你的結構，內容請勿改數字與指令）：

````markdown
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
   - 南山美滿臻愛的保單貸款利率是多少？（展示「資料不足」時系統不硬答）
   - 南山美滿臻愛最新一期的宣告利率是多少？
   - 超美利的宣告利率是多少？
   - 哪一家最好？（展示不做排名、回覆資料不足）
   回答下方點「檢視回答依據」開啟引用原文；展開「檢索到的依據片段」可看檢索結果。
4. **分析報告匯出**（側欄）：「準備 Markdown 報告」→「下載 UTF-8 Markdown」。

### Demo 時請避免

- **市場情報追蹤**頁：真實快照沒有新聞資料，頁面為空。
- 頁首「資料待補／複核提示（457 項）」展開區：內容是逐筆待補清單，現場展開會很長。
- 臨時輸入未快取的題目：若 Gemini 忙碌，會顯示忙碌提示而不是答案。
````

## (b) 請核對快取回答

快取檔：`data/real/demo_cache/demo_answers.json`（共 8 筆，2026-11-05 由 `gemini-3.5-flash-lite` 產生，快照 `real_2026_11_05_v4`）。請逐筆對照 `data/real/dataset.json` 與 PDF／官網原文，確認數字、單位與引用 ID：

| 題目 | 狀態 | 引用 | D 初步對照 | 請 C 確認 |
| --- | --- | --- | --- | --- |
| 美利大運的宣告利率是多少？ | success | ev_rate_fbo_202611 | 4.35%、115/11 與原文一致 | 月份是否為 Demo 時最新 |
| 哪些保險商品提供 6 年期繳費方案？ | success | E16、E4、E1 | 三商品 6 年期與原文一致 | 回答寫「增值回饋分享金為2.25％」，E16 章節為「預定利率（6年期、8年期）」，請確認此數字性質與用語正確 |
| 各家美元保單投保年齡限制為何？ | success | E14、E4、doc_acupl_dm_2026_06_p004_006 | 年齡範圍與原文一致 | 回答內文含 [E14] 等方括號 ID，屬正常引用標記；回答提到南山 10 年期 0～70 歲（DM 原文有），但快照投保規則只登記 6 年期，比較庫篩 10 年期不會出現南山（你已標記的不一致），請決定是否補登或 Demo 時口頭說明 |
| 超美利第 10 年末的保證解約金是多少？ | success | E9 | 305,291.60 與原文一致 | 回答未寫幣別與對應基本保額／投保年齡條件，請確認是否需重產或在 Demo 口頭補充 |
| 南山美滿臻愛的保單貸款利率是多少？ | insufficient_data | 無 | 回覆資料不足 | 確認快照確實沒有保單貸款利率 |
| 南山美滿臻愛最新一期的宣告利率是多少？ | success | ev_rate_acupl_202611 | 4.30%、115/11 與原文一致，含「非保證，可能調整」 | 月份是否為 Demo 時最新 |
| 超美利的宣告利率是多少？ | success | ev_rate_u09r0_202611 | 4.20%、2026/11 與原文一致，含「非保證，可能調整」 | 同上（此題 C 未以 RAG 實測過） |
| 哪一家最好？ | insufficient_data | 無 | 回覆資料不足，無排名 | — |

**Q2 核對結果**：Q2「哪些保險商品提供 6 年期繳費方案？」11/5 晚重產 4 次（間隔 40 秒）：3 次通過驗證但都含「增值回饋分享金為2.25％」（檢索到的富邦 6 年期依據只有 E16，原文本身就是這句），第 4 次 503。無符合條件的結果，保留舊快取，舊快取同樣含這句。要根治需修正 E16 或補一筆富邦繳費年期證據（B15，C 組資料），之後再用 `--refresh` 重產。Demo 時這題（也是快速範例第 2 顆按鈕）請由講者說明 2.25% 是增值回饋分享金、非保證，或改問其他題。

若任何一筆不正確：刪除 JSON 中該筆，或以 `--refresh "題目"` 重產後再核對；不要手改回答內容。

另請把快取產生指令（上方 README 段落中的 `python -m tools.build_demo_cache`）加入 `docs/C_DATA_GUIDE.md` 的快照更新流程：**每次快照改版（dataset_id 改變）後都要重跑**，否則舊快取會自動失效、現場無快取可用。

## (c) 其他須知

- 已依你的複核清單逐項接受 10 項代改（結果見 `docs/HANDOFF.md`「D 複核 C 代改（11/5）」）；預設模型已改為 `gemini-3.5-flash-lite`（`services/llm_service.py`，rag_service 共用），README／C_DATA_GUIDE 若寫「預設 gemini-2.5-flash」請更新，`.env` 的 `GEMINI_MODEL` 仍可覆寫。
- 新增檔案：`services/demo_cache.py`、`tools/build_demo_cache.py`、`tests/test_demo_cache.py`；修改：`ui/assistant.py`（快取接入、真實模式說明文字）、`services/report_service.py`（報告標示快取回答）、`tests/test_app.py`、`tests/test_report_service.py`、`docs/HANDOFF.md`。
- 你今天代改的 `ui/assistant.py`、`ui/comparison.py`、`ui/dashboard.py`、`services/rag_service.py`、`services/llm_service.py`、`services/gemini_retry.py`、`prompts/rag_qa.txt` 我都保留，未回退。快取的「忙碌」判斷沿用 `gemini_retry.is_transient`／`BUSY_MESSAGE`，若你改這兩個名稱請先告知。
- `tests/test_app.py` 新增 RealModeAppTests：本機有 `data/real/dataset.json` 時會跑四頁，檢查沒有 Mock／模擬字樣與例外；你改真實快照或頁面文字後若這個測試失敗，代表畫面又出現模擬字樣。
- 部署：未部署。Community Cloud 無法直接放真實資料（data/real 排除且 repo 公開），選項寫在 `docs/HANDOFF.md`，由 A 決定。README 若提到部署，請不要寫成已上線。
