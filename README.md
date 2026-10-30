# InsurRadar — 階段 1D

使用 Python + Streamlit 展示美元利率變動型終身壽險的競品資料。
目前預設載入 **Mock／模擬資料**，不代表真實市場資訊或投保建議。

## 安裝與啟動

在專案根目錄使用 Python 3.11 執行：

```powershell
python -m pip install -r requirements.txt
python -B -m streamlit run app.py --server.address 127.0.0.1
```

以終端機顯示的本機網址開啟應用。Streamlit 固定為 **1.66.1**；
資料驗證保留 `jsonschema>=4.18,<5`。本輪環境為 Python 3.11.9、jsonschema 4.26.0。
UI 沒有額外元件或測試套件，也不需 API Key。

## 1B 功能

- 透過 `st.navigation`／`st.Page` 提供總覽、競品比較、市場情報與 AI／RAG 四頁。
- 共用側邊欄顯示資料模式、資料集 ID 與含時區的快照基準時間。
- 公司、單一年期、可選投保年齡、商品名稱關鍵字與宣告利率月份篩選。
  公司未選表示不限；躉繳使用資料契約中的 0；關鍵字沿用服務的搜尋範圍，
  可命中商品 ID、公司、名稱、保障特色與通路。
- 清單顯示逐條年期／年齡組合；任一年齡界線未知時，有年齡篩選便不視為符合。
- 可選多個商品進行表格比較，未指定時展示目前篩選結果；以商品 ID 選取詳情。
- 每筆指定月份利率、適用條件、生效起迄、紀錄 ID 與證據 ID 各自保留。
  無紀錄與有紀錄但數值缺漏分開標示，均顯示「資料不足」，不補零或沿用其他月份。
- 商品詳情保留投保規則、額外條件、保障、通路、日期與關聯證據 ID。
- 支援無結果、空資料集、無利率、合法缺值、載入／驗證失敗提示及篩選重設。

宣告利率不是保證報酬或 IRR。本頁不自動推測文字適用條件、不計算平均或排名。
即使商品通過六年繳費篩選，某筆僅適用躉繳的利率仍須依原文辨識；
比較表會完整保留這項限制，不宣稱該利率適用目前選定年期。
表格原生欄位排序只改變表格呈現，詳情透過獨立的商品 ID 選單決定。

## 資料與檔案分工

資料僅由 `services/data_service.py` 載入及查詢；UI 不直接開啟 JSON。
預設仍使用服務指定的 Mock 路徑，沒有真實資料自動切換或降級機制。
1A Schema、Mock 快照、資料服務與驗證模組維持不變。

| 檔案 | 責任 |
| --- | --- |
| `app.py` | 共用入口、既有四頁導覽、單一證據視窗調度 |
| `ui/common.py` | 資料載入、品牌、Mock 提示、側邊篩選 |
| `ui/comparison.py` | 比較表、ID 選取與商品詳情 |
| `ui/dashboard.py` | 完整快照統計、Mock 觀察、既有情報摘要、待追查事項 |
| `ui/evidence.py` | 共用證據視窗、引用／複核狀態、獨立視窗狀態 |
| `ui/market.py` | 市場情報篩選、摘要卡片及來源入口 |
| `ui/assistant.py` | AI 觀察、待追查事項與靜態問答 |
| `ui/placeholders.py` | 舊模組匯入轉接；正式導覽直接使用 market／assistant |
| `.streamlit/config.toml` | Stitch 配色與關閉使用統計 |
| `tests/test_app.py` | 1A～1D 回歸、情報篩選、靜態助手與證據互動測試 |

## 測試

```powershell
python -B -m unittest discover -s tests -p test_app.py -v
```

測試使用 Streamlit 內建 AppTest 與 Python 標準函式庫；不需 pytest。
測試中新增月份、多筆條件與空資料集只使用記憶體副本，不改寫 Mock 檔。
涵蓋四頁實際導覽、篩選、0 歲／躉繳、穩定 ID 選取、排序與切頁、月份精確匹配、
缺值、空結果、Mock 標示、載入失敗及 1A 資料／引用驗證介面。
由於 AppTest 公開的 `switch_page` 僅支援檔案頁，callable 頁面的測試
集中於一個 helper，以已登錄的真實頁面 hash 切換，沒有模擬導覽或頁面內容。
此處使用 Streamlit 1.66.1 私有測試屬性，升級 Streamlit 時須一併檢查。

1B 驗證結果（2026-10-30，保留作為回歸基準）：

