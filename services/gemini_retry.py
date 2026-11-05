"""Gemini 暫時性錯誤（503 忙碌、429 額度、500/504）的短暫重試與中文提示。

給 rag_service（問答）與 llm_service（AI 分析）共用：線上互動只多等幾秒，
仍失敗時回傳使用者看得懂的中文訊息，原始錯誤另存於 error_message 供除錯。
外部套件：無。
"""

from __future__ import annotations

import time
from typing import Any, Callable

TRANSIENT_CODES = (429, 500, 503, 504)
_TRANSIENT_MARKERS = ("429", "500", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE",
                      "high demand", "overloaded", "DEADLINE_EXCEEDED", "INTERNAL")
ONLINE_DELAYS = (1.5, 3.0)   # 線上互動：最多再試 2 次，總等待約 4.5 秒

BUSY_MESSAGE = "Gemini 服務目前忙碌或已達使用上限，暫時無法產生回答。請稍候幾秒再問一次。"


def is_transient(err: BaseException) -> bool:
    """依錯誤碼或訊息判斷是否為暫時性錯誤；金鑰錯誤、格式錯誤等不重試。"""
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        code = getattr(err, "code", None) or getattr(err, "status_code", None)
        if isinstance(code, int) and code in TRANSIENT_CODES:
            return True
        if any(marker in str(err) for marker in _TRANSIENT_MARKERS):
            return True
        err = err.__cause__
    return False


def with_retry(call: Callable[[], Any], *, delays: tuple[float, ...] = ONLINE_DELAYS,
               sleep: Callable[[float], None] = time.sleep) -> Any:
    """執行 call；暫時性錯誤時依 delays 等待後重試，其餘錯誤立即拋出。"""
    for delay in (*delays, None):
        try:
            return call()
        except Exception as err:
            if delay is None or not is_transient(err):
                raise
            sleep(delay)
    raise AssertionError("unreachable")


def friendly_message(err: BaseException, prefix: str) -> str:
    """暫時性錯誤回中文提示；其他錯誤保留原訊息（多為設定問題，需要看到細節）。"""
    return BUSY_MESSAGE if is_transient(err) else f"{prefix}{err}"
