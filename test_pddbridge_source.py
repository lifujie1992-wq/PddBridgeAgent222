# -*- coding: utf-8 -*-
"""PddbridgeSource 单测：合成帧解析 + 归一化 + 去重 + 回执 + CDP 辅助函数分发。

跑法: python test_pddbridge_source.py （全绿打印 ALL CHECKS PASSED）
"""
from __future__ import annotations

import json
import sys
import time

from bridge.pddbridge.protocol import buyer_message, messages_from_list, parse_frame, seller_id
from bridge.pddbridge_source import (
    PddbridgeSource,
    _SendWaiter,
    channel_status_cdp,
    open_chat_pdd_cdp,
    send_text_cdp,
)

src = PddbridgeSource({})

# 1. push 帧归一化（买家 user）—— type=0 文本不被吞
push = (
    '{"response":"push","message":{"from":{"role":"user","uid":"4764375385604",'
    '"csid":"cs_12345:678"},"to":{"role":"mall_cs"},"msg_id":"m1","pre_msg_id":"p0",'
    '"content":"你好","type":0,"ts":1756000000000,"nickname":"买家A"}}'
)
p = parse_frame(push)
bm = buyer_message(p)
msg = src._normalize_message(bm)
assert msg["role"] == "user" and msg["buyer_id"] == "4764375385604"
assert msg["account"] == "cs_12345:678", msg["account"]
assert msg["raw_type"] == 0, msg["raw_type"]          # type=0 不被 or 吞
assert msg["content"] == "你好" and msg["platform"] == "pdd"
assert msg["source"] == "pdd_cdp"
assert msg["parent_msg_id"] == "p0"
assert msg["buyer_nick"] == "买家A"
print("PASS 1 push 归一化 (raw_type=%s account=%s)" % (msg["raw_type"], msg["account"]))

# 1b. 当前 PDD push 不总带 csid：用收件店铺 uid + 外层 target_id 还原账号
push_without_csid = (
    '{"response":"push","target_id":164945148,"message":'
    '{"from":{"role":"user","uid":"4764375385604"},'
    '"to":{"role":"mall_cs","uid":"427302374"},"msg_id":"m1b",'
    '"content":"老板你好","type":0,"ts":1756000000000}}'
)
msg = src._normalize_message(buyer_message(parse_frame(push_without_csid)))
assert msg["buyer_id"] == "4764375385604"
assert msg["account"] == "cs_427302374:164945148", msg["account"]
print("PASS 1b 无 csid push 账号还原 (account=%s)" % msg["account"])

# 1c. 帧连 target_id 也不带时，使用聊天 execution context 的店铺账号
src._account_hint = "cs_427302374:164945148"
push_without_identity = (
    '{"response":"push","message":{"from":{"role":"user","uid":"4764375385604"},'
    '"to":{"role":"mall_cs","uid":"427302374"},"msg_id":"m1c",'
    '"content":"今天能发货吗","type":0,"ts":1756000000001}}'
)
msg = src._normalize_message(buyer_message(parse_frame(push_without_identity)))
assert msg["account"] == "cs_427302374:164945148", msg["account"]
print("PASS 1c execution context 账号兜底 (account=%s)" % msg["account"])

# 2. send_message 帧归一化（客服 mall_cs 双向）→ buyer_id 取 to.uid
send = (
    '{"response":"send_message","request_id":"r1","message":{"from":{"role":"mall_cs",'
    '"uid":"cs_12345:678"},"to":{"role":"user","uid":"4764375385604"},"msg_id":"m2",'
    '"content":"回你","type":0,"ts":1756000000001}}'
)
ps = parse_frame(send)
smsg = src._normalize_send_message(ps)
assert smsg["role"] == "mall_cs" and smsg["buyer_id"] == "4764375385604"
assert smsg["account"] == "cs_12345:678"
assert smsg["raw_type"] == 0
print("PASS 2 send_message 归一化 (buyer=%s)" % smsg["buyer_id"])

# 3. 回执关联: send_message 帧按 uid+content 前48 匹配 pending → confirmed
ev = _SendWaiter()
with src._lock:
    src._pending_sends["4764375385604|回你"] = {
        "uid": "4764375385604", "content": "回你", "event": ev, "deadline": time.time() + 5,
    }
src._handle_send_receipt(ps)
assert ev.is_set() and ev.result["status"] == "confirmed" and ev.result["via"] == "cdp+ws"
assert ev.result["request_id"] == "r1" and ev.result["msg_id"] == "m2"
print("PASS 3 回执关联 confirmed request_id=%s" % ev.result["request_id"])

# 4. send_and_wait 未 listening → failed（通道未启动）
r = src.send_and_wait("4764375385604", "hello", "cs_12345:678", timeout=1.0)
assert r["ok"] is False and r["status"] == "failed", r
print("PASS 4 send_and_wait failed(未启动)")

# 5. 去重
assert src._dedup("push", "m1") is False
assert src._dedup("push", "m1") is True, "同 (cmd,msg_id) 第二次应去重"
assert src._seen_before("m1") is False
assert src._seen_before("m1") is True, "跨帧同 msg_id 应去重"
print("PASS 5 (cmd,msg_id) + msg_id 去重")