- 最終程式版本執行 **19 項測試，全部通過**（11.536 秒）。
- 1A 資料服務、驗證模組、Schema 與 Mock Dataset 的 SHA-256 前後一致。
- Streamlit 1.66.1 成功啟動；`/_stcore/health` 回傳 HTTP 200、內容 `ok`。
- `/`、`/dashboard`、`/market`、`/assistant` 均回傳 HTTP 200；
  實際頁面內容另由 AppTest 四頁導覽測試驗證，瀏覽器確認競品比較頁載入。
- Codex 的 Windows 沙箱會阻擋 AppTest 所需的本機 socket；
  本輪已在授權的沙箱外環境完成測試與伺服器驗證，沒有修改系統網路設定。

## 既有商品篩選與選取狀態

`ui/common.py` 的 `load_app_dataset`、`render_shell`、`render_sidebar_filters`
與 `get_filtered_products` 可直接沿用。以下 `st.session_state` 狀態供後續整合：

| 狀態 | 內容 |
| --- | --- |
| `active_dataset_id` | 目前快照 ID；切換快照時重設選取及條件 |
| `filter_company_ids` | 公司 ID 清單；空清單表示不限 |
| `filter_payment_term_years` | `None` 或年期整數，0 為躉繳 |
| `filter_issue_age_enabled`、`filter_issue_age` | 是否啟用年齡與年齡整數 |
| `filter_keyword`、`filter_period_month` | 關鍵字與指定月份 |
| `comparison_product_ids` | 比較商品 ID；空清單表示展示目前篩選結果 |
| `selected_product_id` | 詳情商品 ID；不符合篩選時清為 `None` |
| `selected_evidence_ids` | 詳情商品、投保規則及指定月份利率的去重證據 ID |

`_selected_product_id`、`_comparison_product_ids` 是暫存 widget 狀態，
後續功能應讀取無底線的持久狀態。證據 ID 由已通過 1A 驗證的快照提供；
共用視窗透過既有 `get_evidence(dataset, evidence_id)` 取回證據。
證據 ID 的存在、證據完整性及結論是否經人工確認仍須分開處理。

## 1C Dashboard 與共用證據檢視

Dashboard 的資料由 `get_products()`、`get_insights()`、`get_news()` 與
`get_investigations()` 取得，使用完整快照，不套用側邊欄競品比較條件。
原有四頁網址與預設競品比較頁保持一致。

- KPI：依 `company_id` 去重的公司數、商品筆數、觀察筆數及未結案待追查數。
  未結案包含 `open`、`in_review`，不包含 `resolved`。
  目前 Mock 快照為 **5 家公司、6 項商品、3 則觀察、3 項未結案待查**。
  這些數字在執行時重新計算，沒有寫死在 UI。
- 公司統計：各公司商品數；空資料時提供明確提示。
- 觀察卡片：讀取預先撰寫的 Mock 內容，不呼叫 LLM；
  逐筆沿用 `validate_insight_evidence()`，分開顯示引用有效性、證據完整性、
  證據人工複核及 AI 結論複核。只有 `can_display_as_verified` 允許才有驗證顯示資格。
  Mock 的確認狀態標示為模擬複核紀錄，不能成為真實已驗證結論。
- 最新情報：只呈現快照中既有摘要，按發佈時間由新至舊顯示最多五則；
  日期未知另列在待確認區，不補成今天。沒有執行新聞分析。
- 待追查事項：未結案優先、再依優先級排序；顯示原因、缺漏欄位及建議處理方式，
  不提供指派或寫回資料功能。
- 全頁沿用 Mock 標示與含時區的資料基準日期。

觀察、情報與待查事項上的證據按鈕，以及商品詳情中的「檢視商品與利率證據」，
使用同一個 `st.dialog`。視窗內按穩定的 `evidence_id` 切換，顯示 ID、文件標題、
來源類型、來源日期、版本、頁碼、章節、原文、人工複核狀態、複核者及時間。
來源網址存在時才顯示連結，沒有讀取 `local_path` 或渲染 PDF。
單筆來源的完整性沿用 `validate_dataset()` 回報的 `evidence_incomplete` 提示；
不重新定義完整性規則，也不為商品來源假造 AI 觀察來驗證。

不存在的證據 ID 不會被靜默排除；視窗顯示錯誤並允許切換其他證據。
空引用清單、缺版本與缺原文均有對應提示。正式載入仍須先通過 1A 驗證，
沒有為了展示錯誤情境而放寬快照驗證。

## 證據視窗生命週期與後續交接

