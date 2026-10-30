"""競品清單、指定月份比較與商品詳情；外部套件：streamlit。"""

import streamlit as st

from services.data_service import get_declared_rates
from ui.common import format_term, get_filtered_products
from ui.evidence import evidence_button


def display_text(value):
    return "資料待補" if value is None or value == "" else str(value)


def format_rule(rule):
    minimum = display_text(rule["min_issue_age"])
    maximum = display_text(rule["max_issue_age"])
    return f"{format_term(rule['payment_term_years'])}：{minimum}～{maximum} 歲"


def product_rows(products):
    return [{
        "商品 ID": item["product_id"], "保險公司": item["company_name"],
        "商品名稱": item["product_name"], "幣別": item["currency"],
        "繳費年期與投保年齡": "；".join(map(format_rule, item["eligibility_rules"])) or "資料待補",
        "保障特色": "；".join(item["benefits"]) or "資料待補",
        "通路": "；".join(item["channels"]) or "資料待補",
        "商品證據 ID": "、".join(item["evidence_ids"]) or "資料待補",
    } for item in products]


def comparison_rows(products, rates, period_month):
    """每一適用條件獨立一列，不合併利率、不推測條件或進行排名。"""
    rows = []
    for product in products:
        product_rates = [rate for rate in rates if rate["product_id"] == product["product_id"]
                         and rate["period_month"] == period_month]
        for rate in product_rates or [None]:
            value = rate["rate_percent"] if rate else None
            rows.append({
                "商品 ID": product["product_id"], "保險公司": product["company_name"],
                "商品名稱": product["product_name"], "幣別": product["currency"],
                "宣告利率月份": period_month or "無月份資料",
                "宣告利率": f"{value:.2f}%" if value is not None else "資料不足",
                "資料狀態": ("有數值，須核對適用條件" if value is not None else
                         ("該月份利率數值缺漏" if rate else "該月份無利率紀錄")),
                "適用條件": display_text(rate["applicable_conditions"]) if rate else "資料待補",
                "生效起日": display_text(rate["effective_from"]) if rate else "資料待補",
                "生效迄日": display_text(rate["effective_to"]) if rate else "資料待補",
                "利率紀錄 ID": rate["rate_id"] if rate else "無紀錄",
                "利率證據 ID": "、".join(rate["evidence_ids"]) if rate and rate["evidence_ids"] else "資料待補",
            })
    return rows


def save_selection(widget_key, state_key):
    st.session_state[state_key] = st.session_state[widget_key]


def render_rate_table(products, rates, period_month):
    """優先顯示月份、利率與條件，識別碼仍保留在表格資料中。"""
    st.dataframe(
        comparison_rows(products, rates, period_month), hide_index=True, width="stretch",
        column_order=["商品名稱", "宣告利率月份", "宣告利率", "適用條件", "資料狀態",
                      "生效起日", "生效迄日", "保險公司", "幣別", "商品 ID", "利率紀錄 ID", "利率證據 ID"],
        column_config={
            "商品名稱": st.column_config.TextColumn(width="medium"),
            "宣告利率月份": st.column_config.TextColumn(width="small"),
            "宣告利率": st.column_config.TextColumn(width="small"),
            "適用條件": st.column_config.TextColumn(width="large"),
        },
    )


def sync_selection(product_ids):
    """持久狀態只保存 ID；widget 狀態分開，避免跨頁清理造成遺失。"""
    if "selected_product_id" not in st.session_state:
        st.session_state["selected_product_id"] = product_ids[0] if product_ids else None
    if st.session_state["selected_product_id"] not in product_ids:
        st.session_state["selected_product_id"] = None
    st.session_state["comparison_product_ids"] = [
        value for value in st.session_state.get("comparison_product_ids", []) if value in product_ids
    ]
    st.session_state["_selected_product_id"] = st.session_state["selected_product_id"]
    st.session_state["_comparison_product_ids"] = st.session_state["comparison_product_ids"]
    if st.session_state["selected_product_id"] is None:
        st.session_state["selected_evidence_ids"] = []


