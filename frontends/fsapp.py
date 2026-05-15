import glob, json, os, queue as Q, re, sys, threading, time, uuid
from collections import OrderedDict
import atexit, hashlib
try:
    import msvcrt
except Exception:
    msvcrt = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
from agentmain import GeneraticAgent
from frontends.approval_store import ApprovalStore, RESOLVED
from frontends.ga_inbound_message import GAInboundMessage
from frontends.chatapp_common import format_restore
from frontends.continue_cmd import handle_frontend_command as handle_continue_frontend, reset_conversation
from llmcore import mykeys

import traceback
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

_TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_AUDIO_EXTS = {".opus", ".mp3", ".wav", ".m4a", ".aac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
_MSG_TYPE_MAP = {"image": "[image]", "audio": "[audio]", "file": "[file]", "media": "[media]", "sticker": "[sticker]"}

TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
MEDIA_DIR = os.path.join(TEMP_DIR, "feishu_media")
os.makedirs(MEDIA_DIR, exist_ok=True)


def _acquire_fsapp_singleton():
    """Per-agent process lock; prevents duplicate Feishu long-connection clients."""
    if msvcrt is None:
        return None
    path = os.path.join(TEMP_DIR, "fsapp_singleton.lock")
    f = open(path, "a+b")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(f"[INFO] another fsapp is already running for {PROJECT_ROOT}; exiting duplicate pid={os.getpid()}", flush=True)
        f.close()
        sys.exit(0)
    atexit.register(lambda: (msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1), f.close()))
    return f


_FSAPP_LOCK = _acquire_fsapp_singleton()
_TRUNC_TAIL = 300  # 截断兜底时保留原文尾部字符数
_DEDUP_FILE = os.path.join(TEMP_DIR, "fsapp_seen_message_ids.txt")
_DEDUP_TTL_SEC = 6 * 3600
_DEDUP_MAX = 500


def _message_claim_once(message_id):
    """Return True only for the first process that claims this Feishu message_id."""
    if not message_id:
        return True
    now = time.time()
    lock_path = _DEDUP_FILE + ".lock"
    lf = None
    try:
        if msvcrt is not None:
            lf = open(lock_path, "a+b")
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        rows = []
        if os.path.exists(_DEDUP_FILE):
            with open(_DEDUP_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split(" ", 1)
                    if len(parts) != 2:
                        continue
                    try:
                        ts = float(parts[0])
                    except Exception:
                        continue
                    mid = parts[1]
                    if now - ts <= _DEDUP_TTL_SEC:
                        rows.append((ts, mid))
        if any(mid == message_id for _, mid in rows):
            return False
        rows.append((now, message_id))
        rows = rows[-_DEDUP_MAX:]
        tmp = _DEDUP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for ts, mid in rows:
                f.write(f"{ts:.3f} {mid}\n")
        os.replace(tmp, _DEDUP_FILE)
        return True
    except Exception as e:
        print(f"[WARN] message dedup failed: {e}")
        return True
    finally:
        if lf is not None:
            try:
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                lf.close()
            except Exception:
                pass


def _clean(text):
    for pat in _TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_files(text):
    return re.findall(r"\[FILE:([^\]]+)\]", text or "")


def _strip_files(text):
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def _display_text(text):
    cleaned = _strip_files(_clean(text))
    if cleaned:
        return cleaned
    tail = (text or "").strip()[-_TRUNC_TAIL:]
    return "（无文本输出）" + (f"\n…{tail}" if tail else "")


def _to_allowed_set(value):
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _extract_share_card_content(content_json, msg_type):
    parts = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join([p for p in parts if p]).strip() or f"[{msg_type}]"


def _extract_interactive_content(content):
    parts = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts
    title = content.get("title")
    if isinstance(title, dict):
        title_text = title.get("content", "") or title.get("text", "")
        if title_text:
            parts.append(f"title: {title_text}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")
    elements = content.get("elements", [])
    if isinstance(elements, list):
        for row in elements:
            if isinstance(row, dict):
                parts.extend(_extract_element_content(row))
            elif isinstance(row, list):
                for el in row:
                    parts.extend(_extract_element_content(el))
    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))
    header = content.get("header", {})
    if isinstance(header, dict):
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    return [p for p in parts if p]


def _extract_element_content(element):
    parts = []
    if not isinstance(element, dict):
        return parts
    tag = element.get("tag", "")
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str) and text:
            parts.append(text)
        for field in element.get("fields", []) or []:
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    content = field_text.get("content", "") or field_text.get("text", "")
                    if content:
                        parts.append(content)
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            content = text.get("content", "") or text.get("text", "")
            if content:
                parts.append(content)
        url = element.get("url", "") or (element.get("multi_url", {}) or {}).get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = element.get("alt", {})
        if isinstance(alt, dict):
            parts.append(alt.get("content", "[image]") or "[image]")
        else:
            parts.append("[image]")
    for child in element.get("elements", []) or []:
        parts.extend(_extract_element_content(child))
    for col in element.get("columns", []) or []:
        for child in (col.get("elements", []) if isinstance(col, dict) else []):
            parts.extend(_extract_element_content(child))
    return parts


def _extract_post_content(content_json):
    def _parse_block(block):
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if block.get("title"):
            texts.append(block.get("title"))
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and el.get("image_key"):
                    images.append(el["image_key"])
        text = " ".join([t for t in texts if t]).strip()
        return text or None, images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs
    return "", []


APP_ID = str(mykeys.get("fs_app_id", "") or "").strip()
APP_SECRET = str(mykeys.get("fs_app_secret", "") or "").strip()
ALLOWED_USERS = _to_allowed_set(mykeys.get("fs_allowed_users", []))
PUBLIC_ACCESS = not ALLOWED_USERS or "*" in ALLOWED_USERS
APPROVAL_ADMINS = _to_allowed_set(mykeys.get("fs_approval_admins", []))
AGENT_TIMEOUT_SEC = 900
# Feishu emoji_type for the transient "bot is typing" reaction.
TYPING_REACTION_EMOJI = "Typing"


