"""GA-normalized inbound message model.

This module provides a frontend-neutral message shape for future Channel-layer
migration. It can be constructed from ``lark_oapi.channel.types.InboundMessage``
without importing or depending on the legacy ``frontends.fsapp`` parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class GAResource:
    """Normalized inbound attachment/resource descriptor."""

    kind: str
    key: Optional[str] = None
    name: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GAMention:
    """Normalized mention descriptor."""

    id: Optional[str] = None
    name: Optional[str] = None
    key: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GAInboundMessage:
    """Stable GA-facing inbound message model."""

    message_id: str
    chat_id: Optional[str]
    chat_type: Optional[str]
    sender_id: Optional[str]
    sender_name: Optional[str]
    content_text: str = ""
    reply_to_message_id: Optional[str] = None
    mentions: List[GAMention] = field(default_factory=list)
    mentioned_bot: bool = False
    mentioned_all: bool = False
    resources: List[GAResource] = field(default_factory=list)
    raw_content_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_feishu_channel(cls, msg: Any) -> "GAInboundMessage":
        """Build from ``lark_oapi.channel.types.InboundMessage``-like object."""
        conversation = getattr(msg, "conversation", None)
        sender = getattr(msg, "sender", None)
        reply = getattr(msg, "reply", None)
        return cls(
            message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            chat_id=_first_attr(conversation, "id", "chat_id", "open_chat_id"),
            chat_type=_first_attr(conversation, "type", "chat_type"),
            sender_id=_first_attr(sender, "open_id", "user_id", "union_id", "id"),
            sender_name=_first_attr(sender, "name", "display_name", "nickname"),
            content_text=str(getattr(msg, "content_text", "") or ""),
            reply_to_message_id=_first_attr(reply, "message_id", "id"),
            mentions=[_mention_from_obj(m) for m in (getattr(msg, "mentions", None) or [])],
            mentioned_bot=bool(getattr(msg, "mentioned_bot", False)),
            mentioned_all=bool(getattr(msg, "mentioned_all", False)),
            resources=[_resource_from_obj(r) for r in (getattr(msg, "resources", None) or [])],
            raw_content_type=getattr(msg, "raw_content_type", None),
            raw=getattr(msg, "raw", None) or {},
        )

    def to_agent_text(self) -> str:
        """Text passed to the agent, preserving a tiny attachment hint."""
        text = (self.content_text or "").strip()
        if self.resources:
            suffix = "\n".join(f"[resource:{r.kind}] {r.name or r.key or ''}".rstrip() for r in self.resources)
            return (text + "\n" + suffix).strip() if text else suffix
        return text

    def is_direct_chat(self) -> bool:
        return (self.chat_type or "").lower() in {"p2p", "direct", "private"}


def _first_attr(obj: Any, *names: str) -> Optional[str]:
    if obj is None:
        return None
    for name in names:
        val = getattr(obj, name, None)
        if val is not None and val != "":
            return str(val)
    if isinstance(obj, dict):
        for name in names:
            val = obj.get(name)
            if val is not None and val != "":
                return str(val)
    return None


def _raw_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    return {k: v for k, v in getattr(obj, "__dict__", {}).items() if not k.startswith("_")}


def _mention_from_obj(obj: Any) -> GAMention:
    return GAMention(
        id=_first_attr(obj, "id", "open_id", "user_id", "union_id"),
        name=_first_attr(obj, "name", "display_name"),
        key=_first_attr(obj, "key"),
        raw=_raw_dict(obj),
    )


def _resource_from_obj(obj: Any) -> GAResource:
    return GAResource(
        kind=_first_attr(obj, "type", "kind", "resource_type") or "unknown",
        key=_first_attr(obj, "key", "file_key", "image_key", "media_key"),
        name=_first_attr(obj, "name", "file_name"),
        mime_type=_first_attr(obj, "mime_type", "mime"),
        size=_safe_int(getattr(obj, "size", None) if not isinstance(obj, dict) else obj.get("size")),
        raw=_raw_dict(obj),
    )


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


__all__ = ["GAInboundMessage", "GAResource", "GAMention"]
