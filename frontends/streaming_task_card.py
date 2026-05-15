"""Streaming task card abstraction for GA Feishu channel output.

This module is a side-effect-free skeleton for Phase 7 of the Feishu Channel
migration. It does not start network connections. It wraps a channel-like object
(``GAFeishuChannelAdapter`` or compatible fake) and provides a small stateful
interface for task progress updates.

Design goals:

- Prefer FeishuChannel ``stream(...)`` when available.
- Fallback to card send/update for channels that do not support streaming yet.
- Keep GA-facing code independent from Feishu SDK card details.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from frontends.feishu_channel_adapter import GAChannelSendResult, GAFeishuChannelAdapter


@dataclass
class TaskStep:
    title: str
    detail: str = ""
    status: str = "running"
    ts: float = field(default_factory=time.time)


@dataclass
class StreamingTaskState:
    title: str
    status: str = "running"
    summary: str = ""
    steps: List[TaskStep] = field(default_factory=list)
    final_text: str = ""
    message_id: Optional[str] = None
    stream_started: bool = False


class StreamingTaskCard:
    """Small task-progress façade over GAFeishuChannelAdapter."""

    def __init__(
        self,
        channel: GAFeishuChannelAdapter,
        to: str,
        *,
        title: str = "GA Task",
        reply_to: Optional[str] = None,
        prefer_stream: bool = True,
    ) -> None:
        self.channel = channel
        self.to = to
        self.reply_to = reply_to
        self.prefer_stream = prefer_stream
        self.state = StreamingTaskState(title=title)

    def start(self, summary: str = "") -> GAChannelSendResult:
        self.state.summary = summary
        if self.prefer_stream:
            result = self.channel.stream(
                self.to,
                {"markdown": self._markdown()},
                {"reply_to": self.reply_to, "uuid": self._uuid("stream-start")},
            )
            self.state.stream_started = result.success
            self.state.message_id = result.message_id
            if result.success:
                return result
        result = self.channel.send_card(self.to, self._card(), reply_to=self.reply_to, uuid=self._uuid("card-start"))
        self.state.message_id = result.message_id
        return result

    def add_step(self, title: str, detail: str = "", *, status: str = "running") -> GAChannelSendResult:
        self.state.steps.append(TaskStep(title=title, detail=detail, status=status))
        return self.flush()

    def update_last_step(self, *, detail: Optional[str] = None, status: Optional[str] = None) -> GAChannelSendResult:
        if not self.state.steps:
            self.state.steps.append(TaskStep(title="Step"))
        last = self.state.steps[-1]
        if detail is not None:
            last.detail = detail
        if status is not None:
            last.status = status
        return self.flush()

    def finish(self, final_text: str = "") -> GAChannelSendResult:
        self.state.status = "done"
        self.state.final_text = final_text
        return self.flush()

    def fail(self, error_text: str) -> GAChannelSendResult:
        self.state.status = "failed"
        self.state.final_text = error_text
        return self.flush()

    def flush(self) -> GAChannelSendResult:
        if self.state.stream_started:
            return self.channel.stream(
                self.to,
                {"markdown": self._markdown()},
                {"reply_to": self.reply_to, "uuid": self._uuid("stream-update")},
            )
        if self.state.message_id:
            return self.channel.update_card(self.state.message_id, self._card())
        return self.start(self.state.summary)

    def _markdown(self) -> str:
        lines = [f"**{self.state.title}**", "", f"Status: `{self.state.status}`"]
        if self.state.summary:
            lines += ["", self.state.summary]
        if self.state.steps:
            lines += ["", "**Steps**"]
            for idx, step in enumerate(self.state.steps, 1):
                detail = f" — {step.detail}" if step.detail else ""
                lines.append(f"{idx}. `{step.status}` {step.title}{detail}")
        if self.state.final_text:
            lines += ["", "**Result**", self.state.final_text]
        return "\n".join(lines)

    def _card(self) -> Dict[str, Any]:
        template = "green" if self.state.status == "done" else "red" if self.state.status == "failed" else "blue"
        elements: List[Dict[str, Any]] = [
            {"tag": "markdown", "content": self.state.summary or f"Status: `{self.state.status}`"}
        ]
        if self.state.steps:
            elements.append({"tag": "markdown", "content": self._steps_markdown()})
        if self.state.final_text:
            elements.append({"tag": "markdown", "content": f"**Result**\n{self.state.final_text}"})
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": self.state.title}, "template": template},
            "body": {"elements": elements},
        }

    def _steps_markdown(self) -> str:
        lines = ["**Steps**"]
        for idx, step in enumerate(self.state.steps, 1):
            detail = f" — {step.detail}" if step.detail else ""
            lines.append(f"{idx}. `{step.status}` {step.title}{detail}")
        return "\n".join(lines)

    @staticmethod
    def _uuid(prefix: str) -> str:
        return f"ga-task-{prefix}-{int(time.time() * 1000)}"


__all__ = ["StreamingTaskCard", "StreamingTaskState", "TaskStep"]