agent = GeneraticAgent()
_agent_thread = threading.Thread(target=agent.run, daemon=True, name="GA-core")
_agent_thread.start()
try:
    print(f"[INFO] GA core started pid={os.getpid()} thread_alive={_agent_thread.is_alive()} llm={agent.get_llm_name()}")
except Exception as e:
    print(f"[WARN] GA core started but status unavailable: {e}")
client, user_tasks = None, {}
SENT_MESSAGES = OrderedDict()
_MAX_SENT_MESSAGES = 200
_APPROVAL_STATE = {}
_APPROVAL_DONE = {}
_APPROVAL_DONE_TTL_SEC = 600
_APPROVAL_STORE = ApprovalStore(os.path.join(TEMP_DIR, "approval_state.sqlite3"), done_ttl_sec=_APPROVAL_DONE_TTL_SEC)
_APPROVAL_LOCK = threading.RLock()
_APPROVAL_CHOICE_MAP = {
    "approve_once": "once",
    "approve_session": "session",
    "approve_always": "always",
    "deny": "deny",
}


def _record_sent_message(message_id, receive_id=None, receive_id_type=None, msg_type=None):
    if not message_id:
        return
    SENT_MESSAGES[message_id] = {
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "msg_type": msg_type,
        "ts": time.time(),
    }
    SENT_MESSAGES.move_to_end(message_id)
    while len(SENT_MESSAGES) > _MAX_SENT_MESSAGES:
        SENT_MESSAGES.popitem(last=False)


def _last_sent_message_id(receive_id=None):
    for message_id, meta in reversed(SENT_MESSAGES.items()):
        if receive_id is None or meta.get("receive_id") == receive_id:
            return message_id
    return next(reversed(SENT_MESSAGES), None) if SENT_MESSAGES else None


def _is_own_sent_message(message_id):
    return bool(message_id and message_id in SENT_MESSAGES)


def create_client():
    return lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).log_level(lark.LogLevel.INFO).build()


def _card_raw(elements):
    return json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }, ensure_ascii=False)


def _card(text):
    return _card_raw([{"tag": "markdown", "content": text}])


def _send_raw(receive_id, payload, msg_type, rtype):
    try:
        body = CreateMessageRequest.builder().receive_id_type(rtype).request_body(
            CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type).content(payload).build()
        ).build()
        r = client.im.v1.message.create(body)
        if r.success():
            message_id = r.data.message_id if r.data else None
            _record_sent_message(message_id, receive_id, rtype, msg_type)
            return message_id
        print(f"发送失败: {r.code}, {r.msg}")
    except Exception as e:
        print(f"[ERROR] _send_raw 网络异常: {e}")
    return None


def _patch_card(message_id, card_json):
    return _patch_card_result(message_id, card_json)[0]


def _patch_card_result(message_id, card_json):
    try:
        body = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder().content(card_json).build()
        ).build()
        r = client.im.v1.message.patch(body)
        if not r.success():
            print(f"[ERROR] patch_card 失败: {r.code}, {r.msg}")
        msg = f"{getattr(r, 'code', '')} {getattr(r, 'msg', '')}".lower()
        return r.success(), ("230099" in msg or "11310" in msg or "element exceeds the limit" in msg)
    except Exception as e:
        print(f"[ERROR] _patch_card 网络异常: {e}")
        return False, False


def _approval_button(label, action_name, approval_id, btn_type="default"):
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "value": {"ga_approval_action": action_name, "approval_id": approval_id},
    }


def build_exec_approval_card(approval_id, cmd, reason, requester=None):
    content = (
        "**Command Approval Required**\n\n"
        f"**Approval ID:** `{approval_id}`\n\n"
        "**Command:**\n"
        f"```\n{cmd}\n```\n\n"
        f"**Reason:** {reason}"
    )
    if requester:
        content += f"\n\n**Requester:** {requester}"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Command Approval Required"},
        },
        "elements": [
            {"tag": "markdown", "content": content},
            {"tag": "action", "actions": [
                _approval_button("Allow Once", "approve_once", approval_id, "primary"),
                _approval_button("Session", "approve_session", approval_id),
                _approval_button("Always", "approve_always", approval_id),
                _approval_button("Deny", "deny", approval_id, "danger"),
            ]},
        ],
    }


def build_approval_result_card(approval_id, cmd, reason, choice, operator_name=None):
    ok = choice in ("once", "session", "always")
    title = "Command Approved" if ok else "Command Denied"
    template = "green" if ok else "red"
    if choice == "timeout":
        title, template = "Approval Timeout", "grey"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "markdown", "content": (
            f"**Approval ID:** `{approval_id}`\n\n"
            f"**Result:** `{choice}`\n\n"
            f"**Operator:** {operator_name or '-'}\n\n"
            "**Command:**\n"
            f"```\n{cmd}\n```\n\n"
            f"**Reason:** {reason}"
        )}],
    }


def _approval_response(card=None, toast=None, toast_type="info"):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    body = {}
    if card is not None:
        body["card"] = {"type": "raw", "data": card}
    if toast:
        body["toast"] = {"type": toast_type, "content": toast}
    return P2CardActionTriggerResponse(body)


def _approval_toast(content, toast_type="info"):
    return _approval_response(toast=content, toast_type=toast_type)


def _operator_name(operator):
    if not operator:
        return "unknown"
    return getattr(operator, "open_id", None) or getattr(operator, "user_id", None) or "unknown"