# 6. list 帧 → messages_from_list
listf = (
    '{"response":"list","messages":['
    '{"from":{"role":"user","uid":"u9","csid":"cs_12345:678"},"to":{"role":"mall_cs"},'
    '"msg_id":"m9","content":"历史","type":0,"ts":1756000000002}]}'
)
pl = parse_frame(listf)
lst = messages_from_list(pl)
assert len(lst) == 1 and lst[0]["buyer_id"] == "u9" and lst[0]["content"] == "历史"
print("PASS 6 list 帧解析 (%d 条)" % len(lst))

# 7. channel_status_cdp: 无源 → 死端口 False; 带假源 listening → dll_ready
st = channel_status_cdp({})
assert st["source"] == "pdd_cdp"
print("PASS 7a channel_status_cdp(无源) source=%s cdp_port=%s" % (st["source"], st["cdp_port"]))


class _FakeSrc:
    last_error = ""

    def status(self):
        return {"state": "listening", "injected": True, "port": 57165}

    def send_and_wait(self, uid, content, account, **kw):
        return {"ok": True, "status": "confirmed", "real_send": True, "via": "cdp+ws",
                "request_id": "req-f", "msg_id": "m-f", "buyer_id": uid, "content": content}


st = channel_status_cdp({"_pddbridge_source": _FakeSrc()})
assert st["dll_ready"] is True and st["send_ready"] is True
assert isinstance(st["cdp_port"], int) and st["cdp_port"] > 0, st["cdp_port"]  # 真实扫描端口, 环境相关
print("PASS 7b channel_status_cdp(假源注入) dll_ready=%s port=%s" % (st["dll_ready"], st["cdp_port"]))

# 8. send_text_cdp: 无源 failed / dry_run accepted / 假源回执直通
r = send_text_cdp("uid1", "hi", "cs_s:1", cfg={})
assert r["ok"] is False and r["status"] == "failed"
r = send_text_cdp("uid1", "hi", "cs_s:1", cfg={}, dry_run=True)
assert r["status"] == "accepted" and r["via"] == "dry_run"
r = send_text_cdp("uid1", "hi", "cs_s:1", cfg={"_pddbridge_source": _FakeSrc()})
assert r["status"] == "confirmed" and r["via"] == "cdp+ws"
print("PASS 8 send_text_cdp 分支 (failed/dry_run/confirmed)")

# 9. open_chat_pdd_cdp → unsupported 引导
r = open_chat_pdd_cdp("uid1", "cs_s:1", cfg={})
assert r["status"] == "unsupported" and "手动" in r.get("error_user", "")
print("PASS 9 open_chat_pdd_cdp unsupported 引导")

# 10. 发送回执帧里的 csid 是工作台内部 id（pdd42730237415），不能当客服账号上传，
#     否则中心判 shop_not_active 直接拒收 —— 客服回复就永远进不了大脑。
send_frame = (
    '{"response":"send_message","csid":"pdd42730237415","message":{"from":{"role":"mall_cs","uid":"15"},'
    '"to":{"role":"user","uid":"4764375385604"},"content":"hi","msg_id":"m1","type":0},'
    '"target_id":"4764375385604"}'
)
parsed_send = parse_frame(send_frame)
sid = seller_id(parsed_send)
assert sid != "pdd42730237415", sid
send_msg = src._normalize_send_message(parsed_send)
assert send_msg is None or send_msg["account"] != "pdd42730237415", send_msg
assert seller_id({"csid": "cs_427302374:164945148"}) == "cs_427302374:164945148"
assert seller_id({"csid": "427302374:164945148"}) == "cs_427302374:164945148"
print("PASS 10 畸形 csid 不当账号用 (seller_id=%r)" % (sid,))

# 11. 回执帧带席位 uid：不能把客服自己发的消息记成“买家=席位”，也不能把确认漏掉。
#     大脑出现“两个会话”（一个完整对话 + 一个只含 AI 发言）就是这个 bug。
SEAT_ACCOUNT = "cs_427302374:164945148"
REAL_BUYER = "4764375385604"
shadow_src = PddbridgeSource({})
shadow_src._account_hint = SEAT_ACCOUNT
waiter = _SendWaiter()
waiter.deadline = time.monotonic() + 5
shadow_src._pending_sends["%s|%s" % (REAL_BUYER, "你好")] = {
    "uid": REAL_BUYER, "content": "你好", "event": waiter,
    "deadline": time.time() + 5, "account": SEAT_ACCOUNT,
}
seat_receipt = json.dumps({
    "response": "send_message", "csid": "pdd42730237415",
    "message": {"from": {"role": "mall_cs", "uid": "15"},
                "to": {"role": "mall_cs", "uid": "164945148"},
                "content": "你好", "msg_id": "seat-1", "type": 0},
})
parsed_seat = parse_frame(seat_receipt)
seat_msg = shadow_src._normalize_send_message(parsed_seat)
assert seat_msg is not None, "不应因为 to.uid 是席位就把消息丢掉"
assert seat_msg["buyer_id"] == REAL_BUYER, seat_msg["buyer_id"]
assert seat_msg["account"] == SEAT_ACCOUNT, seat_msg["account"]
shadow_src._handle_send_receipt(parsed_seat)
assert shadow_src._pending_sends == {}, "回执应按内容匹配上并清空待确认表"
assert waiter.result and waiter.result["status"] == "confirmed"
assert waiter.result["buyer_id"] == REAL_BUYER, waiter.result["buyer_id"]
print("PASS 11 回执带席位 uid 也能归到真买家 %s" % (REAL_BUYER,))

print("\nALL CHECKS PASSED")
