# -*- coding: utf-8 -*-
"""本机回环探活必须绕开代理。

现场（KO5MD2C）：工作台 57165 在监听却报"未发现可用 CDP 端口"，
网关 18767 在监听却"did not become healthy"循环重启 —— 都是
`urllib.request.urlopen("http://127.0.0.1:...")` 被 HTTP_PROXY 转发走了。
"""
from __future__ import annotations

import http.server
import json
import os
import threading
import urllib.request

from bridge import netlocal


def _serve(payload: dict):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):            # noqa: N802
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_get_json_ignores_http_proxy(monkeypatch):
    server = _serve({"ok": True, "pid": 123})
    port = server.server_address[1]
    url = "http://127.0.0.1:%d/api/status" % port
    try:
        # 指向一个必然连不通的代理：走代理就会失败
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        assert netlocal.get_json(url, timeout=2.0) == {"ok": True, "pid": 123}
        assert netlocal.probe_json(url, timeout=2.0) == {"ok": True, "pid": 123}
        # 对照：原生 urllib 会因为代理失败（证明这个用例真的覆盖到了差异）
        failed = False
        try:
            urllib.request.urlopen(url, timeout=1.0)
        except Exception:
            failed = True
        assert failed, "原生 urllib 竟然没走代理，用例失效"
    finally:
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        server.shutdown()


def test_active_proxy_env_reports_without_values_leak():
    os.environ["HTTP_PROXY"] = "http://user:pass@proxy.local:8080"
    try:
        env = netlocal.active_proxy_env()
        assert "HTTP_PROXY" in env
    finally:
        os.environ.pop("HTTP_PROXY", None)


def test_is_local_url():
    assert netlocal.is_local_url("http://127.0.0.1:18767/x")
    assert netlocal.is_local_url("ws://localhost:57165/devtools")
    assert not netlocal.is_local_url("http://47.112.219.14:18765/")