def _is_approval_operator_authorized(open_id):
    if not open_id:
        return False
    if APPROVAL_ADMINS:
        return open_id in APPROVAL_ADMINS
    if ALLOWED_USERS and "*" not in ALLOWED_USERS:
        return open_id in ALLOWED_USERS
    return False


def update_approval_card(message_id, approval_id, cmd, reason, choice, operator_name=None):
    if not message_id:
        return False
    card = build_approval_result_card(approval_id, cmd, reason, choice, operator_name)
    return _patch_card_result(message_id, json.dumps(card, ensure_ascii=False))[0]


def _approval_state_from_record(record, local_state=None):
    if not record:
        return local_state or {}
    state = dict(local_state or {})
    state.update({
        "approval_id": record.approval_id,
        "session_key": record.session_key,
        "cmd": record.cmd,
        "reason": record.reason,
        "receive_id": record.receive_id,
        "receive_id_type": record.receive_id_type,
        "message_id": record.message_id,
        "result": record.result,
        "requester_open_id": record.requester_open_id,
        "operator_open_id": record.operator_open_id,
        "operator_name": record.operator_name,
        "created_at": record.created_at,
        "resolved_at": record.resolved_at,
        "expires_at": record.expires_at,
    })
    return state


def send_exec_approval(receive_id, session_key, cmd, reason, requester_open_id=None, timeout_sec=300, receive_id_type="chat_id"):
    """Send a Feishu interactive approval card and block until resolved.

    The SQLite ApprovalStore is the source of truth for pending/resolved state.
    In-memory state is retained only to wake the local waiting thread quickly.
    The wait loop also polls the store so a callback handled by another process
    can still release this caller.
    """
    approval_id = uuid.uuid4().hex
    event = threading.Event()
    _APPROVAL_STORE.cleanup_done()
    _APPROVAL_STORE.expire_pending()
    _APPROVAL_STORE.create_pending(
        approval_id=approval_id,
        session_key=session_key,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        cmd=cmd,
        reason=reason,
        requester_open_id=requester_open_id,
        timeout_sec=timeout_sec,
    )
    state = {
        "approval_id": approval_id,
        "session_key": session_key,
        "cmd": cmd,
        "reason": reason,
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "message_id": None,
        "created_at": time.time(),
        "event": event,
        "result": None,
        "requester_open_id": requester_open_id,
    }
    # Register before sending the card. Feishu users can click very quickly; if the
    # callback arrives before local memory is populated, it looks "expired".
    with _APPROVAL_LOCK:
        _APPROVAL_STATE[approval_id] = state
    card = build_exec_approval_card(approval_id, cmd, reason, requester_open_id)
    message_id = _send_raw(receive_id, json.dumps(card, ensure_ascii=False), "interactive", receive_id_type)
    if not message_id:
        with _APPROVAL_LOCK:
            _APPROVAL_STATE.pop(approval_id, None)
        _APPROVAL_STORE.resolve(approval_id, "send_failed")
        return "send_failed"
    state["message_id"] = message_id
    _APPROVAL_STORE.set_message_id(approval_id, message_id)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        remaining = max(0.0, min(1.0, deadline - time.time()))
        event.wait(remaining)
        rec = _APPROVAL_STORE.get(approval_id)
        if rec and rec.status == RESOLVED:
            with _APPROVAL_LOCK:
                _APPROVAL_STATE.pop(approval_id, None)
            return rec.result or "deny"
        if event.is_set():
            break
    rec = _APPROVAL_STORE.get(approval_id)
    if rec and rec.status == RESOLVED:
        with _APPROVAL_LOCK:
            _APPROVAL_STATE.pop(approval_id, None)
        return rec.result or "deny"
    with _APPROVAL_LOCK:
        _APPROVAL_STATE.pop(approval_id, None)
    result = _APPROVAL_STORE.resolve(approval_id, "timeout")
    timeout_state = _approval_state_from_record(result.record, state)
    update_approval_card(message_id, approval_id, cmd, reason, "timeout", None)
    return timeout_state.get("result") or "timeout"


def resolve_approval(approval_id, choice, operator_open_id=None, operator_name=None, patch_card=True):
    result = _APPROVAL_STORE.resolve(
        approval_id,
        choice,
        operator_open_id=operator_open_id,
        operator_name=operator_name,
    )
    with _APPROVAL_LOCK:
        local_state = _APPROVAL_STATE.pop(approval_id, None)
    if not result.ok:
        # Backward-compatible fallback for very old in-memory approvals created
        # before the store was wired in this process.
        if local_state is None:
            return False, result.reason
        local_state["result"] = choice
        local_state["operator_open_id"] = operator_open_id
        local_state["operator_name"] = operator_name
        local_state["resolved_at"] = time.time()
        with _APPROVAL_LOCK:
            _APPROVAL_DONE[approval_id] = local_state
        local_state.get("event").set()
        return True, local_state
    state = _approval_state_from_record(result.record, local_state)
    state["result"] = result.record.result if result.record else choice
    if local_state and local_state.get("event"):
        local_state.get("event").set()
    with _APPROVAL_LOCK:
        _APPROVAL_DONE[approval_id] = state
    if patch_card:
        update_approval_card(
            state.get("message_id"),
            state.get("approval_id", approval_id),
            state.get("cmd", ""),
            state.get("reason", ""),
            state.get("result") or choice,
            operator_name,
        )
    return True, state

