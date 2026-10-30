"""共用 Markdown 匯出入口；外部套件：streamlit。報告內容由 Report Service 產生。"""

from datetime import datetime, timedelta, timezone
import re

import streamlit as st

from services.report_service import build_markdown_report, report_fingerprint, report_state


def render_report_export(dataset):
    """在頁面完成後讀取狀態；下載不 rerun，不變更聊天、篩選與證據要求。"""
    state = report_state(st.session_state)
    fingerprint = report_fingerprint(dataset, state)
    prepared = st.session_state.get("report_prepared")
    if prepared and prepared["fingerprint"] != fingerprint:
        st.session_state.pop("report_prepared", None)
        prepared = None
    with st.sidebar.expander("分析報告匯出"):
        st.caption("商品採目前比較條件與指定比較商品；市場情報採獨立篩選。AI／RAG 僅收錄可確認屬於目前快照及商品範圍的既有結果。")
        st.caption(f"商品月份：{state.get('filter_period_month') or '未指定'}｜情報分類：{state.get('market_category') or '不限'}｜情報日期：{state.get('market_date_from') or '不限'} ～ {state.get('market_date_to') or '不限'}")
        st.caption("不重新呼叫 Gemini。條件或生成結果變更後，請重新準備報告。")
        if st.button("準備 Markdown 報告", key="report_prepare", type="primary"):
            try:
                now = datetime.now(timezone(timedelta(hours=8)))
                report = build_markdown_report(dataset, state, generated_at=now.isoformat())
                safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", dataset["dataset_id"])
                prepared = {"fingerprint": fingerprint, "data": report["markdown"].encode("utf-8"),
                            "filename": f"InsurRadar_{safe_id}_{now:%Y%m%d_%H%M%S}.md",
                            "warnings": report["warnings"], "counts": report["counts"]}
                st.session_state["report_prepared"] = prepared
            except (ValueError, TypeError, KeyError) as err:
                st.error(f"報告未完成，請檢查資料或篩選條件：{err}")
        if prepared:
            counts = prepared["counts"]
            st.caption(f"商品 {counts['products']} 筆｜觀察 {counts['insights']} 筆｜RAG 回答 {counts['rag_answers']} 筆｜情報 {counts['news']} 筆")
            for warning in prepared["warnings"]:
                st.warning(warning)
            st.download_button("下載 UTF-8 Markdown", data=prepared["data"],
                               file_name=prepared["filename"], mime="text/markdown; charset=utf-8",
                               key="report_download", on_click="ignore")
