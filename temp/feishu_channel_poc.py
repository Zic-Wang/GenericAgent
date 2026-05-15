#!/usr/bin/env python3
"""FeishuChannel POC for GA channel-layer migration.

Default mode is dry-run: no network, no secrets, no message sending.

Live mode examples:
  python temp/feishu_channel_poc.py --live --chat-id oc_xxx
  python temp/feishu_channel_poc.py --live --chat-id oc_xxx --reply-to om_xxx

Credentials are intentionally read from environment variables only:
  FEISHU_APP_ID / FEISHU_APP_SECRET
This avoids reading project key files in this POC.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lark_oapi.channel import FeishuChannel
from lark_oapi.channel.types import CardActionEvent, InboundMessage, SendResult


def _result_dict(result: SendResult) -> Dict[str, Any]:
    if is_dataclass(result):
        data = asdict(result)
    else:
        data = {
            "success": getattr(result, "success", None),
            "message_id": getattr(result, "message_id", None),
            "error": getattr(result, "error", None),
            "raw": getattr(result, "raw", None),
            "chunk_ids": getattr(result, "chunk_ids", None),
        }
    err = data.get("error")
    if err is not None and is_dataclass(err):
        data["error"] = asdict(err)
    elif err is not None:
        data["error"] = repr(err)
    return data


def build_demo_card(title: str = "GA FeishuChannel POC") -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "**FeishuChannel POC**\n\n- send text/markdown\n- send card\n- update card\n- cardAction handler\n- optional reply_to"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "POC Action"},
                    "type": "primary",
                    "value": {"ga_poc_action": "clicked", "ts": int(time.time())},
                },
            ]
        },
    }


def build_updated_card(action_note: str = "card updated by POC") -> Dict[str, Any]:
    card = build_demo_card("GA FeishuChannel POC ✅")
    card["header"]["template"] = "green"
    card["body"]["elements"].insert(0, {"tag": "markdown", "content": f"**Update:** {action_note}"})
    return card


def describe_channel_api() -> Dict[str, Any]:
    import inspect

    methods = [
        "__init__",
        "start_background",
        "connect_until_ready",
        "stop",
        "on",
        "send",
        "update_card",
        "add_reaction",
        "remove_reaction",
        "stream",
        "handle_webhook_request",
    ]
    return {name: str(inspect.signature(getattr(FeishuChannel, name))) for name in methods if hasattr(FeishuChannel, name)}


def run_dry() -> int:
    print("[DRY] FeishuChannel signatures:")
    print(json.dumps(describe_channel_api(), ensure_ascii=False, indent=2))
    print("[DRY] demo card:")
    print(json.dumps(build_demo_card(), ensure_ascii=False, indent=2))
    print("[DRY] SendResult fields:", getattr(SendResult, "__annotations__", {}))
    print("[DRY] InboundMessage fields:", getattr(InboundMessage, "__annotations__", {}))
    print("[DRY] CardActionEvent fields:", getattr(CardActionEvent, "__annotations__", {}))
    print("[DRY] OK: no network, no credentials read")
    return 0


def run_live(chat_id: str, reply_to: Optional[str] = None, timeout: float = 30.0) -> int:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("[LIVE] missing FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2

    channel = FeishuChannel(app_id=app_id, app_secret=app_secret, transport="ws")

    def on_error(err: Any) -> None:
        print("[event:error]", repr(err), flush=True)

    def on_message(msg: InboundMessage) -> None:
        print("[event:message]", getattr(msg, "message_id", None), getattr(msg, "content_text", ""), flush=True)

    def on_card_action(event: CardActionEvent) -> None:
        print("[event:cardAction]", event.message_id, event.action, flush=True)
        try:
            channel.update_card(event.message_id, build_updated_card("button clicked"))
        except Exception as exc:
            print("[event:cardAction update failed]", repr(exc), flush=True)

    channel.on("error", on_error)
    channel.on("message", on_message)
    channel.on("cardAction", on_card_action)
    channel.start_background(timeout=timeout)

    opts: Dict[str, Any] = {"uuid": f"ga-feishu-channel-poc-{int(time.time())}"}
    if reply_to:
        opts["reply_to"] = reply_to

    results = []
    results.append(("markdown", channel.send(chat_id, {"markdown": "**GA FeishuChannel POC** live markdown send"}, opts)))
    results.append(("card", channel.send(chat_id, {"card": build_demo_card()}, {"uuid": f"ga-feishu-channel-poc-card-{int(time.time())}"})))

    for label, result in results:
        print(f"[LIVE] {label} result:")
        print(json.dumps(_result_dict(result), ensure_ascii=False, indent=2))

    card_result = results[-1][1]
    if card_result.success and card_result.message_id:
        update_result = channel.update_card(card_result.message_id, build_updated_card("initial live update"))
        print("[LIVE] update_card result:")
        print(json.dumps(_result_dict(update_result), ensure_ascii=False, indent=2))

    print("[LIVE] waiting briefly for cardAction events; press Ctrl+C to stop earlier")
    try:
        time.sleep(min(timeout, 30.0))
    finally:
        channel.stop()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FeishuChannel POC for GA")
    parser.add_argument("--live", action="store_true", help="run real WebSocket/send test")
    parser.add_argument("--chat-id", help="target chat_id for live mode")
    parser.add_argument("--reply-to", help="optional message_id to test reply_to")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if not args.live:
        return run_dry()
    if not args.chat_id:
        parser.error("--chat-id is required in --live mode")
    return run_live(args.chat_id, args.reply_to, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