def handle_card_action(data):
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    action_name = value.get("ga_approval_action")
    approval_id = value.get("approval_id")
    if action_name not in _APPROVAL_CHOICE_MAP or not approval_id:
        return _approval_toast("无效审批动作", "warning")
    operator = getattr(event, "operator", None)
    open_id = getattr(operator, "open_id", None)
    operator_name = _operator_name(operator)
    if not _is_approval_operator_authorized(open_id):
        print(f"未授权审批用户: {open_id}")
        return _approval_toast("你没有审批权限", "warning")
    choice = _APPROVAL_CHOICE_MAP[action_name]
    ok, state = resolve_approval(approval_id, choice, open_id, operator_name, patch_card=False)
    if not ok:
        print(f"[WARN] approval action missing or expired: approval_id={approval_id}, operator={open_id}")
        return _approval_toast("审批已处理或已过期", "warning")
    result_card = build_approval_result_card(
        state.get("approval_id", approval_id),
        state.get("cmd", ""),
        state.get("reason", ""),
        choice,
        operator_name,
    )
    return _approval_response(card=result_card)


def send_message(receive_id, content, msg_type="text", use_card=False, receive_id_type="open_id"):
    if use_card:
        return _send_raw(receive_id, _card(content), "interactive", receive_id_type)
    if msg_type == "text":
        return _send_raw(receive_id, json.dumps({"text": content}, ensure_ascii=False), "text", receive_id_type)
    return _send_raw(receive_id, content, msg_type, receive_id_type)


def update_message(message_id, content):
    return _patch_card(message_id, _card(content))


def edit_message_raw(message_id, content):
    """Edit a Feishu message with raw content JSON string."""
    try:
        body = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder().content(content).build()
        ).build()
        r = client.im.v1.message.patch(body)
        if not r.success():
            print(f"[ERROR] edit_message 失败: {r.code}, {r.msg}")
        return r.success(), r
    except Exception as e:
        print(f"[ERROR] edit_message 网络异常: {e}")
        return False, None


def edit_text_message(message_id, text):
    return edit_message_raw(message_id, json.dumps({"text": text}, ensure_ascii=False))


def edit_card_message(message_id, content):
    return edit_message_raw(message_id, _card(content))


def recall_message(message_id):
    """Recall/delete a message. Callers should restrict this to bot-sent message ids."""
    try:
        body = DeleteMessageRequest.builder().message_id(message_id).build()
        r = client.im.v1.message.delete(body)
        if not r.success():
            print(f"[ERROR] recall_message 失败: {r.code}, {r.msg}")
            return False, r
        SENT_MESSAGES.pop(message_id, None)
        return True, r
    except Exception as e:
        print(f"[ERROR] recall_message 网络异常: {e}")
        return False, None


def add_message_reaction(message_id, emoji_type):
    try:
        body = CreateMessageReactionRequest.builder().message_id(message_id).request_body(
            CreateMessageReactionRequestBody.builder().reaction_type(
                Emoji.builder().emoji_type(emoji_type).build()
            ).build()
        ).build()
        r = client.im.v1.message_reaction.create(body)
        if not r.success():
            print(f"[ERROR] add_reaction 失败: {r.code}, {r.msg}")
        return r.success(), r
    except Exception as e:
        print(f"[ERROR] add_reaction 网络异常: {e}")
        return False, None


def list_message_reactions(message_id, reaction_type=None, page_size=20):
    try:
        builder = ListMessageReactionRequest.builder().message_id(message_id).page_size(page_size)
        if reaction_type:
            builder = builder.reaction_type(reaction_type)
        r = client.im.v1.message_reaction.list(builder.build())
        if not r.success():
            print(f"[ERROR] list_reactions 失败: {r.code}, {r.msg}")
            return None, r
        return r.data, r
    except Exception as e:
        print(f"[ERROR] list_reactions 网络异常: {e}")
        return None, None


def delete_message_reaction(message_id, reaction_id):
    try:
        body = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        r = client.im.v1.message_reaction.delete(body)
        if not r.success():
            print(f"[ERROR] delete_reaction 失败: {r.code}, {r.msg}")
        return r.success(), r
    except Exception as e:
        print(f"[ERROR] delete_reaction 网络异常: {e}")
        return False, None


def add_message_reaction_get_id(message_id, emoji_type):
    ok, r = add_message_reaction(message_id, emoji_type)
    if not ok or not r or not getattr(r, "data", None):
        return None
    return getattr(r.data, "reaction_id", None)


def delete_own_reaction_by_type(message_id, emoji_type, reaction_id=None):
    if reaction_id:
        ok, _ = delete_message_reaction(message_id, reaction_id)
        if ok:
            return True
    data, _ = list_message_reactions(message_id, emoji_type)
    if not data:
        return False
    for item in getattr(data, "items", None) or []:
        rid = getattr(item, "reaction_id", None)
        if rid:
            ok, _ = delete_message_reaction(message_id, rid)
            if ok:
                return True
    return False


def _resolve_sent_target(target, receive_id=None):
    if not target or target == "last":
        return _last_sent_message_id(receive_id)
    return target


def _upload_image_sync(file_path):
    try:
        with open(file_path, "rb") as f:
            request = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()
            ).build()
            response = client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            print(f"[ERROR] upload image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload image failed {file_path}: {e}")
    return None


def _upload_file_sync(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    file_type = _FILE_TYPE_MAP.get(ext, "stream")
    file_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            request = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(f).build()
            ).build()
            response = client.im.v1.file.create(request)
            if response.success():
                return response.data.file_key
            print(f"[ERROR] upload file failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] upload file failed {file_path}: {e}")
    return None


def _download_image_sync(message_id, image_key):
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type("image").build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download image failed {image_key}: {e}")
    return None, None


def _download_file_sync(message_id, file_key, resource_type="file"):
    if resource_type == "audio":
        resource_type = "file"
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download {resource_type} failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download {resource_type} failed {file_key}: {e}")
    return None, None


