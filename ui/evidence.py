"""共用證據對話框、引用狀態與獨立生命週期；外部套件：streamlit、jsonschema（既有驗證）。"""

import streamlit as st

from services.data_service import get_evidence, get_insights, load_schema
from services.validation import validate_dataset, validate_insight_evidence


DIALOG_KEYS = ("evidence_dialog_context", "active_evidence_id", "_evidence_choice_id", "_evidence_open_requested")
REVIEW_LABELS = {"confirmed": "已確認", "unreviewed": "待複核", "rejected": "已退回"}
SOURCE_LABELS = {"mock": "Mock／模擬來源", "official_page": "官方網頁", "pdf": "PDF 文件", "news": "新聞"}
GAP_LABELS = {
    "source_title": "文件標題", "source_date": "來源日期", "document_version": "文件版本",
    "excerpt": "原文片段", "source_url|local_path": "來源網址或檔案識別",
    "page": "頁碼", "page|section": "頁碼或章節", "captured_at": "擷取時間",
}


def close_evidence_dialog():
    """可作為原生關閉回呼；只清除證據狀態，保留商品篩選及選取。"""
    st.session_state.pop("evidence_dialog_context", None)
    st.session_state.pop("active_evidence_id", None)
    st.session_state.pop("_evidence_open_requested", None)


def begin_evidence_run():
    """入口每次完整執行時清除舊視窗；fragment 切換不會執行此函式。"""
    close_evidence_dialog()
    st.session_state.pop("_evidence_choice_id", None)


