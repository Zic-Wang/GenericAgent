"""Thin GA adapter over ``lark_oapi.channel.FeishuChannel``.

This module is intentionally small and side-effect free:

- It does not read project key files.
- It does not start WebSocket connections on import.
- It keeps FeishuChannel behind a GA-facing interface so the legacy
  ``frontends.fsapp`` implementation can migrate gradually.

The adapter is introduced as a skeleton in the Channel migration plan.
Later phases will wire approval storage, inbound normalization, streaming
cards, media bridge, error observer, and webhook mode on top of it.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from lark_oapi.channel import FeishuChannel
from lark_oapi.channel.types import SendResult

ChannelEventHandler = Callable[[Any], Any]


@dataclass(frozen=True)
class GAChannelSendResult:
    """Stable GA-facing send result independent of SDK internals."""

    success: bool
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_hint: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_sdk(cls, result: SendResult) -> "GAChannelSendResult":
        error = getattr(result, "error", None)
        error_code = getattr(error, "code", None) if error is not None else None
        error_hint = getattr(error, "hint", None) if error is not None else None
        if error_hint is None and error is not None:
            error_hint = repr(error)
        return cls(
            success=bool(getattr(result, "success", False)),
            message_id=getattr(result, "message_id", None),
            error_code=str(error_code) if error_code is not None else None,
            error_hint=error_hint,
            raw=getattr(result, "raw", None),
        )


def _uuid(prefix: str) -> str:
    return f"ga-{prefix}-{int(time.time() * 1000)}"


class GAFeishuChannelAdapter:
    """GA-oriented façade for FeishuChannel.

    Parameters are passed through to ``FeishuChannel``. Callers may provide an
    already constructed channel for tests, or credentials/config for live use.
    """

    def __init__(self, channel: Optional[FeishuChannel] = None, **channel_kwargs: Any) -> None:
        if channel is not None and channel_kwargs:
            raise ValueError("Provide either channel or channel_kwargs, not both")
        self.channel = channel or FeishuChannel(**channel_kwargs)

    # ---- lifecycle / event handling -------------------------------------
    def start_background(self, *, timeout: Optional[float] = 30.0) -> None:
        self.channel.start_background(timeout=timeout)

    def connect(self) -> None:
        self.channel.connect()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self.channel.stop(join_timeout=join_timeout)

    def on(self, event_name: str, handler: ChannelEventHandler):
        return self.channel.on(event_name, handler)

    def handle_webhook_request(self, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        return self.channel.handle_webhook_request(headers, body)

    # ---- outbound sending ------------------------------------------------
    def send_text(self, to: str, text: str, *, reply_to: Optional[str] = None, uuid: Optional[str] = None) -> GAChannelSendResult:
        opts = self._opts(reply_to=reply_to, uuid=uuid or _uuid("text"))
        return GAChannelSendResult.from_sdk(self.channel.send(to, {"text": text}, opts))

    def send_markdown(self, to: str, markdown: str, *, reply_to: Optional[str] = None, uuid: Optional[str] = None) -> GAChannelSendResult:
        opts = self._opts(reply_to=reply_to, uuid=uuid or _uuid("markdown"))
        return GAChannelSendResult.from_sdk(self.channel.send(to, {"markdown": markdown}, opts))

    def send_card(self, to: str, card: Dict[str, Any], *, reply_to: Optional[str] = None, uuid: Optional[str] = None) -> GAChannelSendResult:
        opts = self._opts(reply_to=reply_to, uuid=uuid or _uuid("card"))
        return GAChannelSendResult.from_sdk(self.channel.send(to, {"card": card}, opts))

    def send_file(self, to: str, source: Any, *, file_name: Optional[str] = None, reply_to: Optional[str] = None) -> GAChannelSendResult:
        message: Dict[str, Any] = {"file": {"source": source}}
        if file_name:
            message["file"]["file_name"] = file_name
        return GAChannelSendResult.from_sdk(self.channel.send(to, message, self._opts(reply_to=reply_to, uuid=_uuid("file"))))

    def send_image(self, to: str, source: Any, *, reply_to: Optional[str] = None) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(
            self.channel.send(to, {"image": {"source": source}}, self._opts(reply_to=reply_to, uuid=_uuid("image")))
        )

    # ---- cards / helpers -------------------------------------------------
    def update_card(self, message_id: str, card: Dict[str, Any]) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.update_card(message_id, card))

    def add_reaction(self, message_id: str, emoji_type: str) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.add_reaction(message_id, emoji_type))

    def remove_reaction(self, message_id: str, reaction_id: str) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.remove_reaction(message_id, reaction_id))

    def edit_message(self, message_id: str, message: Any) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.edit_message(message_id, message))

    def recall_message(self, message_id: str) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.recall_message(message_id))

    def get_chat_info(self, chat_id: str):
        return self.channel.get_chat_info(chat_id)

    # ---- media -----------------------------------------------------------
    def upload_media(self, source: Any, *, kind: str, file_name: Optional[str] = None, file_type: Optional[str] = None) -> str:
        return self.channel.upload_media(source, kind=kind, file_name=file_name, file_type=file_type)

    def download_resource(self, file_key: str, *, resource_type: str = "image", message_id: Optional[str] = None) -> Optional[bytes]:
        return self.channel.download_resource(file_key, resource_type=resource_type, message_id=message_id)

    def download_resource_to_file(
        self,
        file_key: str,
        *,
        resource_type: str = "image",
        message_id: Optional[str] = None,
        dest_dir: Path,
        file_name: Optional[str] = None,
    ) -> Path:
        return self.channel.download_resource_to_file(
            file_key,
            resource_type=resource_type,
            message_id=message_id,
            dest_dir=dest_dir,
            file_name=file_name,
        )

    # ---- streaming -------------------------------------------------------
    def stream(self, to: str, spec: Dict[str, Any], opts: Optional[Dict[str, Any]] = None) -> GAChannelSendResult:
        return GAChannelSendResult.from_sdk(self.channel.stream(to, spec, opts))

    @staticmethod
    def _opts(*, reply_to: Optional[str] = None, uuid: Optional[str] = None, reply_in_thread: Optional[bool] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {}
        if reply_to:
            opts["reply_to"] = reply_to
        if uuid:
            opts["uuid"] = uuid
        if reply_in_thread is not None:
            opts["reply_in_thread"] = reply_in_thread
        return opts

    @staticmethod
    def result_to_dict(result: GAChannelSendResult) -> Dict[str, Any]:
        return asdict(result) if is_dataclass(result) else dict(result)


__all__ = ["GAChannelSendResult", "GAFeishuChannelAdapter"]