def _download_and_save_media(msg_type, content_json, message_id):
    data, filename = None, None
    if msg_type == "image":
        image_key = content_json.get("image_key")
        if image_key and message_id:
            data, filename = _download_image_sync(message_id, image_key)
            if not filename:
                filename = f"{image_key[:16]}.jpg"
    elif msg_type in ("audio", "file", "media"):
        file_key = content_json.get("file_key")
        if file_key and message_id:
            data, filename = _download_file_sync(message_id, file_key, msg_type)
            if not filename:
                filename = file_key[:16]
            if msg_type == "audio" and filename and not filename.endswith(".opus"):
                filename = f"{filename}.opus"
    if data and filename:
        file_path = os.path.join(MEDIA_DIR, os.path.basename(filename))
        with open(file_path, "wb") as f:
            f.write(data)
        return file_path, filename
    return None, None


def _describe_media(msg_type, file_path, filename):
    if msg_type == "image":
        return f"[image: {filename}]\n[Image: source: {file_path}]"
    if msg_type == "audio":
        return f"[audio: {filename}]\n[File: source: {file_path}]"
    if msg_type in ("file", "media"):
        return f"[{msg_type}: {filename}]\n[File: source: {file_path}]"
    return f"[{msg_type}]\n[File: source: {file_path}]"


def _send_local_file(receive_id, file_path, receive_id_type="open_id"):
    if not os.path.isfile(file_path):
        send_message(receive_id, f"⚠️ 文件不存在: {file_path}", receive_id_type=receive_id_type)
        return False
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTS:
        image_key = _upload_image_sync(file_path)
        if image_key:
            send_message(receive_id, json.dumps({"image_key": image_key}, ensure_ascii=False), msg_type="image", receive_id_type=receive_id_type)
            return True
    else:
        file_key = _upload_file_sync(file_path)
        if file_key:
            msg_type = "media" if ext in _AUDIO_EXTS or ext in _VIDEO_EXTS else "file"
            send_message(receive_id, json.dumps({"file_key": file_key}, ensure_ascii=False), msg_type=msg_type, receive_id_type=receive_id_type)
            return True
    send_message(receive_id, f"⚠️ 文件发送失败: {os.path.basename(file_path)}", receive_id_type=receive_id_type)
    return False


def _send_generated_files(receive_id, raw_text, receive_id_type="open_id"):
    for file_path in _extract_files(raw_text):
        _send_local_file(receive_id, file_path, receive_id_type)


def _build_user_message(message):
    msg_type = message.message_type
    message_id = message.message_id
    content_json = _parse_json(message.content)
    parts, image_paths = [], []
    if msg_type == "text":
        text = str(content_json.get("text", "") or "").strip()
        if text:
            parts.append(text)
    elif msg_type == "post":
        text, image_keys = _extract_post_content(content_json)
        if text:
            parts.append(text)
        for image_key in image_keys:
            file_path, filename = _download_and_save_media("image", {"image_key": image_key}, message_id)
            if file_path and filename:
                parts.append(_describe_media("image", file_path, filename))
                image_paths.append(file_path)
            else:
                parts.append("[image: download failed]")
    elif msg_type in ("image", "audio", "file", "media"):
        file_path, filename = _download_and_save_media(msg_type, content_json, message_id)
        if file_path and filename:
            parts.append(_describe_media(msg_type, file_path, filename))
            if msg_type == "image":
                image_paths.append(file_path)
        else:
            parts.append(f"[{msg_type}: download failed]")
    elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
        parts.append(_extract_share_card_content(content_json, msg_type))
    else:
        parts.append(_MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))
    return "\n".join([p for p in parts if p]).strip(), image_paths


def _fmt_tool_call(tc):
    name = tc.get('tool_name', '?')
    args = {k: v for k, v in (tc.get('args') or {}).items() if not k.startswith('_')}
    return f"- `{name}`({json.dumps(args, ensure_ascii=False)[:200]})"


def _build_step_detail(resp, tool_calls):
    """从 LLM response + tool_calls 组装单步展开详情（纯函数）。"""
    parts = []
    thinking = (getattr(resp, 'thinking', '') or '').strip() if resp else ''
    if thinking:
        parts.append(f"### 💭 Thinking\n{thinking}")
    if tool_calls:
        parts.append("### 🛠 Tool Calls\n" + "\n".join(_fmt_tool_call(tc) for tc in tool_calls))
    content = _display_text((getattr(resp, 'content', '') or '')).strip() if resp else ''
    if content and content != '...':
        parts.append(f"### 📝 Output\n{content}")
    return "\n\n".join(parts)