def render_product_detail(product, rates, period_month, dataset):
    st.subheader("商品詳細資訊")
    st.write(f"**{product['product_name']}**")
    st.caption(f"商品 ID：{product['product_id']}｜{product['company_name']}｜{product['currency']}")
    left, right = st.columns(2)
    left.write("**保障特色**")
    left.write("；".join(product["benefits"]) or "資料待補")
    right.write("**銷售通路**")
    right.write("；".join(product["channels"]) or "資料待補")
    st.write(f"上市日期：{display_text(product['launched_on'])}")
    st.write(f"停售日期：{product['discontinued_on'] or '未提供（不據此推定仍在售）'}")
    st.write("**逐條投保規則**")
    rule_rows = [{
        "繳費年期": format_term(rule["payment_term_years"]),
        "最低投保年齡": display_text(rule["min_issue_age"]),
        "最高投保年齡": display_text(rule["max_issue_age"]),
        "額外條件": display_text(rule["conditions"]),
        "證據 ID": "、".join(rule["evidence_ids"]) or "資料待補",
    } for rule in product["eligibility_rules"]]
    if rule_rows:
        st.dataframe(rule_rows, hide_index=True, width="stretch")
    else:
        st.info("投保條件資料不足。")
    st.write(f"**指定月份宣告利率：{period_month or '無月份資料'}**")
    render_rate_table([product], rates, period_month)
    related_rates = [rate for rate in rates if rate["product_id"] == product["product_id"]
                     and rate["period_month"] == period_month]
    # 完整原文另以可換行文字呈現，避免表格截斷重要的適用限制。
    for rate in related_rates:
        st.write(f"適用條件（{rate['rate_id']}）：{display_text(rate['applicable_conditions'])}")
    evidence_ids = list(dict.fromkeys([
        *product["evidence_ids"],
        *(eid for rule in product["eligibility_rules"] for eid in rule["evidence_ids"]),
        *(eid for rate in related_rates for eid in rate["evidence_ids"]),
    ]))
    st.session_state["selected_evidence_ids"] = evidence_ids
    st.caption("關聯證據 ID：" + ("、".join(evidence_ids) or "資料待補"))
    evidence_button(dataset, evidence_ids, source_kind="product", source_id=product["product_id"],
                    label="檢視商品與利率證據")
    st.caption("引用存在與模擬複核紀錄，不代表真實市場已驗證結論。")


def render_comparison(dataset):
    st.caption("INSURRADAR / PRODUCT COMPARISON")
    st.title("競品比較庫")
    st.write("美元利率變動型終身壽險 · 商品條件與指定月份宣告利率")
    products = get_filtered_products(dataset)
    product_index = {item["product_id"]: item for item in products}
    product_ids = list(product_index)
    period_month = st.session_state["filter_period_month"]
    rates = get_declared_rates(dataset, product_ids=product_ids, period_month=period_month)
    sync_selection(product_ids)
    with st.container(key="kpis"):
        columns = st.columns(3)
        columns[0].metric("符合條件商品", len(products))
        columns[1].metric("保險公司", len({item["company_id"] for item in products}))
        columns[2].metric("宣告利率月份", period_month or "無月份資料")
    st.info("宣告利率不是保證報酬或 IRR。商品符合年期／年齡篩選，不代表該筆利率適用；"
            "請逐筆核對月份、生效期間與適用條件。本頁不提供利率排名。")
    if not products:
        st.info("沒有符合條件的商品，請調整或重設側邊欄篩選。")
        return
    st.subheader("商品清單")
    st.caption("每條年期與年齡規則分開呈現；未知年齡不視為符合投保條件。")
    st.dataframe(product_rows(products), hide_index=True, width="stretch",
                 column_order=["保險公司", "商品名稱", "幣別", "繳費年期與投保年齡",
                               "保障特色", "通路", "商品 ID", "商品證據 ID"],
                 column_config={"商品名稱": st.column_config.TextColumn(width="large"),
                                "繳費年期與投保年齡": st.column_config.TextColumn(width="large")})
    label = lambda value: f"{product_index[value]['product_name']}｜{value}"
    st.multiselect("選擇比較商品", product_ids, format_func=label,
                   key="_comparison_product_ids", on_change=save_selection,
                   args=("_comparison_product_ids", "comparison_product_ids"),
                   placeholder="全部符合條件商品（可指定）",
                   help="未指定時展示所有符合篩選條件的商品。")
    comparison_ids = st.session_state["comparison_product_ids"] or product_ids
    st.subheader("指定月份比較表")
    st.caption("每個商品的每筆適用條件獨立列示；比較表中的利率仍須人工核對是否適用目前選定的年期。")
    render_rate_table([product_index[value] for value in comparison_ids], rates, period_month)
    st.selectbox("選取商品查看詳情", product_ids, index=None, format_func=label,
                 placeholder="請選取商品", key="_selected_product_id", on_change=save_selection,
                 args=("_selected_product_id", "selected_product_id"))
    selected_id = st.session_state["selected_product_id"]
    if selected_id is not None:
        with st.container(border=True, key="product_detail"):
            render_product_detail(product_index[selected_id], rates, period_month, dataset)
    else:
        st.info("請選取商品查看詳情。先前選取若不符合目前條件，會自動清除。")
