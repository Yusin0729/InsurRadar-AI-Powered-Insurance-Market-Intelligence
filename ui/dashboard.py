"""完整快照 Dashboard、Mock 觀察與既有情報摘要；外部套件：streamlit。"""

from collections import Counter
from datetime import datetime

import streamlit as st

from services.data_service import get_insights, get_investigations, get_news, get_products
from services.llm_service import get_gemini_config, run_ai_analysis
from ui.evidence import evidence_button, render_insight_review


PRIORITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
STATUS_LABELS = {"open": "待處理", "in_review": "複核中", "resolved": "已結案"}
CATEGORY_LABELS = {"regulatory": "監理", "product": "商品", "interest_rate": "利率", "market": "市場"}


def dashboard_data(dataset):
    """讀取完整快照；不套用競品比較頁的任何篩選條件。"""
    products = get_products(dataset)
    insights = get_insights(dataset)
    news = get_news(dataset)
    investigations = get_investigations(dataset)
    company_names = {item["company_id"]: item["company_name"] for item in products}
    company_counts = Counter(item["company_id"] for item in products)
    return {
        "products": products, "insights": insights, "news": news, "investigations": investigations,
        "company_rows": [{"公司 ID": company_id, "保險公司": name, "商品數": company_counts[company_id]}
                         for company_id, name in company_names.items()],
        "kpis": {
            "保險公司數": len(company_names), "商品數": len(products), "觀察數": len(insights),
            "未結案待追查數": sum(item["status"] in {"open", "in_review"} for item in investigations),
        },
    }


def ordered_news(news):
    """只依真實存在的發佈時間排序，未知日期另外展示。"""
    dated = sorted((item for item in news if item["published_at"] is not None),
                   key=lambda item: datetime.fromisoformat(item["published_at"].upper()), reverse=True)
    undated = [item for item in news if item["published_at"] is None]
    return dated, undated


def render_news_item(dataset, item):
    with st.container(border=True, key=f"news_card_{item['news_id']}"):
        st.write(f"**{item['title']}**")
        st.caption(f"{CATEGORY_LABELS[item['category']]}｜{item['publisher'] or '發布者待補'}｜"
                   f"{item['published_at'] or '發佈日期待確認'}")
        for point in item["summary_points"]:
            st.write(point["text"])
            st.caption("摘要引用：" + ("、".join(point["evidence_ids"]) or "未提供引用"))
        evidence_button(dataset, item["evidence_ids"], source_kind="news", source_id=item["news_id"],
                        label="檢視情報來源")


