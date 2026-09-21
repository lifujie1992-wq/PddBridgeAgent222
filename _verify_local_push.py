# -*- coding: utf-8 -*-
"""验证 BridgeAgent 本地工作台推送：起假 gateway(18799) 收 POST，调 _on_local_event 看请求到达。"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")
captured = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8")
        captured.append({
            "path": self.path,
            "auth": self.headers.get("X-Agent-Token"),
            "agent_id_hdr": self.headers.get("X-Agent-Id"),
            "body": json.loads(body),
        })
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 18799), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

from bridge.agent import BridgeAgent  # noqa: E402

cfg = {
    "server_url": "http://127.0.0.1:18799",
    "agent_token": "tok-test",
    "agent_id": "agent-t",
    "agent_name": "",
    "device_id": "",
    "tanyu_log_dir": "D:\\nope",
    "platform": "pdd",
    "data_source": "cdp",
    "local_workbench_url": "http://127.0.0.1:18799",
    "local_seat_push": "true",
}
a = BridgeAgent(cfg)
assert a._local_ingest_url == "http://127.0.0.1:18799/api/local-seat/v1/events", a._local_ingest_url

ev = {
    "type": "message", "platform": "pdd", "msg_id": "m1", "idempotency_key": "ik1",
    "platform_message_id": "m1", "identity_kind": "stable_hash", "buyer_id": "u1",
    "role": "user", "content": "你好", "ts": 1756000000, "platform_ts_key": "0",
    "account": "cs_123:0", "shop_id": "mall_123", "buyer_nick": "买家", "source": "pdd_cdp",
}
a._on_local_event(ev)
time.sleep(1.2)
assert len(captured) == 1, captured
c = captured[0]
assert c["path"] == "/api/local-seat/v1/events", c["path"]
assert c["auth"] == "tok-test", c["auth"]
assert c["agent_id_hdr"] == "agent-t", c["agent_id_hdr"]
e = c["body"]["events"][0]
assert e["buyer_id"] == "u1" and e["content"] == "你好", e
assert e["source"] == "pdd_cdp" and e["type"] == "message", e
print("LOCAL PUSH OK path=%s buyer=%s content=%s" % (c["path"], e["buyer_id"], e["content"]))

# 容错：gateway 死端口时 _on_local_event 不抛、主队列照常
a._local_ingest_url = "http://127.0.0.1:19999/api/local-seat/v1/events"
a._on_local_event(ev)
time.sleep(1.0)
with a._pending_lock:
    assert len(a._pending) == 2, len(a._pending)
print("TOLERANCE OK pending=%d (死端口不阻塞)" % len(a._pending))
print("\nALL CHECKS PASSED")