`evidence_button()` 只在當次點擊產生開窗要求，入口在頁面執行後呼叫一次
`render_requested_evidence()`；`st.dialog` 內的證據切換只重新執行 fragment。
入口每次完整執行先呼叫 `begin_evidence_run()`，因此切頁或按 Streamlit 的 `r`
重新執行會關閉舊視窗。關閉按鈕、右上角、Esc 與遮罩均清除視窗狀態。
這些動作不修改既有的 `filter_*`、`selected_product_id`、
`comparison_product_ids`、`selected_evidence_ids`。

| 狀態 | 內容 |
| --- | --- |
| `evidence_dialog_context` | 當次快照 ID、來源類型／ID 與去重證據 ID 清單 |
| `active_evidence_id` | 視窗內目前顯示的證據 ID |
| `_evidence_open_requested` | 一次性開窗要求，入口消耗後立即移除 |
| `_evidence_choice_id` | 視窗內的暫存 widget 狀態 |

後續頁面可直接重用 `evidence_button()` 與 `render_insight_review()`；
Dashboard 不覆寫比較頁的 `selected_evidence_ids`。
證據視窗狀態是暫時的，不應作為跨頁持久資料來源。

## 1C 驗證紀錄（2026-10-30）

- **33 項自動測試全部通過**，最終執行 9.948 秒；包含原有 19 項 1A／1B 回歸。
- 1A 資料服務、驗證模組、Schema、Mock Dataset 與 AGENTS.md 的 SHA-256 前後一致。
- AppTest 覆蓋完整快照 KPI、觀察引用對應、四類狀態、Mock 防誤標、
  空區塊、缺漏／不存在／空清單證據、開關與重新開啟、切頁及快照變更。
- 證據切換另有 AppTest 元件層測試；真實 fragment 行為由瀏覽器實測補足。
- 瀏覽器已實測商品與觀察的多筆證據切換、不完整證據提示、關閉按鈕、
  右上角、Esc、遮罩關閉、重新開啟、開窗時按 `r` 重新執行、
  開窗時使用瀏覽器返回競品比較頁；公司／年期篩選、詳情商品與比較集合均保留。
- Dashboard 在比較條件已設定時仍顯示完整快照的 5／6／3／3 指標。
- Streamlit 1.66.1 成功啟動；健康端點 HTTP 200、`ok`；四頁路由均回傳 HTTP 200。
- 不存在 ID 使用記憶體與 lookup 替身測試，未改寫 Mock 快照；
  真實快照的 1A 引用驗證仍會阻止不合法資料載入。

已知限制：AppTest callable 頁面切換仍使用前述集中封裝的私有測試屬性；
Mock 來源網址皆為 null，因此沒有以真實外部來源網址做瀏覽器連線驗證。
Streamlit 的整頁瀏覽器重新整理會建立新的連線，狀態不承諾跨連線持久保存；
本輪的保留驗證範圍為同一 session 內的彈窗、切頁與應用重新執行。
尚未實作 LLM、RAG、真實 PDF 渲染、新聞分析、報告匯出與 C 組整合。
本輪完成後停止於 1C，不開始 1D。

## 1D 市場情報與靜態助手（2026-10-30）

本輪新增 `ui/market.py`、`ui/assistant.py`，修改入口、共用狀態初始化、
舊佔位模組、測試與本文件。搜尋確認原本只有 app.py 引用 placeholders；
入口現已直接匯入正式模組，placeholders 保留名稱轉接，函式須傳入 dataset。
未新增套件，未修改 1A 資料服務、驗證模組、Schema 或 Mock Dataset。
前述 1C「不開始 1D」為該階段歷史紀錄；目前已完成以下 1D 功能。

### 市場情報追蹤

- 資料由 `get_news()` 取得，相關公司／商品名稱由 `get_products()` 取得。
- 分類、公司、商品及日期條件交給既有服務；條件間 AND、ID 清單內 OR。
  公司／商品未選時傳 None，代表不限。分類為既有監理、商品、利率、市場。
- 關鍵字只在服務回傳的標題、發布者、摘要文字進行去頭尾空白及大小寫不敏感比對。
- 起迄日皆可留空；依快照時區、包含起迄日。指定任一日期便排除日期未知的情報；
  起日晚於迄日顯示提示，不呼叫服務查詢。無日期條件時，未知日期另列待確認區。
- 已知日期由新至舊排序，沿用 Dashboard 的 ordered_news。
- 卡片包含新聞 ID、分類、發布者、日期、公司／商品關聯、既有摘要、逐點引用及整則引用。
  逐點與整則均可開啟共用證據視窗；網址存在才顯示外部來源連結。
- 空結果、空摘要、缺發布者／日期／網址及無公司／商品關聯均明確提示，不補造資訊。
- 重設情報條件不影響競品比較；側邊比較條件不套用市場情報。

