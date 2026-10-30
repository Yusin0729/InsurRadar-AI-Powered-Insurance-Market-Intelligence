"""InsurRadar 應用入口、四頁導覽與共用證據視窗；外部套件：streamlit、jsonschema（資料服務）。"""

import streamlit as st

from ui.common import load_app_dataset, render_shell, render_sidebar_filters
from ui.comparison import render_comparison
from ui.dashboard import render_dashboard
from ui.evidence import begin_evidence_run, render_requested_evidence
from ui.market import render_market
from ui.assistant import render_assistant
from ui.report import render_report_export


def main():
    st.set_page_config(page_title="InsurRadar｜保險競品情報", page_icon="📡", layout="wide",
                       initial_sidebar_state="auto")
    begin_evidence_run()
    dataset, notices = load_app_dataset()
    render_shell(dataset, notices)
    # 共用篩選元件由入口執行，跨頁保留相同的 widget 身分。
    render_sidebar_filters(dataset)
    page = st.navigation([
        st.Page(lambda: render_dashboard(dataset), title="總覽儀表板", icon=":material/dashboard:", url_path="dashboard"),
        st.Page(lambda: render_comparison(dataset), title="競品比較庫",
                icon=":material/compare_arrows:", url_path="comparison", default=True),
        st.Page(lambda: render_market(dataset), title="市場情報追蹤", icon=":material/insights:", url_path="market"),
        st.Page(lambda: render_assistant(dataset), title="AI 分析與 RAG 助手",
                icon=":material/psychology:", url_path="assistant"),
    ])
    page.run()
    render_report_export(dataset)
    render_requested_evidence(dataset)


if __name__ == "__main__":
    main()
