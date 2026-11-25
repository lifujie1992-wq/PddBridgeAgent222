# -*- coding: utf-8 -*-
r"""本机回环 HTTP 工具：**永远绕过代理**。

现场事故（2026-11-25，KO5MD2C）：

  * PDD 工作台调试端口 `127.0.0.1:57165` 明明在监听，桥接却报
    「未发现可用 CDP 端口 discovered=[57165]」（`is_alive()` 返回 False）；
  * 本地网关 `127.0.0.1:18767` 也明明在监听，被判 `did not become healthy` 并循环重启。

两个都用 `urllib.request.urlopen` 探活，而 **urllib 会读 `HTTP_PROXY` / `http_proxy`
环境变量**，把 `http://127.0.0.1:...` 也塞给代理，于是本机回环全部探活失败。
机器上装了代理/VPN 客户端（还带 TUN）时必然复现，而且看起来像"程序坏了"。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

#: 不应该走代理的主机
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def active_proxy_env() -> dict:
    """返回当前设置了的代理相关环境变量（用于启动时提示，不打印用户名密码）。"""
    out = {}
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            out[key] = value
    return out


def is_local_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return host in LOCAL_HOSTS


def _open(request, timeout: float):
    # ProxyHandler({}) 表示"没有任何代理"：直连。回环地址不该经过代理，
    # 也不该受 NO_PROXY 写错的影响。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def opener(request, timeout: float = 1.0):
    """带请求对象的免代理打开（POST 等场景）。"""
    return _open(request, timeout)


def get_json(url: str, *, timeout: float = 1.0, headers: dict | None = None):
    """GET 一个本机 URL 并解析 JSON；失败抛异常，由调用方决定怎么兜。"""
    request = urllib.request.Request(url, headers=headers or {})
    with _open(request, timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def probe_json(url: str, *, timeout: float = 1.0) -> dict | None:
    """同 :func:`get_json`，但失败返回 None（探活场景）。"""
    try:
        payload = get_json(url, timeout=timeout)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None