class _TaskCard:
    """飞书任务卡片：单卡片持续 patch；每步一个独立折叠面板（header 显示 summary，展开看详情）。"""
    _DETAIL_LIMIT = 8000
    _FINAL_LIMIT = 6000

    def __init__(self, receive_id, rid_type):
        self.rid, self.rtype = receive_id, rid_type
        self.steps = []          # [(summary, detail), ...]
        self.status = "🤔 思考中..."
        self.final = None
        self.msg_id = None
        self.page_no = 1
        self.turn_no = 0
        self.turn_base = 1
        self.note = None

    def _step_panel(self, idx, summary, detail):
        detail = detail or "_(无输出)_"
        if len(detail) > self._DETAIL_LIMIT:
            detail = detail[:self._DETAIL_LIMIT] + f"\n\n…(已截断,共 {len(detail)} 字符)"
        return {
            "tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": f"Turn {idx} · {summary}"}},
            "elements": [{"tag": "markdown", "content": detail}],
        }

    def _build(self):
        header = f"**{self.status}**"
        if self.page_no > 1:
            header += f"\n\n📄 工作卡片 {self.page_no}"
        els = [{"tag": "markdown", "content": header}]
        if self.note:
            els.append({"tag": "markdown", "content": self.note})
        for i, (s, d) in enumerate(self.steps, self.turn_base):
            els.append(self._step_panel(i, s, d))
        if self.final:
            els += [{"tag": "hr"}, {"tag": "markdown", "content": self.final}]
        return _card_raw(els)

    def _push(self):
        card = self._build()
        if self.msg_id:
            return _patch_card_result(self.msg_id, card)
        else:
            self.msg_id = _send_raw(self.rid, card, "interactive", self.rtype)
            return bool(self.msg_id), False

    def _rollover(self):
        self.page_no += 1
        self.msg_id = None
        self.final = None
        self.note = "⚠️ 上一张工作卡片达到飞书限制，本页继续展示后续进展。"

    # ── 公开接口 ──

    def start(self):
        self._push()

    def step(self, summary, detail=""):
        self.turn_no += 1
        step = (summary, detail)
        self.steps.append(step)
        self.status = f"⏳ 工作中 · Turn {self.turn_no}"
        ok, limit = self._push()
        if limit:
            self.steps.pop()
            self._rollover()
            self.turn_base = self.turn_no
            self.steps = [step]
            self._push()

    def done(self, text):
        self.status = "✅ 已完成"
        self.final = (text or "_(无文本输出)_")[:self._FINAL_LIMIT]
        ok, limit = self._push()
        if limit:
            self._rollover()
            self.steps = []
            self.turn_base = self.turn_no + 1
            self.final = (text or "_(无文本输出)_")[:self._FINAL_LIMIT]
            ok, _ = self._push()
        # Last-resort delivery: if card creation/patch fails (common after cold boot
        # or message-card expiry), still send a plain text reply so the user is not
        # left with no response.
        if not ok:
            try:
                send_message(self.rid, _display_text(text), receive_id_type=self.rtype)
            except Exception as e:
                print(f"[ERROR] done fallback send failed: {e}")

    def fail(self, msg):
        self.status = f"❌ {msg}"
        self._push()


def _extract_ask_user_event(ctx):
    exit_reason = (ctx or {}).get("exit_reason") or {}
    if exit_reason.get("result") != "EXITED":
        return None
    payload = exit_reason.get("data")
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "INTERRUPT" or payload.get("intent") != "HUMAN_INTERVENTION":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    question = str(data.get("question") or "请审批下一步操作：").strip() or "请审批下一步操作："
    raw_candidates = data.get("candidates") or []
    if not isinstance(raw_candidates, (list, tuple)):
        raw_candidates = []
    candidates = [str(c).strip() for c in raw_candidates if str(c).strip()]
    return {"question": question, "candidates": candidates}


def _candidate_score(candidate, keywords):
    text = str(candidate or "").lower()
    return sum(1 for kw in keywords if kw in text)


def _select_ask_user_candidate(event, approval_choice):
    candidates = list((event or {}).get("candidates") or [])
    if not candidates:
        return {
            "approve_once": "Allow Once",
            "approve_session": "Allow Session",
            "approve_always": "Always Allow",
            "deny": "Deny",
            "timeout": "Deny",
        }.get(approval_choice, str(approval_choice or "Deny"))
    keyword_map = {
        "approve_once": ["allow once", "once", "本次", "一次", "允许"],
        "approve_session": ["session", "会话", "本会话"],
        "approve_always": ["always", "永久", "始终", "以后"],
        "deny": ["deny", "reject", "拒绝", "不允许", "取消", "no"],
        "timeout": ["deny", "reject", "拒绝", "不允许", "取消", "no"],
    }
    keywords = keyword_map.get(approval_choice) or []
    ranked = sorted((( _candidate_score(c, keywords), idx, c) for idx, c in enumerate(candidates)), reverse=True)
    if ranked and ranked[0][0] > 0:
        return ranked[0][2]
    if approval_choice in ("deny", "timeout"):
        return candidates[-1]
    return candidates[0]


def _format_ask_user_cmd(event):
    question = str((event or {}).get("question") or "请审批下一步操作：")
    candidates = list((event or {}).get("candidates") or [])
    if not candidates:
        return question
    lines = [question, "", "候选项："]
    lines.extend(f"{idx}. {candidate}" for idx, candidate in enumerate(candidates, start=1))
    return "\n".join(lines)


def _make_task_hook(card, done_event, on_final, on_ask_user=None):
    """飞书任务 hook：每轮 patch 卡片状态；结束触发 on_final(raw) 处理附件。"""
    def hook(ctx):
        try:
            if ctx.get('exit_reason'):
                ask_event = _extract_ask_user_event(ctx)
                if ask_event and on_ask_user:
                    on_ask_user(ask_event)
                    return
                resp = ctx.get('response')
                raw = resp.content if hasattr(resp, 'content') else str(resp)
                display = _display_text(raw)
                if display.startswith("（无文本输出）"):
                    # fallback: show thinking + tool_calls when content is empty after cleaning
                    parts = []
                    thinking = getattr(resp, 'thinking', '') or ''
                    if thinking:
                        parts.append(f"**[Thinking]**\n{thinking.strip()}")
                    tool_calls = ctx.get('tool_calls') or []
                    for tc in tool_calls:
                        name = tc.get('tool_name') or tc.get('name', '?')
                        args = tc.get('args') or tc.get('arguments') or {}
                        if name == 'ask_user':
                            q = args.get('question', '')
                            candidates = args.get('candidates') or []
                            if candidates:
                                q += '\n' + '\n'.join(f'- {c}' for c in candidates)
                            parts.append(q)
                        else:
                            args_s = json.dumps(args, ensure_ascii=False)
                            if len(args_s) > 200:
                                args_s = args_s[:200] + '...'
                            parts.append(f"`{name}`: {args_s}")
                    display = "\n\n".join(p for p in parts if p) or display
                card.done(display)
                on_final(raw)
                done_event.set()
            elif ctx.get('summary'):
                detail = _build_step_detail(ctx.get('response'), ctx.get('tool_calls') or [])
                card.step(ctx['summary'], detail)
        except Exception as e:
            print(f"[fs hook] error: {e}")
    return hook


