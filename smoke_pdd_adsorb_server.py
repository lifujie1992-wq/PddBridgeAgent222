from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    web_root = Path.cwd()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/runtime-config":
            self.send_json({
                "ok": True,
                "service": "pdd-local-seat-gateway",
                "platform": "pdd",
                "ui_role": "seat",
                "seat_mode": True,
                "rbac_enabled": False,
            })
            return
        if path == "/api/auth/me":
            self.send_json({
                "ok": True,
                "authenticated": True,
                "rbac_enabled": False,
                "user": {
                    "username": "seat:smoke",
                    "display_name": "本机测试工位",
                    "role": "agent",
                    "role_label": "本机工位",
                    "enabled": True,
                    "system": False,
                    "perms": ["workbench.view", "workbench.reply", "workbench.export"],
                },
            })
            return
        if path == "/api/queue/handoff":
            self.send_json({
                "ok": True,
                "items": [{
                    "nickname": "测试买家",
                    "buyer_id": "smoke-buyer",
                    "shop_name": "拼多多测试店",
                    "shop_id": "smoke-shop",
                    "platform": "pdd",
                    "account": "smoke-account",
                    "last_text": "这是一条本地界面验证消息，不会发送到中心。",
                    "handoff_reason": "等待人工确认",
                    "last_ts": 1787150000000,
                }],
            })
            return
        if path == "/api/status":
            self.send_json({
                "ok": True,
                "watching": True,
                "brain_mode": "tanyu_shadow",
                "send_mode": "observe",
                "session_count": 1,
                "platform": "pdd",
                "dll_port": 19001,
            })
            return
        if path == "/api/sessions":
            session = {
                "nickname": "测试买家",
                "buyer_id": "smoke-buyer",
                "shop_name": "拼多多测试店",
                "shop_id": "smoke-shop",
                "platform": "pdd",
                "account": "smoke-account",
                "last_content": "这是一条完整工作台 dock 模式验证消息。",
                "last_role": "user",
                "handoff": True,
                "handoff_reason": "等待人工确认",
                "unread": 1,
                "msg_count": 1,
                "last_ts": 1787150000000,
            }
            self.send_json({
                "ok": True,
                "sessions": [session],
                "total": 1,
                "page": 1,
                "pages": 1,
                "counts": {"active": 1, "history": 0, "unread": 1, "handoff": 1, "all": 1},
                "shop_counts": {"smoke-shop": 1},
            })
            return
        if path == "/api/session/smoke-buyer":
            self.send_json({
                "ok": True,
                "nickname": "测试买家",
                "buyer_id": "smoke-buyer",
                "shop_name": "拼多多测试店",
                "shop_id": "smoke-shop",
                "platform": "pdd",
                "account": "smoke-account",
                "messages": [{
                    "msg_id": "smoke-message",
                    "role": "user",
                    "content": "这是一条完整工作台 dock 模式验证消息。",
                    "ts": 1787150000000,
                }],
                "message_total": 1,
                "has_more": False,
            })
            return
        relative = "index.html" if path == "/" else (
            "adsorb.html" if path in {"/adsorb", "/adsorb.html"} else path.lstrip("/")
        )
        target = (self.web_root / relative).resolve()
        try:
            target.relative_to(self.web_root.resolve())
        except ValueError:
            self.send_error(400)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        content_type = "application/javascript" if target.suffix == ".js" else "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/queue/jump":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_json({"ok": True, "hint": "本地冒烟跳转已接收"})
            return
        if path == "/api/clear_unread":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_json({"ok": True})
            return
        self.send_error(404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--web-root", type=Path, required=True)
    args = parser.parse_args()
    Handler.web_root = args.web_root.resolve()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