def render_dashboard(dataset):
    data = dashboard_data(dataset)
    st.caption("INSURRADAR / OVERVIEW")
    st.title("總覽儀表板")
    st.write("保險市場競品情報總覽")
    st.info("統計範圍：完整資料快照；不受側邊欄競品比較條件影響。未結案包含待處理與複核中事項。")
    st.caption(f"資料基準：{dataset['as_of']}｜資料集：{dataset['dataset_id']}")
    with st.container(key="kpis"):
        for column, (label, value) in zip(st.columns(4), data["kpis"].items()):
            column.metric(label, value)
    with st.container(key="dashboard_layout"):
        main, side = st.columns([2, 1], gap="large")
    with main:
        st.subheader("AI 重點觀察")
        mode = st.radio(
            "觀察來源模式",
            options=["mock", "gemini"],
            format_func=lambda x: "📌 預設 Mock 快照觀察" if x == "mock" else "✨ Gemini 智慧分析",
            horizontal=True,
            key="dashboard_insight_mode",
            label_visibility="collapsed",
        )
        if mode == "mock":
            st.caption("Mock／預先撰寫的模擬觀察；未呼叫 LLM。")
            if not data["insights"]:
                st.info("目前沒有觀察資料。")
            for insight in data["insights"]:
                with st.container(border=True, key=f"insight_card_{insight['insight_id']}"):
                    st.write(f"**{insight['title']}**")
                    st.write(insight["statement"])
                    with st.expander("觀察追溯資訊"):
                        st.caption(f"觀察 ID：{insight['insight_id']}")
                        st.caption("關聯商品 ID：" + ("、".join(insight["product_ids"]) or "全域觀察"))
                        st.caption("引用證據 ID：" + ("、".join(insight["evidence_ids"]) or "未提供引用"))
                    render_insight_review(insight, dataset)
                    evidence_button(dataset, insight["evidence_ids"], source_kind="insight",
                                    source_id=insight["insight_id"], label="檢視觀察證據")
        else:
            res = st.session_state.get("gemini_dashboard_analysis")
            cfg = get_gemini_config()
            top_cols = st.columns([3, 1])
            with top_cols[0]:
                if res and res.get("status") == "success":
                    st.caption(f"✨ Gemini 即時分析完成（模型：`{res.get('model')}`｜生成時間：`{res.get('created_at', '')[:19].replace('T', ' ')} UTC`）")
                else:
                    st.caption(f"✨ 透過 Gemini 官方 SDK 交叉分析全市場商品規格、利率與依據原文（預設模型：`{cfg['model']}`）")
            with top_cols[1]:
                trigger_btn_label = "重新產生 AI 分析" if (res and res.get("status") == "success") else "產生 AI 分析"
                if st.button(trigger_btn_label, key="dashboard_btn_run_llm", type="primary", icon=":material/psychology:"):
                    with st.spinner("Gemini 分析進行中，正在進行多維度交叉驗證..."):
                        res = run_ai_analysis(dataset)
                        st.session_state["gemini_dashboard_analysis"] = {**res, "dataset_id": dataset["dataset_id"]}
                        st.rerun()

            if not res:
                with st.container(border=True):
                    st.write("**即時 AI 競品分析**")
                    st.write("點擊右上角「**產生 AI 分析**」按鈕，系統將呼叫 Gemini 模型產出結構化觀察，並自動進行 ID 存在性與數值一致性檢驗。")
                    if not cfg["api_key"]:
                        st.warning("⚠️ 系統尚未偵測到 `GEMINI_API_KEY`。請在 `.env` 或 Streamlit secrets 中設定金鑰後執行。")
                    else:
                        st.success("✅ GEMINI_API_KEY 已就緒，隨時可發起分析。")
            elif res.get("status") == "success":
                llm_insights = res.get("insights", [])
                if not llm_insights:
                    st.info("AI 分析未產生有效觀察項目。")
                for insight in llm_insights:
                    with st.container(border=True, key=f"gemini_card_{insight['insight_id']}"):
                        st.write(f"**{insight['title']}**")
                        st.write(insight["statement"])
                        with st.expander("觀察追溯資訊"):
                            st.caption(f"觀察 ID：{insight['insight_id']}（AI 即時分析產出）")
                            st.caption("關聯商品 ID：" + ("、".join(insight["product_ids"]) or "全域觀察"))
                            st.caption("引用證據 ID：" + ("、".join(insight["evidence_ids"]) or "未提供引用"))
                            st.caption(f"生成模式：{insight['generation_mode']}｜資料性質：{'模擬資料分析' if insight['is_mock'] else '真實資料分析'}")
                        render_insight_review(insight, dataset)
                        evidence_button(dataset, insight["evidence_ids"], source_kind="gemini_insight",
                                        source_id=insight["insight_id"], label="檢視觀察證據")
            elif res.get("status") == "insufficient_data":
                st.warning(f"資料不足：{res.get('error_message')}")
            else:
                st.error(f"AI 分析失敗：{res.get('error_message')}")
                if res.get("rejected_insights"):
                    with st.expander(f"查看因不合規而被拒絕的觀察（共 {len(res['rejected_insights'])} 項）"):
                        for rej in res["rejected_insights"]:
                            st.caption(f"原因：{', '.join(rej['reasons'])}")
                            st.code(json.dumps(rej["raw_item"], ensure_ascii=False, indent=2), language="json")
        st.subheader("最新市場情報摘要")
        st.caption("呈現快照中既有摘要，未執行新聞分析。")
        dated, undated = ordered_news(data["news"])
        if not data["news"]:
            st.info("目前沒有市場情報。")
        for item in dated[:5]:
            render_news_item(dataset, item)
        if undated:
            with st.expander(f"發佈日期待確認（{len(undated)} 則）"):
                for item in undated:
                    render_news_item(dataset, item)
    with side:
        st.subheader("商品與公司統計")
        if data["company_rows"]:
            st.dataframe(data["company_rows"], hide_index=True, width="stretch",
                         column_order=["保險公司", "商品數", "公司 ID"])
        else:
            st.info("目前沒有商品與公司資料。")
        st.subheader("待追查事項")
        if not data["investigations"]:
            st.info("目前沒有待追查事項。")
        priority_order = {"high": 0, "medium": 1, "low": 2}
        for item in sorted(data["investigations"], key=lambda value: (
            value["status"] == "resolved", priority_order[value["priority"]]
        )):
            with st.container(border=True, key=f"investigation_card_{item['investigation_id']}"):
                st.write(f"**{item['title']}**")
                st.caption(f"優先級：{PRIORITY_LABELS[item['priority']]}｜{STATUS_LABELS[item['status']]}")
                st.write(item["reason"])
                st.caption("待補欄位：" + ("、".join(item["missing_fields"]) or "未列出"))
                st.write("建議處理：" + item["suggested_action"])
                evidence_button(dataset, item["evidence_ids"], source_kind="investigation",
                                source_id=item["investigation_id"], label="檢視待查證據")
