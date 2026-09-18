/* pddbridge 注入脚本 —— 在聊天页面执行。幂等。 */
(function () {
  'use strict';
  /* ---- 升级自愈 ----
     每次注入都往 socket 上挂一条 message 监听，而旧脚本的监听摘不掉（当时没存句柄）。
     于是升级后页面上会同时有两条监听，每条入站帧被两个闭包各记一次：
       缓冲容量减半；上行每帧两份；溢出时旧闭包先 splice 且不落盘 —— spill 兜底被绕过。
     版本号对不上就只能整页刷新让窗口重来（刷新后 hooked 不存在，不会循环）。 */
  var BRIDGE_VER = 3;
  if (window.__pddBridge_hooked && window.__pddBridge_ver !== BRIDGE_VER) {
    var reloading = false;
    try { reloading = true; location.reload(); } catch (e) {}
    return { ok: true, attached: 0, reloading: reloading,
             previous: window.__pddBridge_ver || 'legacy' };
  }
  /* 版本相同（桥接重启等重复注入）：自己能把上一条监听摘掉，不必刷新页面 */
  try { if (typeof window.__pddBridge_off === 'function') window.__pddBridge_off(); } catch (e) {}
  window.__pddBridge_ver = BRIDGE_VER;
  var su = window.socketUtil;

  function rawFrame(e) {
    try {
      var d = e.data;
      if (typeof d === 'string') return d;
      if (d instanceof Blob) return 'BLOB:' + d.size;
      if (d instanceof ArrayBuffer) return 'AB:' + d.byteLength;
      if (d && d.data) return (typeof d.data === 'string') ? d.data : 'BYTES:' + (d.data.byteLength || 0);
      return String(d);
    } catch (err) { return 'ERR:' + err.message; }
  }
  function stData() {
    if (!window.__pddBridge_stats_data) {
      window.__pddBridge_stats_data = { recorded: 0, drained: 0, dedup_skip: 0, overflow_drop: 0,
                                        exact_repeat: 0, spilled: 0, spill_replayed: 0,
                                        spill_failed: 0, spill_evicted: 0,
                                        push_attempt: 0, pushed: 0, push_skip: 0, push_retry: 0 };
    }
    return window.__pddBridge_stats_data;
  }
  /* 判重表守卫。
     旧版按「长度 + 前 120 字符」判重, 而 PDD 帧里 msg_id 正好落在第 118~122 字符:
     同长度的连续消息 key 完全相同 → 第二条起被当重复整批丢掉（实调 6 条同长度消息只剩 1 条）。
     页面未刷新时旧包装仍在跑, 它读的就是这个全局 → 换成永不判重的对象, 误丢当场被拦住;
     真正的重复投递交给 Python 侧按 msg_id / event_id 去重(那一层本来就在)。 */
  function installDedupGuard() {
    var prev = window.__pddBridge_dedup;
    if (prev && prev.__pddBridgeGuard) return prev;
    var seen = [];
    var guard = {
      __pddBridgeGuard: 1,
      _blocked: 0,
      indexOf: function (key) {
        if (seen.indexOf(key) >= 0) guard._blocked++;
        return -1;                       /* 永不判重 */
      },
      push: function (key) {
        seen.push(key);
        if (seen.length > 30) seen.shift();
        return seen.length;
      },
      shift: function () { return seen.shift(); }
    };
    Object.defineProperty(guard, 'length', { get: function () { return seen.length; } });
    window.__pddBridge_dedup = guard;
    if (prev && typeof prev.length === 'number' && prev.length) guard._blocked += 0;
    return guard;
  }
  /* 推不出去才进缓冲, 由 drain 兜底 —— 每条帧只走一条路, 所以既不重复也不丢 */
  var BUFFER_MAX = 20000;   /* 1000 太小, 推送一挂就容易溢出丢最旧的帧 */
  function bufferEntry(entry) {
    var st = stData();
    var buf = window.__pddBridge_buf;
    buf.push(entry);
    if (buf.length > BUFFER_MAX) {
      var dropped = buf.slice(0, buf.length - BUFFER_MAX);
      if (spillWrite(dropped)) {
        buf.splice(0, dropped.length);
        st.overflow_drop += dropped.length;
      } else {
        st.spill_failed = (st.spill_failed || 0) + dropped.length;
      }
    }
  }
  function markPushFailed(entry) {
    window.__pddBridge_push_state = 'fail';
    stData().push_retry++;
    bufferEntry(entry);          /* 推送失败/非 2xx → 补进缓冲, 由 drain 兜底 */
    if (!window.__pddBridge_push_probe) {
      window.__pddBridge_push_probe = 1;
      setTimeout(function () {   /* 30 秒后再试一次, 免得一挂就永远退回轮询 */
        window.__pddBridge_push_probe = 0;
        window.__pddBridge_push_state = '';
      }, 30000);
    }
  }
  function pushEntry(entry) {
    var url = window.__pddBridge_push_url;
    if (!url || typeof fetch !== 'function') return false;
    var st = stData();
    if (window.__pddBridge_push_state === 'fail') { st.push_skip++; return false; }
    var payload = entry;
    if (window.__pddBridge_session) {
      payload = { sid: window.__pddBridge_session, t: entry.t, dir: entry.dir,
                  data: entry.data, chan: entry.chan, type: entry.type,
                  uid: entry.uid, content: entry.content };
    }
    var q = window.__pddBridge_pushq || (window.__pddBridge_pushq = []);
    q.push(payload);
    if (q.length >= PUSH_BATCH_MAX) { flushPushQueue(); return true; }
    if (!window.__pddBridge_push_timer) {
      window.__pddBridge_push_timer = setTimeout(flushPushQueue, PUSH_FLUSH_MS);
    }
    return true;
  }
  /* 批量推送：进线量大时一帧一 POST 会把浏览器连接池堵住，积压后只能退到缓冲，容易溢出丢最旧。
     改成攒一小批用一次 HTTP 发（数组体，Python 侧 _parse 原生支持），失败仍逐条入缓冲兜底。 */
  var PUSH_BATCH_MAX = 200;   /* 攒满即刻发 */
  var PUSH_FLUSH_MS = 60;     /* 否则最多等 60ms（本机回环，几乎不影响时延）*/
  function flushPushQueue() {
    var q = window.__pddBridge_pushq;
    if (!q || !q.length) return;
    window.__pddBridge_pushq = [];
    if (window.__pddBridge_push_timer) {
      clearTimeout(window.__pddBridge_push_timer);
      window.__pddBridge_push_timer = 0;
    }
    var st = stData();
    try {
      st.push_attempt++;
      fetch(window.__pddBridge_push_url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(q),
        keepalive: true
      }).then(function (r) {
        if (r && r.ok) { stData().pushed += q.length; window.__pddBridge_push_state = 'ok'; return; }
        for (var i = 0; i < q.length; i++) markPushFailed(q[i]);
      }).catch(function () {
        for (var i = 0; i < q.length; i++) markPushFailed(q[i]);
      });
    } catch (e) {
      window.__pddBridge_push_state = 'fail';
      for (var j = 0; j < q.length; j++) markPushFailed(q[j]);
    }
  }
  window.__pddBridge_flush_push = flushPushQueue;
  function record(entry) {
    /* 不再按载荷判重（旧实现会误吞连续同长消息）。只统计“字节完全相同的重复帧”作为观测,
       一条都不丢; 逻辑去重交给 Python 侧按 msg_id / event_id。 */
    var st = stData();
    if (entry.dir === 'in' && entry.data && typeof entry.data === 'string') {
      var key = entry.data.length + ':' + entry.data;
      var recent = window.__pddBridge_recent || (window.__pddBridge_recent = []);
      if (recent.indexOf(key) >= 0) {
        st.exact_repeat++;
      } else {
        recent.push(key);
        if (recent.length > 30) recent.shift();
      }
    }
    st.recorded++;
    if (!pushEntry(entry)) bufferEntry(entry);
  }
  function pushIn(e) {
    record({ t: Date.now(), dir: 'in', data: rawFrame(e) });
  }

  /* ---- 总是重建的接口 ---- */
  window.__pddBridge_buf = window.__pddBridge_buf || [];
  installDedupGuard();
  stData();
  window.__pddBridge_drain = function () {
    var b = window.__pddBridge_buf;
    window.__pddBridge_buf = [];
    stData().drained += b.length;
    return b;
  };
  /* 无损取帧：peek 只读不删，Python 侧确认收到后再 ack 删除。
     旧 drain 是「先清空再传输」，CDP 超时/丢包那批就永久消失（进线量大时正好命中）。 */
  window.__pddBridge_peek = function (n) {
    var b = window.__pddBridge_buf;
    var k = (typeof n === 'number' && n > 0 && b.length > n) ? n : b.length;
    return b.slice(0, k);
  };
  window.__pddBridge_ack = function (n) {
    var b = window.__pddBridge_buf;
    var k = (typeof n === 'number' && n > 0 && b.length > n) ? n : b.length;
    if (k > 0) {
      b.splice(0, k);
      stData().drained += k;
      var replayPending = window.__pddBridge_spill_replay_pending || 0;
      if (replayPending > 0) {
        replayPending -= Math.min(k, replayPending);
        window.__pddBridge_spill_replay_pending = replayPending;
        if (!replayPending) {
          try { window.localStorage.removeItem(SPILL_KEY); } catch (e) {}
        }
      }
    }
    return b.length;
  };
  window.__pddBridge_pending = function () { return window.__pddBridge_buf.length; };

  /* ---- 最后兜底：localStorage 即台账（探域「日志即台账」的浏览器等价物）----
     谁会被写进来：
       1) 缓冲溢出本要被丢的帧（最珍贵的未处理消息）
       2) 页面刷新/关闭时缓冲里还没推出去的帧
     什么时候读回来：下次注入脚本加载时，重放进缓冲随正常链路推给 Python，然后清除。
     重放的帧带 replayed=1，Python 侧按历史帧上报（is_history），不会触发对旧消息的回复。 */
  var SPILL_KEY = '__pddBridge_spill';
  var SPILL_MAX = 400;          /* localStorage 容量有限，最多兜 400 帧（约 1MB）*/
  function spillRead() {
    try {
      var v = window.localStorage.getItem(SPILL_KEY);
      if (!v) return [];
      var arr = JSON.parse(v);
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }
  function spillWrite(entries) {
    if (!entries || !entries.length) return false;
    try {
      var arr = spillRead();
      arr = arr.concat(entries);
      if (arr.length > SPILL_MAX) return false;
      window.localStorage.setItem(SPILL_KEY, JSON.stringify(arr));
      stData().spilled += entries.length;
      return true;
    } catch (e) { return false; }   /* localStorage 满/不可用：退回原状 */
  }
  function spillReplay() {
    var arr = spillRead();
    if (!arr.length) return 0;
    var pushUrl = window.__pddBridge_push_url;
    window.__pddBridge_push_url = '';
    for (var i = 0; i < arr.length; i++) {
      var e = arr[i];
      if (e && typeof e === 'object') {
        e.replayed = 1;
        e.t = e.t || Date.now();
        if (!e.dir) e.dir = 'in';
        record(e);
      }
    }
    window.__pddBridge_push_url = pushUrl;
    window.__pddBridge_spill_replay_pending = arr.length;
    stData().spill_replayed += arr.length;
    return arr.length;
  }
  window.__pddBridge_spill_flush = function () {     /* 页面刷新/关闭前调 */
    var b = window.__pddBridge_buf;
    if (b && b.length) return spillWrite(b);
    return false;
  };
  window.__pddBridge_spill_replay = spillReplay;     /* 也可手动触发重放 */
  window.addEventListener('beforeunload', function () {
    try { window.__pddBridge_spill_flush(); } catch (e) {}
  });
  spillReplay();
  /* 丢弃计数给 Python 侧对账; hooked 让 drain 能自证存活 */
  window.__pddBridge_stats = function () {
    var s = stData();
    var guard = window.__pddBridge_dedup;
    return {
      recorded: s.recorded, drained: s.drained,
      dedup_skip: (guard && guard._blocked) || 0,   /* 旧包装本会丢掉的条数(已被拦下) */
      exact_repeat: s.exact_repeat,
      overflow_drop: s.overflow_drop, push_attempt: s.push_attempt, pushed: s.pushed,
      spilled: s.spilled, spill_replayed: s.spill_replayed,
      spill_failed: s.spill_failed, spill_evicted: s.spill_evicted,
      push_skip: s.push_skip, push_retry: s.push_retry,
      push_state: window.__pddBridge_push_state || 'unknown',
      buf: window.__pddBridge_buf.length, buf_max: BUFFER_MAX, hooked: 1
    };
  };
  window.__pddBridge_sendText = function (uid, content, csid) {
    if (!su || typeof su.sendMsg !== 'function') return { ok: false, err: 'no socketUtil' };
    var opts = { uid: String(uid), content: String(content), cb: function () {} };
    if (csid) opts.csid = String(csid);
    try {
      var p = su.sendMsg(opts);
      return { ok: true, promise: !!p };
    } catch (e) { return { ok: false, err: e && e.message }; }
  };
  /* 直接按 uid 拉历史（服务端分页）：不必先 open_chat 让页面自己加载。
     页大小可自定（页面内置请求写死 20）；应答是 cmd:"list" 帧，走既有帧流。 */
  window.__pddBridge_pullHistory = function (uid, size, beginMsgId, startIndex, preMsgId) {
    var s = window.socketUtil;
    if (!s || typeof s.getChatRecord !== 'function') return { ok: false, err: 'no getChatRecord' };
    var n = (typeof size === 'number' && size > 0) ? size : 100;
    try {
      s.getChatRecord(String(uid), beginMsgId || 0, startIndex || 0, preMsgId || 0, n);
      return { ok: true, size: n };
    } catch (e) { return { ok: false, err: e && e.message }; }
  };
  window.__pddBridge_info = function () {
    var csidGuess = null;
    try {
      var keys = Object.keys(window).filter(function (k) { return /^global_(mall|shop|seller)/i.test(k) || /mallid|mall_id|sellerid|csid/i.test(k); });
      for (var i = 0; i < keys.length; i++) {
        var v = window[keys[i]];
        if (typeof v === 'string' && /^cs_\d+:\d+$/.test(v)) { csidGuess = v; break; }
      }
    } catch (e) {}
    var sock = su && su.socket;
    var sState = null, sUrl = null;
    if (sock) {
      try { sUrl = sock.url || null; } catch (e) {}
      try { sState = sock.readyState; } catch (e) {}
    }
    return {
      pageTitle: document.title,
      hasSocketUtil: !!su,
      hasSocket: !!sock,
      socketUrl: sUrl,
      socketState: sState,
      globalMallId: window.global_mall_id || null,
      globalUid: window.global_uid || null,
      csidGuess: csidGuess,
      wsAddr: (window.__pddConf || {}).wsAddr || null
    };
  };

  /* ---- 原型级钩子: 只装一次 ---- */
  if (!window.__pddBridge_proto) {
    window.__pddBridge_proto = 1;
    var desc = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage');
    var origAdd = WebSocket.prototype.addEventListener;
    WebSocket.prototype.addEventListener = function (type, fn, opts) {
      if (type === 'message' && typeof fn === 'function') {
        var wrapped = function (e) { pushIn(e); return fn.apply(this, arguments); };
        return origAdd.call(this, type, wrapped, opts);
      }
      return origAdd.apply(this, arguments);
    };
    Object.defineProperty(WebSocket.prototype, 'onmessage', {
      get: desc.get,
      set: function (fn) {
        var self = this;
        if (typeof fn === 'function') {
          return desc.set.call(self, function (e) { pushIn(e); return fn.apply(self, arguments); });
        }
        return desc.set.call(self, fn);
      },
      configurable: true
    });
  }

  /* ---- 直达现有 socket (web 模式: 真 WebSocket; 及探域桥的 __reverseWebSocket) ---- */
  var attached = 0;
  var candidates = [su && su.socket];
  if (window.__reverseWebSocket && window.__reverseWebSocket !== (su && su.socket)) {
    candidates.push(window.__reverseWebSocket);
  }
  var listeners = [];
  for (var ci = 0; ci < candidates.length; ci++) {
    var cand = candidates[ci];
    if (cand && typeof cand.addEventListener === 'function') {
      try {
        cand.addEventListener('message', pushIn);
        listeners.push([cand, pushIn]);
        attached = 1;
      } catch (e) { attached = -1; }
    }
  }
  /* 留给下一次注入摘钩子用（见顶部升级自愈） */
  window.__pddBridge_off = function () {
    for (var li = 0; li < listeners.length; li++) {
      try { listeners[li][0].removeEventListener('message', listeners[li][1]); } catch (e) {}
    }
    listeners = [];
  };

  /* ---- native 模式: pinnotification.message = native→JS 入站总入口 ---- */
  var pinAttached = 0;
  try {
    if (window.pinnotification && typeof window.pinnotification.message === 'function' && !window.__pddBridge_pin) {
      window.__pddBridge_pin = 1;
      var origMsg = window.pinnotification.message.bind(window.pinnotification);
      window.pinnotification.message = function (channel, payload) {
        try {
          if (typeof channel === 'string' && /socket|message|chat/i.test(channel)) {
            var data = (typeof payload === 'string') ? payload : JSON.stringify(payload);
            record({ t: Date.now(), dir: 'in', chan: channel, data: data });
            pinAttached = 1;
          }
        } catch (e) {}
        return origMsg(channel, payload);
      };
    }
  } catch (e) { pinAttached = -2; }

  /* ---- 包装 sendMsg 记录发出方向 ---- */
  if (su && typeof su.sendMsg === 'function' && !su.__pddBridge_wrapped) {
    su.__pddBridge_wrapped = 1;
    var origSendMsg = su.sendMsg.bind(su);
    su.sendMsg = function (opts) {
      var uid = opts && (opts.uid || opts.buyerId);
      var content = opts && opts.content;
      record({ t: Date.now(), dir: 'out', type: 'send', uid: uid, content: content });
      return origSendMsg(opts);
    };
  }

  window.__pddBridge_attached = (attached === 1 || pinAttached === 1) ? 1 : 0;
  window.__pddBridge_hooked = 1;
  return { ok: true, attached: window.__pddBridge_attached, hasSocketUtil: !!su, hasSocket: !!(su && su.socket) };
})();