### AI／RAG 靜態助手

- 左欄以 get_insights、get_investigations 展示完整快照的觀察與待查事項。
  待查維持未結案優先、再依優先級排序，不提供指派或資料寫回。
- 右欄用 st.chat_message 呈現固定問題與所選觀察的 statement 原文；
  回答及 evidence_ids 直接沿用既有觀察，不另建問答資料契約、不假裝生成新分析。
- 觀察與回答均沿用 render_insight_review，區分引用有效性、完整性、證據複核與結論複核。
- 所有證據入口沿用 evidence_button 及 app.py 的單一視窗調度。
  範例回答使用 assistant_example 來源識別，避免與觀察卡片重複 key；
  彈窗顯示來源資料狀態，回答旁顯示原始觀察結論狀態。
- 頁面及回答清楚標示靜態／模擬內容，聊天輸入與送出停用。
  未呼叫 LLM API、未執行 RAG 檢索、未產生檢索精準度或向量庫統計。
- 無觀察時不顯示範例回答；選取觀察被移除時回到目前第一筆。

### Session State 與交接

| 持久狀態 | 內容 |
| --- | --- |
| market_keyword | 情報標題、發布者及摘要搜尋字串 |
| market_category | None 或既有分類代碼 |
| market_company_ids／market_product_ids | 情報公司／商品 ID，空清單代表不限 |
| market_date_from／market_date_to | date 或 None |
| assistant_example_id | 靜態範例的原始 insight_id |

上述元件使用帶底線的暫存 widget key，callback 寫回持久狀態；
切頁後由持久狀態還原，彈窗操作不覆寫比較狀態。快照 ID 變更時由 render_shell
清除 market_*／assistant_* 及其暫存狀態；市場公司／商品選項失效時移除舊選取。
狀態只保證同一 session 內保留，不保證瀏覽器整頁重新整理或重新連線後保存。

### 驗證結果與限制

- 最終 **46 項自動測試全部通過（17.014 秒）**：原有 33 項回歸加 13 項 1D 測試。
  涵蓋篩選交集／聯集及重設、摘要搜尋、含起迄日、快照時區、未知日期、反向區間、
  缺值／空資料、答案原文與逐筆引用、各來源按鈕、缺漏證據、狀態保留及快照重設。
- 原始 1A 引用驗證仍會拒絕不存在或超出新聞引用範圍的摘要引用；
  異常展示測試僅用記憶體副本或 lookup 替身，不放寬正式資料載入。
- 1A 資料服務、驗證、Schema、Mock Dataset 與 AGENTS.md 的 SHA-256 前後一致。
- 瀏覽器實測摘要關鍵字與分類交集、重設、原生日期輸入及反向區間提示、
  摘要引用開窗、範例切換、多筆證據 fragment 切換、按鈕與 Esc 關窗、
  缺版本／缺原文提示，以及市場條件跨頁保留。
- 本機 Streamlit 使用 127.0.0.1:8510 啟動。健康端點與四頁路由均回傳 HTTP 200；
  AppTest／HTTP 在 Windows 沙箱內受 socket 限制，已改以授權的沙箱外環境完成。
- AppTest callable 頁面切換仍沿用既有集中 helper 及 Streamlit 1.66.1 私有測試屬性。
- Mock 網址皆為 null，未以真實外部來源連線驗證。未驗證行動版及所有瀏覽器。
  視窗右上角／遮罩的行為沿用未修改的 1C 元件，本輪未重複其全套瀏覽器測試。

### 供後續 UI 優化使用的元件與樣式

| 區域 | 既有元件／樣式入口 |
| --- | --- |
| 市場篩選 | render_market_filters；st.text_input、selectbox、multiselect、date_input、columns |
| 新聞卡片 | render_market_card；st.container(border=True)、subheader、caption、來源按鈕 |
| 觀察及待查 | render_assistant 左欄；原生邊框容器與既有狀態標示 |
| 範例問答 | render_assistant 右欄；st.chat_message、範例 selectbox、停用 chat_input |
| 引用及複核 | ui/evidence.py 的 evidence_button、render_insight_review、單一 st.dialog |
| 全站外觀 | .streamlit/config.toml；ui/common.py 的 render_shell 既有 CSS |

維持既有淺色主題：背景 #faf8ff、次背景 #eaedff、主色 #0051d5、文字 #131b2e；
1D 未增加全站 CSS。後續可獨立調整卡片密度、長商品關聯文字、雙欄內複核文字換行與留白，
須保留 Mock／缺漏提示及引用可操作性。本輪停止於 1D，不開始 UI 優化或 E 階段。
