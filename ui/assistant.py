"""AI 觀察、待追查事項與 RAG 智慧問答；外部套件：streamlit、google-genai、numpy（C 組檢索器）。"""

import streamlit as st

from services.c_integration import describe_mode, get_retriever
from services.data_service import get_insights, get_investigations
from services.rag_service import answer_rag_query
from ui.dashboard import PRIORITY_LABELS, STATUS_LABELS
from ui.evidence import evidence_button, render_insight_review


def render_assistant(dataset):
    st.caption("INSURRADAR / AI & RAG")
    st.title("AI 分析與 RAG 助手")
    st.info("問答會檢索目前快照並呼叫 Gemini；Mock 表示來源為模擬資料。引用檢查只確認來源可追溯，不代表語意支持性或人工複核已通過。每次問題獨立檢索，不解析前文指代。")
    if dataset["data_mode"] == "mock":
        st.caption("Mock Data／模擬觀察、來源與複核紀錄，不代表真實市場結論。")
    st.caption("展示完整快照，不套用競品比較或市場情報篩選。")
    res = st.session_state.get("gemini_dashboard_analysis")
    has_gemini = bool(res and res.get("status") == "success" and res.get("insights"))
    if has_gemini:
        source_mode = st.radio(
            "觀察卡片來源",
            options=["mock", "gemini"],
            format_func=lambda x: "📌 預設 Mock 快照觀察" if x == "mock" else f"✨ Gemini 智慧分析觀察（{len(res['insights'])} 則）",
            horizontal=True,
            key="assistant_insight_source_mode",
        )
        insights = res["insights"] if source_mode == "gemini" else get_insights(dataset)
    else:
        insights = get_insights(dataset)
    investigations = get_investigations(dataset)
    with st.container(key="assistant_layout"):
        left, right = st.columns([55, 45], gap="large")
    with left:
        st.subheader("AI 觀察卡片")
        if not insights:
            st.info("目前沒有觀察資料。")
        for insight in insights:
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
        st.subheader("待追查事項")
        if not investigations:
            st.info("目前沒有待追查事項。")
        priorities = {"high": 0, "medium": 1, "low": 2}
        for item in sorted(investigations, key=lambda value: (
            value["status"] == "resolved", priorities[value["priority"]]
        )):
            with st.container(border=True, key=f"investigation_card_{item['investigation_id']}"):
                st.write(f"**{item['title']}**")
                st.caption(f"{item['investigation_id']}｜優先級：{PRIORITY_LABELS[item['priority']]}｜"
                           f"{STATUS_LABELS[item['status']]}")
                st.write(item["reason"])
                st.caption("關聯商品 ID：" + ("、".join(item["product_ids"]) or "未提供商品關聯"))
                st.caption("待補欄位：" + ("、".join(item["missing_fields"]) or "未列出"))
                st.write("建議處理：" + item["suggested_action"])
                st.caption("引用證據 ID：" + ("、".join(item["evidence_ids"]) or "未提供引用"))
                evidence_button(dataset, item["evidence_ids"], source_kind="investigation",
                                source_id=item["investigation_id"], label="檢視待查證據")
    with right:
        st.subheader("AI 智慧問答 (RAG)")
        st.caption("支援自然語言提問，由 Retriever 檢索相關依據片段，Gemini 生成附可追溯引用的解答。")

        st.session_state.setdefault("rag_chat_messages", [])

        top_bar = st.columns([3, 1])
        with top_bar[1]:
            if st.button("清空對話", key="btn_clear_rag_chat", icon=":material/delete:"):
                st.session_state["rag_chat_messages"] = []
                st.rerun()

        # 快速範例問題捷徑
        sample_questions = [
            "模擬甲人壽一號商品宣告利率為何？",
            "哪些保險商品提供 6 年期繳費方案？",
            "各家美元保單投保年齡限制為何？",
        ]
        with st.expander("💡 快速範例問題捷徑", expanded=not bool(st.session_state["rag_chat_messages"])):
            st.caption("點擊下方問題可直接發起檢索與問答：")
            q_cols = st.columns(len(sample_questions))
            selected_sample = None
            for idx, q_text in enumerate(sample_questions):
                if q_cols[idx].button(q_text, key=f"btn_sample_q_{idx}"):
                    selected_sample = q_text

        # 繪製歷史對話容器
        if not st.session_state["rag_chat_messages"]:
            with st.container(border=True):
                st.write("**歡迎使用 InsurRadar 智慧問答**")
                st.caption("您可於下方輸入框詢問關於各公司宣告利率、繳費年期、投保規則或市場情報。")
                st.caption("有效引用可透過彈窗追溯原文；資料不足時會明確回覆，結論仍須人工複核。")

        for idx, msg in enumerate(st.session_state["rag_chat_messages"]):
            with st.chat_message(msg["role"]):
                if msg["role"] == "user":
                    st.write(msg["content"])
                else:
                    status = msg.get("status", "success")
                    if status == "validation_failed":
                        st.error(f"⚠️ 回答未通過引用驗證：{msg.get('error_message')}")
                    elif status == "error":
                        st.error(msg["content"])
                    else:
                        st.write(msg["content"])
                        if status == "success":
                            st.caption("Gemini 生成回答｜引用存在及檢索範圍檢查通過；語意支持性未驗證，AI 結論待人工複核。")
                        if msg.get("is_mock"):
                            st.caption("模擬資料回答：依據 Mock 快照檢索結果生成，未經真實市場投保驗證。")
                        citations = msg.get("citations", [])
                        if citations:
                            st.caption("依據引用：" + "、".join(citations))
                            evidence_button(
                                dataset, citations,
                                source_kind="rag_answer",
                                source_id=f"msg_{idx}",
                                label="檢視回答依據",
                            )
                        retrieved = msg.get("retrieved_evidence", [])
                        if retrieved:
                            with st.expander(f"檢索到的依據片段（共 {len(retrieved)} 筆）"):
                                if msg.get("retrieval_mode"):
                                    st.caption(msg["retrieval_mode"])
                                for r_item in retrieved:
                                    st.caption(f"[{r_item['evidence_id']}] 《{r_item['source_title']}》（相關度: {r_item['score']}）")
                                    st.write(r_item["excerpt"])

        # 輸入與處理
        chat_query = st.chat_input("請輸入關於商品、宣告利率或市場情報之問題...", key="assistant_rag_chat_input")
        active_query = selected_sample or chat_query

        if active_query:
            st.session_state["rag_chat_messages"].append({
                "role": "user",
                "dataset_id": dataset["dataset_id"],
                "content": active_query,
            })
            with st.spinner("正在檢索相關保單與證據，並由 Gemini 生成解答..."):
                retriever = get_retriever(dataset)
                result = answer_rag_query(active_query, dataset, retriever=retriever)
                st.session_state["rag_chat_messages"].append({
                    "role": "assistant",
                    "dataset_id": dataset["dataset_id"],
                    "created_at": result.get("created_at"),
                    "product_ids": sorted({pid for item in result.get("retrieved_evidence", [])
                                           for pid in item.get("product_ids", [])}),
                    "content": result["answer"],
                    "citations": result.get("citations", []),
                    "retrieved_evidence": result.get("retrieved_evidence", []),
                    "status": result.get("status", "success"),
                    "verification_passed": result.get("verification_passed", False),
                    "error_message": result.get("error_message"),
                    "is_mock": result.get("is_mock", True),
                    "retrieval_mode": describe_mode(retriever),
                })
            st.rerun()

