# -*- coding: utf-8 -*-
"""PDD 网页版 WS 帧协议解析 (来自对 socketUtil.handleSocket/regWebSocket 的还原)"""
import json

from ..parser import ACCOUNT_RE, _canonical_account


def _pick(d, *paths, default=None):
    for p in paths:
        cur = d
        ok = True
        for k in p.split("."):
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and cur is not None:
            return cur
    return default


def parse_frame(raw):
    """把一条 WS 帧解析成统一结构。raw 是字符串(JSON)或 dict。"""
    if isinstance(raw, dict):
        obj = raw
    else:
        try:
            obj = json.loads(raw)
        except Exception:
            return {"kind": "unparsable", "raw": raw}
    if not isinstance(obj, dict):
        return {"kind": "unparsable", "raw": raw}

    cmd = obj.get("response") or obj.get("cmd")
    msg = obj.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}

    kind = "unknown"
    if cmd == "push":
        kind = "push"           # 买家/系统消息推送
    elif cmd == "send_message":
        kind = "send_message"   # 发送回执 / 自回声
    elif cmd == "list":
        kind = "list"
    elif cmd == "auth":
        kind = "auth"
    elif cmd == "heartbeat":
        kind = "heartbeat"
    elif cmd in ("conciliation_msg", "mall_system_msg"):
        kind = cmd
    elif cmd == "sync_card_status":
        kind = "sync_card_status"
    elif cmd == "push_read_state":
        kind = "push_read_state"

    # 从 message 或顶层取角色/uid/content
    from_ = msg.get("from") or obj.get("from") or {}
    to_ = msg.get("to") or obj.get("to") or {}
    if isinstance(from_, str):
        from_ = {"uid": from_}
    if isinstance(to_, str):
        to_ = {"uid": to_}

    role = from_.get("role") or (to_.get("role") == "user" and "buyer" or None)
    uid = from_.get("uid") or to_.get("uid")
    content = msg.get("content") if msg else None
    if content is None:
        content = obj.get("content")

    return {
        "kind": kind,
        "cmd": cmd,
        "request_id": obj.get("request_id"),
        "msg_id": msg.get("msg_id") or obj.get("msg_id"),
        "from_role": from_.get("role"),
        "to_role": to_.get("role"),
        "uid": uid,
        "csid": from_.get("csid") or to_.get("csid") or obj.get("csid"),
        "content": content,
        # 注意 type=0 是文本消息, 不能用 `or` 否则 0 被吞成 None
        "type": msg.get("type") if msg.get("type") is not None else obj.get("type"),
        "ts": msg.get("ts") or obj.get("ts"),
        "pre_msg_id": msg.get("pre_msg_id") or obj.get("pre_msg_id"),
        "obj": obj,
    }


def _canonical_or_none(value) -> str | None:
    """帧里的 csid 不一定是账号（工作台内部 id 形如 pdd42730237415）。
    只有规范化后真的是 cs 账号才回传，否则交给调用方继续推导。"""
    canonical = _canonical_account(value or "")
    return canonical if ACCOUNT_RE.fullmatch(canonical) else None


def seller_id(parsed):
    """从帧显式 csid，或 mall_cs 端点 + 外层 target_id 推导客服账号。"""
    explicit = _canonical_or_none(parsed.get("csid"))
    if explicit:
        return explicit
    obj = parsed.get("obj") or {}
    msg = obj.get("message") or {}
    frm = msg.get("from") or obj.get("from") or {}
    to = msg.get("to") or obj.get("to") or {}
    mall_cs = frm if isinstance(frm, dict) and frm.get("role") == "mall_cs" else to
    if not isinstance(mall_cs, dict):
        return None
    mall_id = str(mall_cs.get("mall_id") or mall_cs.get("mallId") or mall_cs.get("uid") or "")
    target_id = str(obj.get("target_id") or obj.get("targetId") or msg.get("target_id") or "")
    if mall_id.startswith("cs_"):
        return mall_id
    if mall_id.isdigit() and target_id.isdigit():
        return "cs_%s:%s" % (mall_id, target_id)
    return None


def buyer_uid(endpoints, seller=""):
    """从帧的两端里挑出买家 uid。

    不能盲信某一端：客服自己发出的那条消息被 push 回来时（或 send_message 回执
    帧的 to 端是席位时），uid 会是席位自己 —— 那会在中心生成一个只含 AI 消息的
    “影子会话”。席位 uid 就是客服账号 cs_<mall>:<uid> 里的 uid。
    """
    seat_uid = str(seller or "").partition(":")[2]
    rows = [e for e in endpoints if isinstance(e, dict)]
    ordered = [e for e in rows if str(e.get("role") or "").lower() == "user"]
    ordered += [e for e in rows if str(e.get("role") or "").lower() != "user"]
    for endpoint in ordered:
        uid = str(endpoint.get("uid") or "").strip()
        if uid and uid != seat_uid:
            return uid
    return ""


def buyer_message(parsed):
    """push 帧 → 买家消息 dict (与探域 buyer_msg 对齐的字段)"""
    m = parsed["obj"]
    msg = m.get("message") or {}
    frm = msg.get("from") or m.get("from") or {}
    if not isinstance(frm, dict):
        frm = {}
    role = frm.get("role") or parsed["from_role"]
    sid = seller_id(parsed)
    uid = buyer_uid([msg.get("to"), msg.get("from"), m.get("to")], sid) or frm.get("uid") or parsed["uid"]
    return {
        "push_type": "buyer_msg",
        "from_role": role,
        "to_role": parsed["to_role"],
        "buyer_id": uid,
        "seller_id": sid or _canonical_or_none(frm.get("csid")),
        "nickname": msg.get("nickname"),
        "client_msg_id": msg.get("client_msg_id"),
        "msg_id": parsed["msg_id"],
        "pre_msg_id": parsed["pre_msg_id"],
        "content": parsed["content"],
        "type": parsed["type"],
        "ts": parsed["ts"],
        "raw": m,
    }


def messages_from_list(parsed):
    """list 帧 → [消息dict, ...] (每条消息的字段布局与 push 的 message 相同)"""
    obj = parsed["obj"]
    out = []
    for m in obj.get("messages") or []:
        if not isinstance(m, dict):
            continue
        frm = m.get("from") or {}
        if not isinstance(frm, dict):
            frm = {}
        out.append({
            "from_role": frm.get("role"),
            "to_role": (m.get("to") or {}).get("role") if isinstance(m.get("to"), dict) else None,
            "buyer_id": buyer_uid([m.get("to"), m.get("from")], _canonical_or_none(frm.get("csid")) or "") or frm.get("uid"),
            "seller_id": frm.get("csid"),
            "msg_id": m.get("msg_id"),
            "pre_msg_id": m.get("pre_msg_id"),
            "content": m.get("content"),
            "type": m.get("type"),
            "ts": m.get("ts"),
            "raw": m,
        })
    return out
