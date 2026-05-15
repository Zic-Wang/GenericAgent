"""Channel send/error observation helpers for GA Feishu integration.

This module is side-effect free. It classifies SendResult-like objects from
``GAFeishuChannelAdapter`` or ``lark_oapi.channel`` into stable GA-facing error
observations so frontends can decide retry/degrade/user-visible notices.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from frontends.feishu_channel_adapter import GAChannelSendResult


RETRYABLE_CODES = {"rate_limited", "send_timeout", "not_connected", "temporary_unavailable"}
USER_VISIBLE_CODES = {"permission_denied", "target_revoked", "ssrf_blocked", "format_error", "upload_failed", "download_failed"}
FATAL_CODES = {"permission_denied", "target_revoked", "ssrf_blocked"}


@dataclass(frozen=True)
class ChannelErrorObservation:
    success: bool
    code: Optional[str] = None
    hint: Optional[str] = None
    retryable: bool = False
    user_visible: bool = False
    severity: str = "info"
    message_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def should_retry(self) -> bool:
        return self.retryable and not self.success

    def to_user_message(self) -> Optional[str]:
        if self.success or not self.user_visible:
            return None
        hint = f": {self.hint}" if self.hint else ""
        return f"飞书发送失败 [{self.code or 'unknown'}]{hint}"


class ChannelErrorObserver:
    """Classify and record channel send results/errors."""

    def __init__(self) -> None:
        self.events: list[ChannelErrorObservation] = []

    def observe_result(self, result: Any) -> ChannelErrorObservation:
        obs = self.classify_result(result)
        self.events.append(obs)
        return obs

    def observe_exception(self, exc: BaseException) -> ChannelErrorObservation:
        obs = self.classify_error(exc)
        self.events.append(obs)
        return obs

    @staticmethod
    def classify_result(result: Any) -> ChannelErrorObservation:
        success = bool(getattr(result, "success", False))
        message_id = getattr(result, "message_id", None)
        raw = getattr(result, "raw", None) or {}
        if success:
            return ChannelErrorObservation(success=True, message_id=message_id, raw=_ensure_dict(raw))
        err = getattr(result, "error", None)
        if err is None and isinstance(result, GAChannelSendResult):
            code = result.error_code
            hint = result.error_hint
        else:
            code = _error_code(err)
            hint = _error_hint(err)
        return ChannelErrorObserver._build(False, code, hint, message_id=message_id, raw=_ensure_dict(raw))

    @staticmethod
    def classify_error(exc: BaseException) -> ChannelErrorObservation:
        code = _error_code(exc) or exc.__class__.__name__
        hint = _error_hint(exc) or str(exc)
        return ChannelErrorObserver._build(False, code, hint, raw={"exception": exc.__class__.__name__})

    @staticmethod
    def _build(success: bool, code: Optional[str], hint: Optional[str], *, message_id: Optional[str] = None, raw: Optional[Dict[str, Any]] = None) -> ChannelErrorObservation:
        norm = (str(code).lower() if code else None)
        retryable = norm in RETRYABLE_CODES
        user_visible = norm in USER_VISIBLE_CODES or not retryable
        severity = "error" if norm in FATAL_CODES else "warning" if retryable or not success else "info"
        return ChannelErrorObservation(
            success=success,
            code=norm,
            hint=hint,
            retryable=retryable,
            user_visible=user_visible,
            severity=severity,
            message_id=message_id,
            raw=raw or {},
        )

    def recent_failures(self, limit: int = 20) -> list[ChannelErrorObservation]:
        return [e for e in self.events if not e.success][-limit:]


def _error_code(err: Any) -> Optional[str]:
    if err is None:
        return None
    for name in ("code", "error_code", "type"):
        val = getattr(err, name, None)
        if val is not None:
            return _enum_or_str(val)
    if isinstance(err, dict):
        for name in ("code", "error_code", "type"):
            if err.get(name) is not None:
                return _enum_or_str(err.get(name))
    send_error = getattr(err, "send_error", None)
    if send_error is not None:
        return _error_code(send_error)
    return None


def _error_hint(err: Any) -> Optional[str]:
    if err is None:
        return None
    for name in ("hint", "message", "msg", "detail"):
        val = getattr(err, name, None)
        if val:
            return str(val)
    if isinstance(err, dict):
        for name in ("hint", "message", "msg", "detail"):
            if err.get(name):
                return str(err.get(name))
    return str(err) if str(err) else None


def _enum_or_str(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _ensure_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["ChannelErrorObservation", "ChannelErrorObserver"]
