"""共用資料載入、側邊欄與篩選狀態；外部套件：streamlit。"""

from datetime import datetime
from pathlib import Path
import warnings

import streamlit as st

from services.data_service import (
    DatasetLoadError, DatasetWarning, get_declared_rates, get_products, load_dataset,
)
from ui.evidence import close_evidence_dialog


FILTER_DEFAULTS = {
    "filter_company_ids": [],
    "filter_payment_term_years": None,
    "filter_issue_age_enabled": False,
    "filter_issue_age": 30,
    "filter_keyword": "",
    "filter_period_month": None,
}

# 原生 st.logo 會顯示於導覽上方；SVG 僅包含固定品牌圖文，不載入外部素材。
SIDEBAR_BRAND = """<svg xmlns="http://www.w3.org/2000/svg" width="238" height="56" viewBox="0 0 238 56">
<title>InsurRadar — Insurance Intelligence</title>
<g transform="translate(23 28)" fill="none" stroke="#42CB83" stroke-width="1.6">
<circle r="19"/><circle r="12" opacity=".65"/><circle r="5" opacity=".5"/>
<path d="M0 0 14-14M-19 0H19M0-19V19" opacity=".7"/>
<circle cx="9" cy="-9" r="2.8" fill="#42CB83" stroke="none"/>
</g>
<text x="54" y="27" fill="#F2FAF5" font-family="Arial, sans-serif" font-size="25" font-weight="700">InsurRadar</text>
<text x="55" y="45" fill="#B8D7C9" font-family="Arial, sans-serif" font-size="12">Insurance Intelligence</text>
</svg>"""


def load_app_dataset():
    """僅經由既有服務載入及驗證；錯誤時停止，不降級為其他快照。"""
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", DatasetWarning)
            dataset = load_dataset()
    except DatasetLoadError as error:
        st.error("資料快照無法載入，請檢查資料檔與驗證結果。")
        st.code(str(error), language=None)
        st.stop()
    notices = [str(item.message) for item in captured if issubclass(item.category, DatasetWarning)]
    return dataset, notices


def format_term(value):
    return "不限年期" if value is None else ("躉繳" if value == 0 else f"{value} 年繳費")


def reset_filters():
    """按鈕 callback 於元件建立前還原篩選與商品選取。"""
    for key, value in FILTER_DEFAULTS.items():
        st.session_state[key] = list(value) if isinstance(value, list) else value
    for key in ("selected_product_id", "_selected_product_id", "selected_evidence_ids"):
        st.session_state.pop(key, None)
    st.session_state["comparison_product_ids"] = []
    st.session_state.pop("_comparison_product_ids", None)


def render_shell(dataset, notices):
    """所有主頁共用的品牌、Mock 標示與快照基準時間。"""
    st.html(Path(__file__).with_name("styles.css"))
    st.logo(SIDEBAR_BRAND, size="large", icon_image=":material/radar:")
    if st.session_state.get("active_dataset_id") != dataset["dataset_id"]:
        reset_filters()
        close_evidence_dialog()
        st.session_state.pop("rag_chat_messages", None)
        st.session_state.pop("gemini_dashboard_analysis", None)
        st.session_state.pop("report_prepared", None)
        # 1D 狀態只在快照變更時失效；一般切頁及開關彈窗均保留。
        for key in list(st.session_state):
            if key.startswith(("market_", "_market_", "assistant_", "_assistant_")):
                st.session_state.pop(key, None)
        st.session_state["active_dataset_id"] = dataset["dataset_id"]
    with st.sidebar:
        if dataset["data_mode"] == "mock":
            st.warning("Mock Data／模擬資料")
        else:
            st.info("真實資料快照")
        st.caption(f"資料集：{dataset['dataset_id']}")
        st.caption(f"資料基準：{datetime.fromisoformat(dataset['as_of']).isoformat(sep=' ')}")
    st.caption("**Mock Data／模擬資料展示** · 非真實市場資訊或投保建議"
               if dataset["data_mode"] == "mock" else "真實資料快照 · 完整性不代表已人工確認結論")
    description, issues = st.columns(2)
    with description.expander("資料使用說明"):
        if dataset["data_mode"] == "mock":
            st.warning(dataset["mock_notice"])
        else:
            st.info("目前載入真實資料快照；資料完整性不代表已人工確認結論。")
    if notices:
        with issues.expander(f"資料待補／複核提示（{len(notices)} 項）"):
            for notice in notices:
                st.write(notice)



def render_sidebar_filters(dataset):
    products = get_products(dataset)
    companies = {item["company_id"]: item["company_name"] for item in products}
    terms = sorted({rule["payment_term_years"] for item in products for rule in item["eligibility_rules"]})
    months = sorted({item["period_month"] for item in get_declared_rates(dataset)}, reverse=True)
    for key, value in FILTER_DEFAULTS.items():
        st.session_state.setdefault(key, list(value) if isinstance(value, list) else value)
    st.session_state["filter_company_ids"] = [
        value for value in st.session_state["filter_company_ids"] if value in companies
    ]
    if st.session_state["filter_payment_term_years"] not in terms:
        st.session_state["filter_payment_term_years"] = None
    if st.session_state["filter_period_month"] not in months:
        st.session_state["filter_period_month"] = months[0] if months else None
    with st.sidebar.expander("比較條件", expanded=True):
        st.caption("條件僅套用於競品比較庫；總覽與助手顯示完整快照，市場情報使用頁內獨立篩選。")
        st.multiselect("保險公司", list(companies), format_func=companies.get,
                       key="filter_company_ids", placeholder="不限公司（可複選）",
                       help="未選公司表示不限公司。")
        st.selectbox("繳費年期", [None, *terms], format_func=format_term,
                     key="filter_payment_term_years", placeholder="不限年期")
        st.checkbox("啟用投保年齡篩選", key="filter_issue_age_enabled")
        st.number_input("投保年齡（歲）", min_value=0, step=1,
                        key="filter_issue_age", disabled=not st.session_state["filter_issue_age_enabled"])
        st.text_input("商品名稱關鍵字", key="filter_keyword",
                      help="沿用資料服務搜尋；亦可命中商品 ID、公司、保障特色及通路。")
        st.selectbox("宣告利率月份", months or [None], key="filter_period_month",
                     format_func=lambda value: value or "無月份資料", placeholder="無月份資料",
                     disabled=not months)
        st.button("重設篩選", on_click=reset_filters, width="stretch")
        st.caption("年齡與年期須命中同一條投保規則；僅供初步篩選，仍須核對其他承保條件。")


def get_filtered_products(dataset):
    """所有商品條件由 1A 服務處理，不在 UI 重寫篩選規則。"""
    return get_products(
        dataset,
        company_ids=st.session_state["filter_company_ids"] or None,
        payment_term_years=st.session_state["filter_payment_term_years"],
        issue_age=(st.session_state["filter_issue_age"]
                   if st.session_state["filter_issue_age_enabled"] else None),
        keyword=st.session_state["filter_keyword"],
    )
