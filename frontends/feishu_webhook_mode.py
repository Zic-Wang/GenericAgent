"""Webhook-mode helpers for GA Feishu Channel migration.

The official ``FeishuChannel`` supports ``transport="webhook"`` and exposes a
framework-agnostic ``handle_webhook_request(headers, body) -> (status, bytes)``.

This module deliberately does not start an HTTP server and does not read secret
files. Applications can wrap ``GAFeishuWebhookMode.handle_request`` from FastAPI,
Starlette, aiohttp, Flask, or any other HTTP layer.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Tuple

from lark_oapi.channel import FeishuChannel


WebhookResponse = Tuple[int, bytes]
WebhookHandler = Callable[[Any], Any]


@dataclass
class WebhookModeConfig:
    app_id: str
    app_secret: str
    encrypt_key: Optional[str] = None
    verification_token: Optional[str] = None
    domain: Optional[str] = None
    event_handlers: Dict[str, WebhookHandler] = field(default_factory=dict)


class GAFeishuWebhookMode:
    """Small wrapper for FeishuChannel webhook transport."""

    def __init__(self, config: WebhookModeConfig, *, channel: Optional[FeishuChannel] = None) -> None:
        self.config = config
        self.channel = channel or FeishuChannel(
            app_id=config.app_id,
            app_secret=config.app_secret,
            encrypt_key=config.encrypt_key,
            verification_token=config.verification_token,
            domain=config.domain,
            transport="webhook",
        )
        self._ready = False
        for event_name, handler in config.event_handlers.items():
            self.on(event_name, handler)

    def on(self, event_name: str, handler: WebhookHandler):
        """Register an event handler and return SDK unsubscribe callable."""
        return self.channel.on(event_name, handler)

    async def start(self) -> None:
        """Initialize webhook dispatcher.

        In webhook mode this does not bind a socket; it prepares the internal
        dispatcher so HTTP adaptors can call ``handle_request``.
        """
        await self.channel.connect()
        self._ready = True

    async def stop(self) -> None:
        await self.channel.stop()
        self._ready = False

    async def handle_request(self, headers: Mapping[str, str], body: bytes) -> WebhookResponse:
        if not self._ready:
            await self.start()
        return await self.channel.handle_webhook_request(headers, body)

    async def asgi_app(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Dict[str, Any]]], send: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Minimal ASGI-compatible adaptor.

        This is intentionally tiny and framework-neutral. Production services
        should add auth/IP allowlist/rate limiting outside this helper.
        """
        if scope.get("type") != "http":
            raise RuntimeError("GAFeishuWebhookMode.asgi_app only supports HTTP scope")
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
        body = await _read_asgi_body(receive)
        status, payload = await self.handle_request(headers, body)
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


def build_webhook_channel(
    *,
    app_id: str,
    app_secret: str,
    encrypt_key: Optional[str] = None,
    verification_token: Optional[str] = None,
    domain: Optional[str] = None,
    handlers: Optional[Mapping[str, WebhookHandler]] = None,
) -> GAFeishuWebhookMode:
    """Factory used by future frontend wiring/tests."""
    return GAFeishuWebhookMode(
        WebhookModeConfig(
            app_id=app_id,
            app_secret=app_secret,
            encrypt_key=encrypt_key,
            verification_token=verification_token,
            domain=domain,
            event_handlers=dict(handlers or {}),
        )
    )


async def _read_asgi_body(receive: Callable[[], Awaitable[Dict[str, Any]]]) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def run_sync(coro):
    """Run an async helper from synchronous smoke tests/tools."""
    return asyncio.run(coro)


__all__ = ["GAFeishuWebhookMode", "WebhookModeConfig", "WebhookResponse", "build_webhook_channel", "run_sync"]
