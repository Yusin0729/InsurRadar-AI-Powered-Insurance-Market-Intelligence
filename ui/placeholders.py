"""保留舊模組匯入的相容入口；正式頁面已分離，外部套件依頁面使用 streamlit。"""

from ui.assistant import render_assistant
from ui.market import render_market


__all__ = ["render_market", "render_assistant"]
