# -*- coding: utf-8 -*-
r"""抓「工作台发图片」的网络请求：上传接口 + 发送参数。

用法：
    py capture_image_upload.py 120        # 抓 120 秒，输出到 %TEMP%\pdd_img_capture.log
然后在工作台里手动发一张图。脚本会记录：
  * 所有发往 /upload /pic /img /oss /chat-img 的请求（URL、方法、请求体大小/前 800 字）
  * 所有 WebSocket 帧里含 chat-img 或 image 的文本（前 800 字）
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\Administrator\Documents\Codex\2026-09-29\x20\work\github-pdd")
from bridge.pddbridge import cdp as C  # noqa: E402

PORT = None   # 运行时自动发现（工作台重启后端口会变）
INTEREST = re.compile(r"(?i)(upload|/pic|/img|chat-img|image|oss|file)")
OUT = os.path.join(os.environ["TEMP"], "pdd_img_capture.log")
lock = threading.Lock()


def log(prefix: str, text: str) -> None:
    line = "%s  %s\n" % (time.strftime("%H:%M:%S"), text)
    with lock:
        with open(OUT, "a", encoding="utf-8") as handle:
            handle.write(line)
    print(prefix, text[:200])


class Target:
    def __init__(self, cdp, cid: int, label: str):
        self.cdp = cdp
        self.cid = cid
        self.label = label
        self.requests: dict = {}

    def eval(self, expr):
        try:
            return self.cdp.eval(expr, self.cid, timeout=5)
        except Exception:
            return None


def hook_script(label: str) -> str:
    """在页面里包装 XHR/fetch/WebSocket，把关心的事件推到 CDP 的 console（我们轮询读）。"""
    return r"""(function(){
      if (window.__imgcapInstalled) return 'already';
      window.__imgcapInstalled = 1;
      window.__imgcap = [];
      function rec(kind, payload){
        try { window.__imgcap.push({kind: kind, at: Date.now(), payload: payload}); } catch(e){}
        if (window.__imgcap.length > 200) window.__imgcap.shift();
      }
      var oOpen = XMLHttpRequest.prototype.open;
      var oSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url){
        this.__imgcap_url = String(url); this.__imgcap_method = String(method);
        return oOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function(body){
        var url = this.__imgcap_url || '';
        if (/upload|\/pic|\/img|chat-img|image|oss|file/i.test(url)) {
          var preview = '';
          try {
            if (typeof body === 'string') preview = body.slice(0, 800);
            else if (body && body.size) preview = '[binary ' + body.size + ' bytes] ' + (body.type || '');
            else if (body && body.byteLength) preview = '[binary ' + body.byteLength + ' bytes]';
            else preview = String(body).slice(0, 200);
          } catch(e) { preview = 'ERR'; }
          var self = this;
          this.addEventListener('load', function(){
            var text = '';
            try { text = String(self.responseText || '').slice(0, 800); } catch(e) {}
            rec('xhr', {method: self.__imgcap_method, url: url, body: preview,
                        status: self.status, resp: text});
          });
        }
        return oSend.apply(this, arguments);
      };
      var oFetch = window.fetch;
      if (oFetch) {
        window.fetch = function(input, init){
          var url = (typeof input === 'string') ? input : (input && input.url) || '';
          if (/upload|\/pic|\/img|chat-img|image|oss|file/i.test(url)) {
            var body = init && init.body;
            var preview = '';
            try {
              if (typeof body === 'string') preview = body.slice(0, 800);
              else if (body && body.size) preview = '[binary ' + body.size + ' bytes] ' + (body.type || '');
              else preview = String(body);
            } catch(e) { preview = 'ERR'; }
            rec('fetch', {url: url, method: (init && init.method) || 'GET', body: preview});
          }
          return oFetch.apply(this, arguments);
        };
      }
      return 'installed';
    })()"""


def detect_port() -> int:
    """工作台重启后调试端口会变（57165 → 57166 …），必须现查。"""
    ports = C.discover_ports(only_pdd=True)
    for port in sorted(ports):
        try:
            if C.is_alive(port) and C.list_targets(port):
                return port
        except Exception:
            continue
    raise SystemExit("没有可用的 CDP 端口（工作台没开调试端口？）")


def main(seconds: int) -> None:
    global PORT
    PORT = detect_port()
    print("CDP 端口:", PORT)
    open(OUT, "w", encoding="utf-8").close()
    log("==", "开始抓包，等待在工作台里发一张图片…")
    targets = []
    for t in C.list_targets(PORT):
        url = t.get("url") or ""
        if not t.get("webSocketDebuggerUrl"):
            continue
        if not any(k in url for k in ("win-plugin", "mms.pinduoduo.com", "middle_panel", "right_panel")):
            continue
        try:
            cdp = C.Cdp(t["webSocketDebuggerUrl"], timeout=6)
            ctxs = cdp.enable_runtime()
        except Exception as exc:
            log("!!", "attach 失败 %s: %s" % (url[:60], exc))
            continue
        cid = min(ctxs.keys()) if ctxs else None
        if cid is None:
            continue
        target = Target(cdp, cid, url.split("/view/")[-1][:30] or url[:30])
        result = target.eval(hook_script(target.label))
        log("++", "已挂 %s (ctx=%s) -> %s" % (target.label, cid, result))
        targets.append(target)

    end = time.time() + seconds
    seen = 0
    seen_items = set()
    hooked = {(t.label, t.cid) for t in targets}
    last_scan = 0.0
    while time.time() < end:
        # 聊天区可能是「打开会话后才创建的 iframe 上下文」：每 3 秒重扫上下文并补挂，
        # 否则钩子只存在于主框架，发图时一个请求都抓不到（第一轮就栽在这里）。
        if time.time() - last_scan > 3.0:
            last_scan = time.time()
            for target in targets:
                try:
                    target.cdp.enable_runtime()          # 刷新 contexts 列表
                except Exception:
                    continue
                for cid in list(target.cdp.contexts().keys()):
                    if (target.label, cid) in hooked:
                        continue
                    try:
                        if target.cdp.eval(hook_script(target.label), cid) == "installed":
                            hooked.add((target.label, cid))
                            log("++", "补挂 %s ctx=%s" % (target.label, cid))
                    except Exception:
                        continue
        for target in targets:
            for cid in list(target.cdp.contexts().keys()):
                try:
                    raw = target.cdp.eval(
                        "JSON.stringify(window.__imgcap ? window.__imgcap.slice(0) : [])", cid, timeout=4)
                except Exception:
                    continue
                if not isinstance(raw, str) or len(raw) <= 4:
                    continue
                try:
                    items = json.loads(raw)
                except ValueError:
                    continue
                for item in items:
                    key = json.dumps(item, ensure_ascii=False)
                    if key in seen_items:
                        continue
                    seen_items.add(key)
                    log(">>", "%s %s" % (item.get("kind"), key[:900]))
        time.sleep(1.5)
    log("==", "抓包结束，输出：%s" % OUT)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