def evidence_button(dataset, evidence_ids, *, source_kind, source_id, label="檢視證據"):
    """只在當次點擊提出開窗要求，集中於入口呼叫一次 st.dialog。"""
    if st.button(label, key=f"evidence_open_{source_kind}_{source_id}",
                 icon=":material/fact_check:", type="primary"):
        st.session_state["evidence_dialog_context"] = {
            "dataset_id": dataset["dataset_id"], "source_kind": source_kind,
            "source_id": source_id, "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }
        st.session_state["_evidence_open_requested"] = True


def review_label(status, is_mock):
    label = REVIEW_LABELS.get(status, "資料待補")
    return f"模擬複核紀錄：{label}" if is_mock else label


def render_insight_review(insight, dataset):
    """四類狀態完全採用 1A 回報，不由證據的確認狀態推論結論。"""
    status = validate_insight_evidence(insight, dataset)
    st.caption("引用與完整性")
    st.write("引用存在／有效：" + ("全部有效" if status["references_valid"] else "引用缺漏或無效")
             + "  \n證據完整：" + ("完整" if status["evidence_complete"] else "待補"))
    st.caption("人工與結論複核")
    st.write("證據人工複核：" + (
        ("全數確認（模擬複核紀錄）" if status["is_mock"] else "全數確認")
        if status["all_evidence_human_confirmed"] else "尚未全數確認"
    ) + "  \nAI 結論複核：" + review_label(status["conclusion_review_status"], status["is_mock"]))
    if status["missing_evidence_ids"]:
        st.error("引用不存在：" + "、".join(status["missing_evidence_ids"]))
    for evidence_id, gaps in status["incomplete_evidence"].items():
        st.warning(f"證據待補 {evidence_id}：" + "、".join(GAP_LABELS.get(gap, gap) for gap in gaps))
    if status["is_mock"]:
        st.caption("Mock 觀察／模擬資料；複核紀錄亦為模擬，不代表真實市場已驗證結論。")
    elif status["can_display_as_verified"]:
        st.success("已驗證結論（符合既有引用、完整性與人工複核規則）")
    else:
        st.caption("尚不符合已驗證結論的顯示條件。")
    return status


def evidence_gaps(report, evidence_id):
    """只取用既有驗證回報，不在 UI 重寫證據完整性規則。"""
    return [item["message"] for item in report["warnings"]
            if item["code"] == "evidence_incomplete" and item["record_id"] == evidence_id]


def render_source_review(dataset, evidence_ids, report):
    records = [get_evidence(dataset, evidence_id) for evidence_id in evidence_ids]
    all_exist = bool(records) and all(record is not None for record in records)
    complete = all_exist and not report["errors"] and not any(evidence_gaps(report, eid) for eid in evidence_ids)
    confirmed = all_exist and all(
        record["human_review_status"] == "confirmed" and record["reviewed_by"] and record["reviewed_at"]
        for record in records
    )
    st.caption("引用與完整性")
    st.write("引用存在：" + ("全部存在" if all_exist else "未提供引用或有證據不存在"))
    st.write("證據完整：" + ("完整" if complete else "待補或尚無法確認"))
    st.caption("人工與結論複核")
    suffix = "（模擬複核紀錄）" if dataset["data_mode"] == "mock" else ""
    st.write("證據人工複核：" + ("全數確認" + suffix if confirmed else "尚未全數確認"))
    st.write("AI 結論複核：不適用（此處檢視來源資料）")


def save_active_evidence():
    st.session_state["active_evidence_id"] = st.session_state["_evidence_choice_id"]


@st.dialog("原始依據檢視", width="large", on_dismiss=close_evidence_dialog)
def evidence_dialog(dataset, context, report):
    if st.session_state.get("evidence_dialog_context") != context:
        return
    if dataset["data_mode"] == "mock":
        st.warning("Mock Data／模擬證據與模擬複核紀錄，非真實官方文件或市場驗證。")
    st.caption(f"來源：{context['source_kind']} / {context['source_id']}｜資料集：{dataset['dataset_id']}")
    evidence_ids = context["evidence_ids"]
    if not evidence_ids:
        st.info("未提供證據 ID，無法檢視原始依據。")
    else:
        records = {eid: get_evidence(dataset, eid) for eid in evidence_ids}
        if st.session_state.get("active_evidence_id") not in evidence_ids:
            st.session_state["active_evidence_id"] = evidence_ids[0]
        st.session_state["_evidence_choice_id"] = st.session_state["active_evidence_id"]
        evidence_id = st.selectbox(
            "切換證據", evidence_ids, key="_evidence_choice_id", on_change=save_active_evidence,
            format_func=lambda eid: f"{eid}｜{records[eid]['source_title'] if records[eid] else '證據不存在'}",
        )
        evidence = records[evidence_id]
        st.caption(f"證據 ID：{evidence_id}")
        if evidence is None:
            st.error(f"證據不存在：{evidence_id}。請確認引用 ID，或切換其他證據。")
        else:
            gaps = evidence_gaps(report, evidence_id)
            if gaps:
                for gap in gaps:
                    st.warning(gap)
            elif report["errors"]:
                st.warning("資料含驗證錯誤，無法確認證據完整性。")
            else:
                st.caption("本筆證據欄位完整；完整性不代表原文已支持觀察結論。")
            st.subheader(evidence["source_title"])
            with st.container(border=True, key="evidence_excerpt"):
                st.write("**原文片段**")
                st.text(evidence["excerpt"] or "原文片段待補，不能據此確認結論。")
            st.caption("來源資訊與文件定位")
            fields = (
                ("來源類型", SOURCE_LABELS.get(evidence["source_type"], evidence["source_type"])),
                ("來源日期", evidence["source_date"]), ("文件版本", evidence["document_version"]),
                ("頁碼", evidence["page"]), ("章節", evidence["section"]),
                ("人工複核狀態", review_label(evidence["human_review_status"], evidence["is_mock"])),
                ("複核者", evidence["reviewed_by"]), ("複核時間", evidence["reviewed_at"]),
            )
            st.write(f"來源類型：{fields[0][1]}｜來源日期：{evidence['source_date'] or '資料待補'}")
            with st.expander("文件定位與複核紀錄"):
                for label, value in fields[2:]:
                    st.write(f"{label}：{value if value is not None else '資料待補'}")
            if evidence["source_url"]:
                st.link_button("開啟來源網址", evidence["source_url"])
    st.divider()
    st.subheader("驗證與複核狀態")
    insight = next((item for item in get_insights(dataset) if item["insight_id"] == context["source_id"]), None
                   ) if context["source_kind"] == "insight" else None
    if insight is not None:
        st.write(insight["title"])
        render_insight_review(insight, dataset)
    else:
        if context["source_kind"] == "insight":
            st.error("對應的觀察已不存在，無法確認其結論複核狀態。")
        render_source_review(dataset, context["evidence_ids"], report)
    if st.button("關閉視窗", key="evidence_close"):
        close_evidence_dialog()
        st.rerun()


def render_requested_evidence(dataset):
    """消耗當次要求；完整重新執行不會靠舊 context 自動開窗。"""
    requested = st.session_state.pop("_evidence_open_requested", False)
    context = st.session_state.get("evidence_dialog_context")
    if requested and context and context["dataset_id"] == dataset["dataset_id"]:
        report = validate_dataset(dataset, load_schema())
        evidence_dialog(dataset, context, report)
