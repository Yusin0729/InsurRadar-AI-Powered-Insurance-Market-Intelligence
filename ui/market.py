"""市場情報篩選、快照摘要與來源檢視；外部套件：streamlit。"""

import streamlit as st

from services.data_service import get_news, get_products
from ui.dashboard import CATEGORY_LABELS, ordered_news
from ui.evidence import evidence_button


MARKET_DEFAULTS = {
    "market_keyword": "", "market_category": None, "market_company_ids": [],
    "market_product_ids": [], "market_date_from": None, "market_date_to": None,
}


def reset_market_filters():
    """只重設情報條件，不影響競品比較。"""
    for key, value in MARKET_DEFAULTS.items():
        st.session_state[key] = list(value) if isinstance(value, list) else value
        st.session_state.pop("_" + key, None)


def save_market_filter(key):
    st.session_state[key] = st.session_state["_" + key]


def filtered_news(dataset, *, keyword="", **filters):
    """篩選交由既有服務；關鍵字只比對回傳的標題、發布者與摘要。"""
    news = get_news(dataset, **filters)
    term = keyword.strip().casefold()
    return [item for item in news if not term or term in " ".join([
        item["title"], item["publisher"] or "", *[point["text"] for point in item["summary_points"]],
    ]).casefold()]


def render_market_filters(products):
    companies = {item["company_id"]: item["company_name"] for item in products}
    names = {item["product_id"]: f"{item['product_name']}｜{item['product_id']}" for item in products}
    for key, value in MARKET_DEFAULTS.items():
        st.session_state.setdefault(key, list(value) if isinstance(value, list) else value)
    for key, options in (("market_company_ids", companies), ("market_product_ids", names)):
        st.session_state[key] = [value for value in st.session_state[key] if value in options]
    if st.session_state["market_category"] not in CATEGORY_LABELS:
        st.session_state["market_category"] = None
    # widget 切頁後會被清除；無底線狀態保留選取，建立元件前還原。
    for key in MARKET_DEFAULTS:
        st.session_state["_" + key] = st.session_state[key]
    with st.container(border=True, key="market_filters"):
        st.text_input("情報關鍵字", key="_market_keyword", on_change=save_market_filter,
                      args=("market_keyword",), help="搜尋標題、發布者與既有摘要。")
        category, company, product = st.columns(3)
        category.selectbox("新聞分類", [None, *CATEGORY_LABELS], key="_market_category",
                           placeholder="全部分類",
                           format_func=lambda value: CATEGORY_LABELS.get(value, "全部分類"),
                           on_change=save_market_filter, args=("market_category",))
        company.multiselect("情報相關公司", list(companies), key="_market_company_ids",
                            format_func=companies.get, on_change=save_market_filter,
                            args=("market_company_ids",), placeholder="不限公司")
        product.multiselect("情報相關商品", list(names), key="_market_product_ids",
                            format_func=names.get, on_change=save_market_filter,
                            args=("market_product_ids",), placeholder="不限商品")
        start, end = st.columns(2)
        start.date_input("發布日期起日", value=None, key="_market_date_from",
                         on_change=save_market_filter, args=("market_date_from",))
        end.date_input("發布日期迄日", value=None, key="_market_date_to",
                       on_change=save_market_filter, args=("market_date_to",))
        st.caption("日期可留空；依快照時區篩選，包含起迄日。指定任一日期後，日期未知的情報不視為符合。"
                   "各條件取交集，同一公司或商品清單內取聯集。")
        st.button("重設情報篩選", key="market_reset", on_click=reset_market_filters)


def render_market_card(dataset, item, companies, products):
    with st.container(border=True, key=f"news_card_{item['news_id']}"):
        st.subheader(item["title"])
        st.caption(f"{CATEGORY_LABELS[item['category']]}｜{item['publisher'] or '發布者待補'}｜"
                   f"{item['published_at'] or '發布日期待確認'}")
        if not item["summary_points"]:
            st.info("新聞摘要待補。")
        for position, point in enumerate(item["summary_points"], 1):
            st.write(f"{position}. {point['text']}")
            with st.expander(f"摘要 {position} 引用與證據"):
                st.caption("摘要引用：" + ("、".join(point["evidence_ids"]) or "未提供引用"))
                evidence_button(dataset, point["evidence_ids"], source_kind="news_summary",
                                source_id=f"{item['news_id']}_{position}", label=f"檢視摘要 {position} 證據")
        with st.expander("商品關聯與引用明細"):
            st.caption(f"情報 ID：{item['news_id']}")
            st.caption("相關公司：" + ("、".join(companies.get(cid, cid) for cid in item["company_ids"])
                                       or "未提供公司關聯"))
            st.write("相關商品：" + ("、".join(products.get(pid, pid) for pid in item["product_ids"])
                                     or "未提供商品關聯"))
            st.caption("情報引用：" + ("、".join(item["evidence_ids"]) or "未提供引用"))
        evidence_button(dataset, item["evidence_ids"], source_kind="news",
                        source_id=item["news_id"], label="檢視情報來源")
        if item["source_url"]:
            st.link_button("開啟新聞來源網址", item["source_url"])
        else:
            st.caption("新聞來源網址待補；可透過證據檢視已有來源資訊。")


def render_market(dataset):
    st.caption("INSURRADAR / MARKET INTELLIGENCE")
    st.title("市場情報追蹤")
    st.info("本頁使用獨立情報篩選，不受側邊欄競品比較條件影響。呈現快照中既有摘要，未執行新聞分析。")
    if dataset["data_mode"] == "mock":
        st.caption("Mock Data／模擬情報，非真實市場新聞。")
    products = get_products(dataset)
    render_market_filters(products)
    start, end = st.session_state["market_date_from"], st.session_state["market_date_to"]
    if start and end and start > end:
        st.warning("發布日期起日不可晚於迄日，請調整日期範圍。")
        return
    news = filtered_news(
        dataset, keyword=st.session_state["market_keyword"], category=st.session_state["market_category"],
        company_ids=st.session_state["market_company_ids"] or None,
        product_ids=st.session_state["market_product_ids"] or None, date_from=start, date_to=end,
    )
    st.caption(f"符合條件：{len(news)} 則｜資料基準：{dataset['as_of']}")
    if not news:
        st.info("沒有符合條件的市場情報；請調整或重設情報篩選。")
        return
    companies = {item["company_id"]: item["company_name"] for item in products}
    names = {item["product_id"]: f"{item['product_name']}（{item['product_id']}）" for item in products}
    dated, undated = ordered_news(news)
    for item in dated:
        render_market_card(dataset, item, companies, names)
    if undated:
        st.subheader("發布日期待確認")
        for item in undated:
            render_market_card(dataset, item, companies, names)