def _ga_inbound_from_lark_event(message, sender, content_text="", resources=None):
    """Build GAInboundMessage from the legacy lark_oapi event objects.

    This is an observe-only bridge for the Channel migration: callers keep using
    existing fsapp parsing/authorization behavior while a stable GA message model
    becomes available for future replacements.
    """
    sender_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
    raw = {
        "message_type": getattr(message, "message_type", None),
        "chat_id": getattr(message, "chat_id", None),
        "message_id": getattr(message, "message_id", None),
    }
    try:
        mentions = getattr(message, "mentions", None) or []
    except Exception:
        mentions = []
    try:
        reply_to = getattr(message, "parent_id", None) or getattr(message, "root_id", None)
    except Exception:
        reply_to = None
    return GAInboundMessage.from_feishu_channel({
        "message_id": getattr(message, "message_id", "") or "",
        "chat_id": getattr(message, "chat_id", None),
        "chat_type": getattr(message, "chat_type", None),
        "sender_id": sender_id,
        "sender_name": getattr(sender, "sender_type", None),
        "content_text": content_text or "",
        "reply_to_message_id": reply_to,
        "mentions": mentions,
        "mentioned_bot": bool(mentions),
        "mentioned_all": False,
        "resources": resources or [],
        "raw": raw,
    })


def handle_message(data):
    event, message, sender = data.event, data.event.message, data.event.sender
    msg_id = getattr(message, "message_id", "") or ""
    if msg_id and not _message_claim_once(msg_id):
        print(f"[INFO] duplicate Feishu message ignored: {msg_id}")
        return
    open_id = sender.sender_id.open_id
    chat_id = message.chat_id
    incoming_message_id = message.message_id
    if not PUBLIC_ACCESS and open_id not in ALLOWED_USERS:
        print(f"未授权用户: {open_id}")
        return
    user_input, image_paths = _build_user_message(message)
    ga_msg = _ga_inbound_from_lark_event(
        message,
        sender,
        content_text=user_input,
        resources=[{"type": "image", "name": path, "key": path} for path in image_paths],
    )
    if not user_input:
        if chat_id:
            send_message(chat_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}", receive_id_type="chat_id")
        else:
            send_message(open_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}")
        return
    print(f"收到消息 [{open_id}] ({message.message_type}, {len(image_paths)} images, ga_msg={ga_msg.message_id}): {user_input[:200]}")
    if message.message_type == "text" and user_input.startswith("/"):
        return handle_command(open_id, user_input, chat_id)

    def run_agent():
        user_tasks[open_id] = {"running": True}
        receive_id = chat_id or open_id
        rid_type = "chat_id" if chat_id else "open_id"
        typing_reaction_id = None
        if incoming_message_id:
            typing_reaction_id = add_message_reaction_get_id(incoming_message_id, TYPING_REACTION_EMOJI)
        done_event = threading.Event()
        ask_user_events = Q.Queue()
        hook_key = f"fs_{open_id}"
        card = _TaskCard(receive_id, rid_type)
        card.start()
        final_raw_holder = {"text": ""}
        def on_final(raw):
            final_raw_holder["text"] = raw or ""
            _send_generated_files(receive_id, raw, receive_id_type=rid_type)
        def on_ask_user(event):
            ask_user_events.put(event)
        if not hasattr(agent, '_turn_end_hooks'): agent._turn_end_hooks = {}
        agent._turn_end_hooks[hook_key] = _make_task_hook(card, done_event, on_final, on_ask_user)
        try:
            # Keep the display_queue as the authoritative completion channel.
            # The turn_end_hook updates the rich card per turn, but startup/cold-run
            # races or hook errors must not leave Feishu waiting forever.
            dq = agent.put_task(user_input, source="feishu", images=image_paths)
            start = time.time()
            while True:
                # Drain any agent output first; this is independent of desktop/Streamlit UI.
                try:
                    while True:
                        item = dq.get_nowait()
                        if 'done' in item:
                            raw = item.get('done', '')
                            if not done_event.is_set():
                                card.done(_display_text(raw))
                                on_final(raw)
                                done_event.set()
                            break
                except Q.Empty:
                    pass
                if done_event.is_set():
                    break
                if not user_tasks.get(open_id, {}).get("running", True):
                    agent.abort()
                    card.fail("已停止")
                    break
                try:
                    ask_event = ask_user_events.get_nowait()
                except Q.Empty:
                    ask_event = None
                if ask_event:
                    choice = send_exec_approval(
                        receive_id=receive_id,
                        receive_id_type=rid_type,
                        session_key=f"ask-user:{open_id}:{int(time.time())}",
                        cmd=_format_ask_user_cmd(ask_event),
                        reason="agent requested human approval via ask_user",
                        requester_open_id=open_id,
                        timeout_sec=AGENT_TIMEOUT_SEC,
                    )
                    selected = _select_ask_user_candidate(ask_event, choice)
                    card.step("审批结果", f"{choice}: {selected}")
                    agent.put_task(selected, source="feishu")
                    start = time.time()
                    continue
                if time.time() - start > AGENT_TIMEOUT_SEC:
                    agent.abort()
                    card.fail("任务超时")
                    break
                done_event.wait(timeout=0.5)
        except Exception as e:
            traceback.print_exc()
            card.fail(f"错误: {e}")
        finally:
            if incoming_message_id:
                delete_own_reaction_by_type(incoming_message_id, TYPING_REACTION_EMOJI, typing_reaction_id)
            agent._turn_end_hooks.pop(hook_key, None)
            user_tasks.pop(open_id, None)

    threading.Thread(target=run_agent, daemon=True).start()


