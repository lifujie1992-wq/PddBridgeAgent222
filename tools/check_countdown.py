"""验证倒计时修复：1) mall_cs(indeterminate) 也清倒计时；2) 发送后会补一条本地 mall_cs 事件。"""
import queue
import sys
import time
import types

sys.path.insert(0, r"C:\Users\Administrator\Documents\Codex\2026-09-29\x20\work\github-pdd")

import run_frontend_service as fs  # noqa: E402
from bridge.agent import BridgeAgent as PddAgent  # noqa: E402


def wait_state(messages):
    obj = types.SimpleNamespace(
        _message_sort_key=fs.LocalSeatState._message_sort_key.__get__(None, object)
    )
    return fs.LocalSeatState._reply_wait_state(obj, messages)


base = [
    {"role": "user", "ts": 100.0, "content": "便宜些", "msg_id": "1"},
]
print("① 只有买家消息            ->", wait_state(base))
for status in ("", "confirmed", "indeterminate", "failed"):
    msgs = base + [{"role": "mall_cs", "ts": 101.0, "content": "好的",
                    "msg_id": "2", "delivery_status": status}]
    state = wait_state(msgs)
    print("② 我方回复 delivery=%-14r -> waiting=%s" % (status, state["waiting_for_reply"]))

# ③ 发送后是否补了本地事件
cfg = {"dual_write_local_workbench": True, "agent_token": "t", "agent_id": "a"}
self = types.SimpleNamespace(
    cfg=cfg,
    platform=types.SimpleNamespace(name="pdd"),
    _local_ingest_queue=queue.Queue(),
)
PddAgent._publish_self_reply(self, "cs_1_2", "4764", "好的，这个价可以",
                             {"ok": True, "status": "indeterminate", "msg_id": ""})
ev = self._local_ingest_queue.get_nowait()
print("③ 补发的本地事件 -> role=%s delivery=%s account=%s buyer=%s" % (
    ev["role"], ev["delivery_status"], ev["account"], ev["buyer_id"]))
print("   清倒计时吗 ->", wait_state(base + [{
    "role": ev["role"], "ts": ev["ts"], "content": ev["content"],
    "delivery_status": ev["delivery_status"], "msg_id": "x"}]))