def handle_command(open_id, cmd, chat_id=None):
    def _send_cmd_response(content):
        if chat_id:
            send_message(chat_id, content, receive_id_type="chat_id")
        else:
            send_message(open_id, content)
    parts = (cmd or "").split()
    op = (parts[0] if parts else "").lower()
    if op == "/stop":
        if open_id in user_tasks:
            user_tasks[open_id]["running"] = False
        agent.abort()
        _send_cmd_response("正在停止...")
    elif op == "/new":
        _send_cmd_response(reset_conversation(agent))
    elif op == "/help":
        _send_cmd_response("命令列表:\n/stop - 停止当前任务\n/status - 查看状态\n/llm - 查看当前模型列表\n/llm [n] - 切换到第 n 个模型\n/restore - 恢复上次对话历史\n/continue - 列出可恢复会话\n/continue [n] - 恢复第 n 个会话\n/new - 开启新对话并清空当前上下文\n/edit last <文本> - 编辑最近一条机器人消息\n/recall last - 撤回最近一条机器人消息\n/react <last|message_id> <emoji_type> - 添加表情回应\n/reactions <last|message_id> [emoji_type] - 查看回应摘要\n/help - 显示帮助")
    elif op == "/status":
        llm = agent.get_llm_name() if agent.llmclient else "未配置"
        _send_cmd_response(f"状态: {'🔴 运行中' if agent.is_running else '🟢 空闲'}\nLLM: [{agent.llm_no}] {llm}")
    elif op == "/llm":
        if not agent.llmclient:
            return _send_cmd_response("❌ 当前没有可用的 LLM 配置")
        if len(parts) > 1:
            try:
                agent.next_llm(int(parts[1]))
                return _send_cmd_response(f"✅ 已切换到 [{agent.llm_no}] {agent.get_llm_name()}")
            except Exception:
                return _send_cmd_response(f"用法: /llm <0-{len(agent.list_llms()) - 1}>")
        lines = [f"{'→' if cur else '  '} [{i}] {name}" for i, name, cur in agent.list_llms()]
        _send_cmd_response("LLMs:\n" + "\n".join(lines))
    elif op == "/edit":
        if len(parts) < 3:
            return _send_cmd_response("用法: /edit <last|message_id> <新文本>")
        target = _resolve_sent_target(parts[1], chat_id or open_id)
        if not target:
            return _send_cmd_response("❌ 没有可编辑的最近消息")
        if not _is_own_sent_message(target):
            return _send_cmd_response("❌ 只能编辑本机器人已记录的消息")
        new_text = (cmd or "").split(None, 2)[2]
        ok, _ = edit_text_message(target, new_text)
        _send_cmd_response("✅ 已编辑" if ok else "❌ 编辑失败")
    elif op == "/recall":
        target = _resolve_sent_target(parts[1] if len(parts) > 1 else "last", chat_id or open_id)
        if not target:
            return _send_cmd_response("❌ 没有可撤回的最近消息")
        if not _is_own_sent_message(target):
            return _send_cmd_response("❌ 只能撤回本机器人已记录的消息")
        ok, _ = recall_message(target)
        _send_cmd_response("✅ 已撤回" if ok else "❌ 撤回失败")
    elif op == "/react":
        if len(parts) < 3:
            return _send_cmd_response("用法: /react <last|message_id> <emoji_type>")
        target = _resolve_sent_target(parts[1], chat_id or open_id)
        if not target:
            return _send_cmd_response("❌ 没有可回应的消息")
        ok, _ = add_message_reaction(target, parts[2])
        _send_cmd_response("✅ 已添加 reaction" if ok else "❌ 添加 reaction 失败")
    elif op == "/reactions":
        if len(parts) < 2:
            return _send_cmd_response("用法: /reactions <last|message_id> [emoji_type]")
        target = _resolve_sent_target(parts[1], chat_id or open_id)
        if not target:
            return _send_cmd_response("❌ 没有可查询的消息")
        data, _ = list_message_reactions(target, parts[2] if len(parts) > 2 else None)
        if data is None:
            return _send_cmd_response("❌ 查询 reaction 失败")
        items = getattr(data, "items", None) or []
        lines = [f"reactions: {len(items)}"]
        for item in items[:10]:
            rtype = getattr(getattr(item, "reaction_type", None), "emoji_type", None) or getattr(item, "reaction_type", "")
            rid = getattr(item, "reaction_id", "")
            lines.append(f"- {rtype} {rid}")
        _send_cmd_response("\n".join(lines))
    elif op == "/restore":
        try:
            restored_info, err = format_restore()
            if err:
                return _send_cmd_response(err.replace("❌ ", ""))
            restored, fname, count = restored_info
            agent.history.extend(restored)
            agent.abort()
            _send_cmd_response(f"已恢复 {count} 轮对话\n来源: {fname}\n(仅恢复上下文，请输入新问题继续)")
        except Exception as e:
            _send_cmd_response(f"恢复失败: {e}")
    elif op == "/continue" or cmd.startswith("/continue"):
        _send_cmd_response(handle_continue_frontend(agent, cmd))
    else:
        _send_cmd_response(f"未知命令: {cmd}")


def main():
    global client
    if not APP_ID or not APP_SECRET:
        print("错误: 请在 mykey.py 或 mykey.json 中配置 fs_app_id 和 fs_app_secret")
        sys.exit(1)
    client = create_client()
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )
    print("=" * 50 + "\n飞书 Agent 已启动（长连接模式）\n" + f"App ID: {APP_ID}\n等待消息...\n" + "=" * 50)
    retry_delay = 5
    while True:
        try:
            cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
            cli.start()
        except Exception as e:
            print(f"[WARN] 飞书长连接断开或启动失败: {e}")
        print(f"[INFO] {retry_delay}s 后重连...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 120)
        # 重连时刷新 client
        try:
            client = create_client()
        except Exception:
            pass


if __name__ == "__main__":
    main()
