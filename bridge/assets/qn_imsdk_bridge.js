/**
 * Qianniu imsdk bridge (openbot-compatible protocol) v4.
 *
 * Receives IM SDK events in the Qianniu chat page and forwards normalized
 * messages to the local Agent at ws://127.0.0.1:41010. Event delivery is the
 * primary path; bounded multi-conversation polling is the safety net.
 */
(function () {
  if (window.__qn_bridge_v4_installed) {
    if (typeof window.__qn_bridge_reconnect === "function") window.__qn_bridge_reconnect();
    if (typeof window.__qn_bridge_poll === "function") window.__qn_bridge_poll();
    return;
  }
  window.__qn_bridge_v4_installed = true;
  window.__qn_bridge_v3_installed = true;
  window.__qn_bridge_v2_installed = true;

  var BRIDGE_VERSION = "qn-bridge-v4";
  var WS_URL = "ws://127.0.0.1:41010";
  var POLL_INTERVAL_MS = 1500;
  var MAX_POLL_TARGETS = 6;
  var MAX_KNOWN_CONVERSATIONS = 80;
  var MAX_SEEN = 1200;
  var socket = null;
  var reconnectTimer = null;
  var heartbeatTimer = null;
  var pollTimer = null;
  var domObserver = null;
  var pollRunning = false;
  var seenMsgIds = {};
  var seenOrder = [];
  var knownConversations = {};
  var conversationOrder = [];
  var urgentConversations = [];
  var pollCursor = 0;
  var lastSellerNick = "";
  var lastIdentityAt = 0;
  var lastDomScanAt = 0;
  var lastDiscoveryAt = 0;
  var discoveryCursor = 0;

  var diagnostics = {
    version: BRIDGE_VERSION,
    started_at_ms: Date.now(),
    websocket_connected: false,
    subscriptions: {},
    event_callbacks: 0,
    known_conversations: 0,
    last_event_at_ms: 0,
    last_capture_at_ms: 0,
    last_capture_mode: "",
    last_poll_at_ms: 0,
    last_poll_duration_ms: 0,
    sent_events: 0,
    dropped_while_disconnected: 0,
  };
  window.__qn_bridge_diag = diagnostics;

  function safeSend(obj) {
    try {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(obj));
        return true;
      }
    } catch (e) {
      console.error("[qn-bridge] send fail", e);
    }
    return false;
  }

  function startHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(function () {
      safeSend({
        type: "hi",
        response: BRIDGE_VERSION,
        diagnostics: {
          known_conversations: conversationOrder.length,
          event_callbacks: diagnostics.event_callbacks,
          last_capture_at_ms: diagnostics.last_capture_at_ms,
        },
      });
    }, 10000);
  }

  function wasSeen(id) {
    id = String(id || "");
    return !!(id && seenMsgIds[id]);
  }

  function rememberSeen(id) {
    id = String(id || "");
    if (!id || seenMsgIds[id]) return;
    seenMsgIds[id] = 1;
    seenOrder.push(id);
    if (seenOrder.length > MAX_SEEN) {
      var old = seenOrder.shift();
      delete seenMsgIds[old];
    }
  }

  function textOf(v) {
    if (v == null) return "";
    if (typeof v === "string") return v.replace(/\s+/g, " ").trim();
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    return "";
  }

  function nickOf(node) {
    if (!node) return "";
    if (typeof node === "string") return textOf(node);
    if (typeof node !== "object") return "";
    return textOf(node.nick || node.display || node.targetId || node.uid || node.userId || "");
  }

  function firstText() {
    for (var i = 0; i < arguments.length; i++) {
      var t = textOf(arguments[i]);
      if (t) return t;
    }
    return "";
  }

  function timestampSeconds(raw) {
    var value = Number(raw) || Date.now();
    // Qianniu fields vary between seconds, milliseconds, microseconds, and
    // nanoseconds. Normalize all of them before upload.
    if (value > 1e17) return Math.floor(value / 1e9);
    if (value > 1e14) return Math.floor(value / 1e6);
    if (value > 1e11) return Math.floor(value / 1e3);
    return Math.floor(value);
  }

  function conversationIdOf(node) {
    if (node == null) return "";
    if (typeof node === "string" || typeof node === "number") {
      var raw = textOf(node);
      if (!raw || raw.length > 300 || raw === "[object Object]") return "";
      if (raw.charAt(0) === "{" || raw.charAt(0) === "[") {
        try { return conversationIdOf(JSON.parse(raw)); } catch (e) { return ""; }
      }
      return raw;
    }
    if (typeof node !== "object") return "";
    return conversationIdOf(
      node.ccode || node.conversationId || node.conversationID ||
      node.conversation_id || node.conversationCode || node.cid || ""
    );
  }

  function queueUrgent(ccode) {
    if (!ccode || urgentConversations.indexOf(ccode) >= 0) return;
    urgentConversations.push(ccode);
    if (urgentConversations.length > MAX_KNOWN_CONVERSATIONS) urgentConversations.shift();
  }

  function registerConversation(raw, source, urgent) {
    var ccode = conversationIdOf(raw);
    if (!ccode) return "";
    var now = Date.now();
    var entry = knownConversations[ccode];
    if (!entry) {
      entry = knownConversations[ccode] = { first_seen_at_ms: now, last_seen_at_ms: now, source: source || "unknown" };
      conversationOrder.push(ccode);
      if (conversationOrder.length > MAX_KNOWN_CONVERSATIONS) {
        var removed = conversationOrder.shift();
        delete knownConversations[removed];
        var queuedIndex = urgentConversations.indexOf(removed);
        if (queuedIndex >= 0) urgentConversations.splice(queuedIndex, 1);
        if (pollCursor > 0) pollCursor -= 1;
      }
    } else {
      entry.last_seen_at_ms = now;
      if (source) entry.source = source;
    }
    if (urgent || !entry.polled_at_ms) queueUrgent(ccode);
    diagnostics.known_conversations = conversationOrder.length;
    return ccode;
  }

  function walkConversationIds(obj, source, depth) {
    depth = depth || 0;
    if (obj == null || depth > 7) return;
    if (Array.isArray(obj)) {
      for (var i = 0; i < Math.min(obj.length, 120); i++) walkConversationIds(obj[i], source, depth + 1);
      return;
    }
    if (typeof obj !== "object") return;
    Object.keys(obj).slice(0, 120).forEach(function (key) {
      var value = obj[key];
      var lower = String(key).toLowerCase();
      if (lower === "ccode" || lower === "conversationid" || lower === "conversation_id" || lower === "conversationcode") {
        registerConversation(value, source, true);
      } else if (lower === "cid") {
        registerConversation(value, source, true);
        walkConversationIds(value, source, depth + 1);
      } else if (value && typeof value === "object") {
        walkConversationIds(value, source, depth + 1);
      } else if (typeof value === "string" && (value.charAt(0) === "{" || value.charAt(0) === "[")) {
        try { walkConversationIds(JSON.parse(value), source, depth + 1); } catch (e) {}
      }
    });
  }

  function extractProduct(detail) {
    var buckets = [];
    function push(x) {
      if (x && typeof x === "object") buckets.push(x);
    }
    push(detail);
    var original = detail && detail.originalData;
    if (typeof original === "string") {
      try { original = JSON.parse(original); } catch (e) { original = null; }
    }
    push(original);
    if (original) {
      ["item", "itemInfo", "goods", "product", "card", "ext", "extra", "bizData", "content"].forEach(function (k) {
        push(original[k]);
      });
      if (Array.isArray(original.jsview)) {
        original.jsview.forEach(function (it) {
          push(it);
          if (it && typeof it === "object") push(it.value);
        });
      }
    }
    var goods = { goods_id: "", goods_name: "", goods_url: "", goods_thumb_url: "", goods_price: "", goods_spec: "" };
    var idKeys = ["goods_id", "product_id", "itemId", "item_id", "itemid", "num_iid", "numIid", "auctionId", "id"];
    var nameKeys = ["goods_name", "product_name", "itemTitle", "item_title", "title", "name", "auctionTitle"];
    var urlKeys = ["goods_url", "product_url", "itemUrl", "item_url", "url", "actionUrl", "pcUrl", "h5Url"];
    var thumbKeys = ["goods_thumb_url", "pic", "picUrl", "pictUrl", "img", "image", "mainPic", "imgUrl"];
    var priceKeys = ["goods_price", "price", "salePrice", "zkFinalPrice", "discountPrice"];
    var specKeys = ["goods_spec", "sku", "skuText", "skuName", "props", "spec"];
    buckets.forEach(function (b) {
      if (!goods.goods_id) {
        for (var i = 0; i < idKeys.length; i++) {
          var cand = b[idKeys[i]];
          if (cand != null && String(cand).match(/^\d{5,20}$/)) {
            if (idKeys[i] === "id" && !(b.title || b.itemTitle || b.pic || b.price || b.itemUrl)) continue;
            goods.goods_id = String(cand);
            break;
          }
        }
      }
      if (!goods.goods_name) goods.goods_name = firstText.apply(null, nameKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_url) goods.goods_url = firstText.apply(null, urlKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_thumb_url) goods.goods_thumb_url = firstText.apply(null, thumbKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_price) goods.goods_price = firstText.apply(null, priceKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_spec) goods.goods_spec = firstText.apply(null, specKeys.map(function (k) { return b[k]; }));
    });
    if (goods.goods_id && !goods.goods_url) goods.goods_url = "https://item.taobao.com/item.htm?id=" + goods.goods_id;
    var out = {};
    Object.keys(goods).forEach(function (k) { if (goods[k]) out[k] = goods[k]; });
    return out;
  }

  function extractText(detail) {
    if (!detail || typeof detail !== "object") return "";
    var t = firstText(detail.summary, detail.text, detail.content, detail.msg);
    if (t) return t;
    var original = detail.originalData;
    if (typeof original === "string") {
      try { original = JSON.parse(original); } catch (e) { original = null; }
    }
    if (original && typeof original === "object") {
      t = firstText(original.text, original.content, original.msg, original.summary);
      if (t) return t;
      if (Array.isArray(original.jsview)) {
        for (var i = 0; i < original.jsview.length; i++) {
          var it = original.jsview[i] || {};
          var val = it.value;
          if (val && typeof val === "object") t = firstText(val.text, val.content);
          else t = textOf(val);
          if (t) return t;
        }
      }
    }
    var product = extractProduct(detail);
    if (product.goods_name || product.goods_id) {
      var name = product.goods_name || product.goods_id;
      return product.goods_spec ? ("咨询商品：" + name + "（" + product.goods_spec + "）") : ("咨询商品：" + name);
    }
    return "";
  }

  function normalizeDetail(detail, sellerHint, captureMode) {
    if (!detail || typeof detail !== "object") return null;
    var content = extractText(detail);
    if (!content) return null;
    var fromid = detail.fromid || detail.fromId || {};
    var toid = detail.toid || detail.toId || {};
    var fromNick = nickOf(fromid);
    var toNick = nickOf(toid);
    var ccode = conversationIdOf(detail.cid || detail.ccode || "");
    if (ccode) registerConversation(ccode, captureMode, true);
    var seller = textOf(sellerHint || lastSellerNick || "");
    var role = "user";
    var buyer = "";
    if (seller && fromNick && fromNick === seller) {
      role = "mall_cs";
      buyer = toNick || ccode;
    } else if (seller && toNick && toNick === seller) {
      role = "user";
      buyer = fromNick || ccode;
    } else {
      buyer = fromNick || ccode || toNick;
      if (!seller) seller = toNick || fromNick;
    }
    if (!buyer) return null;
    var buyerId = ccode || buyer;
    var mcode = detail.mcode && typeof detail.mcode === "object" ? detail.mcode : {};
    var msgId = textOf(mcode.messageId || mcode.clientId || detail.messageId || detail.msg_id || detail.clientId || "");
    var tsRaw = detail.sendTime || detail.sortTimeMicrosecond || detail.ts || Date.now();
    var ts = timestampSeconds(tsRaw);
    if (!msgId) msgId = "tb-js-" + String(seller) + "|" + String(buyerId) + "|" + content.slice(0, 40) + "|" + ts;
    if (wasSeen(msgId)) return null;
    var row = {
      platform: "taobao",
      role: role,
      content: content,
      account: seller || lastSellerNick || "",
      buyer_id: buyerId,
      buyer_nick: role === "user" ? (fromNick || buyer) : (toNick || buyer),
      msg_id: msgId,
      ts: ts,
      source: "openbot_js_v4",
      capture_mode: captureMode || "unknown",
      captured_at_ms: Date.now(),
      raw_type: textOf(detail.templateId || detail.templateid || detail.msgType || detail.type || ""),
    };
    if (seller) lastSellerNick = seller;
    var product = extractProduct(detail);
    Object.keys(product).forEach(function (k) { row[k] = product[k]; });
    if (product.goods_id || product.goods_name) {
      row.order_info = Object.assign({ source: "bridge_product_context" }, product);
      if (!row.raw_type) row.raw_type = "taobao_product_card";
    }
    return row;
  }

  function walkDetails(obj, out, depth) {
    depth = depth || 0;
    if (!obj || depth > 8) return;
    if (Array.isArray(obj)) {
      for (var i = 0; i < Math.min(obj.length, 100); i++) walkDetails(obj[i], out, depth + 1);
      return;
    }
    if (typeof obj !== "object") return;
    var hasParty = obj.fromid || obj.toid || obj.fromId || obj.toId || obj.cid;
    var hasBody = obj.summary || obj.originalData || obj.mcode || obj.templateId || obj.text || obj.content;
    if (hasParty && hasBody) out.push(obj);
    ["data", "result", "msgDetail", "message", "originData", "msgs", "messages", "list", "items"].forEach(function (k) {
      var v = obj[k];
      if (typeof v === "string" && (v.charAt(0) === "{" || v.charAt(0) === "[")) {
        try { walkDetails(JSON.parse(v), out, depth + 1); } catch (e) {}
      } else {
        walkDetails(v, out, depth + 1);
      }
    });
  }

  function emitChatPayload(payload, sellerHint, captureMode) {
    var result = { details: 0, sent: 0 };
    try {
      walkConversationIds(payload, captureMode || "payload", 0);
      var details = [];
      walkDetails(payload, details, 0);
      if (!details.length && payload && typeof payload === "object") details = [payload];
      result.details = details.length;
      details.forEach(function (detail) {
        var row = normalizeDetail(detail, sellerHint, captureMode);
        if (!row) return;
        if (safeSend({ type: "chat_event", payload: row })) {
          rememberSeen(row.msg_id);
          result.sent += 1;
          diagnostics.sent_events += 1;
          diagnostics.last_capture_at_ms = Date.now();
          diagnostics.last_capture_mode = captureMode || "unknown";
        } else {
          diagnostics.dropped_while_disconnected += 1;
        }
      });
    } catch (e) {
      console.error("[qn-bridge] emit fail", e);
    }
    return result;
  }

  function eventHandler(eventName) {
    return function (data) {
      diagnostics.event_callbacks += 1;
      diagnostics.last_event_at_ms = Date.now();
      var mode = "event:" + eventName;
      try {
        walkConversationIds(data, mode, 0);
        emitChatPayload({ event: eventName, data: data }, lastSellerNick, mode);
        if (Array.isArray(data)) {
          data.forEach(function (message) { emitChatPayload(message, lastSellerNick, mode); });
        } else if (data) {
          emitChatPayload(data, lastSellerNick, mode);
        }
      } catch (e) {
        console.error("[qn-bridge] event handle fail", eventName, e);
      }
    };
  }

  var SDK_EVENTS = [
    "im.singlemsg.onReceiveNewMsg",
    "im.imbamsg.onReceiveNewMsg",
    "im.amptribemsg.onReceiveNewMsg",
    "im.singlemsg.onSendNewMsg",
    "im.singlemsg.onMessageUpdate",
  ];

  function subscribeSdkEvents() {
    if (!window.imsdk || typeof window.imsdk.on !== "function") return false;
    if (window.__qn_bridge_v4_events_hooked) return true;
    window.__qn_bridge_v4_events_hooked = true;
    var subscribed = 0;
    SDK_EVENTS.forEach(function (eventName) {
      try {
        window.imsdk.on(eventName, eventHandler(eventName));
        diagnostics.subscriptions[eventName] = "individual";
        subscribed += 1;
      } catch (e) {
        diagnostics.subscriptions[eventName] = "failed:" + String(e && e.message ? e.message : e).slice(0, 120);
      }
    });
    try {
      window.imsdk.on(SDK_EVENTS, eventHandler("compat-array"));
      diagnostics.subscriptions["compat-array"] = "registered";
    } catch (e) {
      diagnostics.subscriptions["compat-array"] = "failed:" + String(e && e.message ? e.message : e).slice(0, 120);
    }
    console.log("[qn-bridge] individual subscriptions", subscribed, SDK_EVENTS.length);
    return subscribed > 0;
  }

  function hookImSdk() {
    try {
      if (!window.imsdk) return false;
      if (!window.__qn_bridge_v4_invoke_hooked) {
        window.__qn_bridge_v4_invoke_hooked = true;
        var original = window.imsdk.invoke && window.imsdk.invoke.bind(window.imsdk);
        if (original) {
          window.imsdk.invoke = function (api, param) {
            var ret = original(api, param);
            try {
              var apiName = String(api || "");
              walkConversationIds(param, "invoke-param:" + apiName, 0);
              if (/msg|message|chat|peek|conv|receive|notify/i.test(apiName)) {
                Promise.resolve(ret).then(function (res) {
                  walkConversationIds(res, "invoke-result:" + apiName, 0);
                  emitChatPayload({ api: apiName, param: param, result: res }, lastSellerNick, "invoke:" + apiName);
                }).catch(function () {});
              }
            } catch (e) {}
            return ret;
          };
        }
      }
      subscribeSdkEvents();
      refreshIdentity(false);
      console.log("[qn-bridge] imsdk hooked", BRIDGE_VERSION);
      return true;
    } catch (e) {
      return false;
    }
  }

  async function invokeWithTimeout(api, param, timeoutMs) {
    var invokePromise;
    try {
      invokePromise = Promise.resolve(window.imsdk.invoke(api, param || {}));
    } catch (e) {
      throw e;
    }
    var timer;
    var timeout = new Promise(function (_resolve, reject) {
      timer = setTimeout(function () { reject(new Error("timeout:" + api)); }, timeoutMs || 1800);
    });
    try {
      return await Promise.race([invokePromise, timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function refreshIdentity(force) {
    if (!window.imsdk || typeof window.imsdk.invoke !== "function") return;
    if (!force && Date.now() - lastIdentityAt < 30000) return;
    lastIdentityAt = Date.now();
    try {
      var login = await invokeWithTimeout("im.login.GetCurrentLoginID", {}, 1800);
      var node = login && login.result ? login.result : login;
      if (node && (node.nick || node.display || node.userid)) lastSellerNick = String(node.nick || node.display || node.userid);
    } catch (e) {}
  }

  async function currentConversationId() {
    try {
      var conv = await invokeWithTimeout("im.uiutil.GetCurrentConversationID", {}, 1800);
      var node = conv && conv.result != null ? conv.result : conv;
      var ccode = conversationIdOf(node);
      return registerConversation(ccode, "current", true);
    } catch (e) {
      return "";
    }
  }

  function scanDomNode(root) {
    if (!root || root.nodeType !== 1) return;
    var attrs = ["data-ccode", "ccode", "data-conversation-id", "conversation-id", "data-conversationid", "conversationid"];
    function scanElement(element) {
      attrs.forEach(function (name) {
        var value = element.getAttribute && element.getAttribute(name);
        if (value) registerConversation(value, "dom:" + name, false);
      });
    }
    scanElement(root);
    try {
      var selector = attrs.map(function (name) { return "[" + name + "]"; }).join(",");
      var nodes = root.querySelectorAll ? root.querySelectorAll(selector) : [];
      for (var i = 0; i < Math.min(nodes.length, 300); i++) scanElement(nodes[i]);
    } catch (e) {}
  }

  function scanConversationDom(force) {
    if (!force && Date.now() - lastDomScanAt < 5000) return;
    lastDomScanAt = Date.now();
    scanDomNode(document.documentElement);
  }

  function startDomObserver() {
    scanConversationDom(true);
    if (domObserver || typeof MutationObserver !== "function" || !document.documentElement) return;
    domObserver = new MutationObserver(function (mutations) {
      mutations.slice(0, 60).forEach(function (mutation) {
        for (var i = 0; i < Math.min(mutation.addedNodes.length, 30); i++) scanDomNode(mutation.addedNodes[i]);
      });
    });
    domObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  var DISCOVERY_APIS = [
    "im.conversation.GetConversationList",
    "im.conversation.GetRecentConversationList",
    "im.uiutil.GetConversationList",
    "im.uiutil.GetRecentConversationList",
    "im.singlemsg.GetRecentSessionList",
    "im.singlemsg.GetRecentContactList",
  ];

  async function discoverRecentConversations() {
    if (Date.now() - lastDiscoveryAt < 4000) return;
    lastDiscoveryAt = Date.now();
    var api = DISCOVERY_APIS[discoveryCursor % DISCOVERY_APIS.length];
    discoveryCursor += 1;
    try {
      var res = await invokeWithTimeout(api, { count: 100 }, 1800);
      if (res && res.ok !== false) {
        var payload = res.result != null ? res.result : res;
        walkConversationIds(payload, "discovery:" + api, 0);
      }
    } catch (e) {}
  }

  async function pollConversation(ccode, current) {
    if (!ccode) return;
    var attempts = current ? [
      ["im.singlemsg.GetNewMsg", { ccode: ccode }],
      ["im.singlemsg.PeekNewMsg", { ccode: ccode }],
      ["im.singlemsg.GetLocalHisMsg", { cid: { ccode: ccode }, gohistory: 1, count: 30 }],
      ["im.singlemsg.GetRemoteHisMsg", { cid: { ccode: ccode }, count: 30 }],
    ] : [
      ["im.singlemsg.GetLocalHisMsg", { cid: { ccode: ccode }, gohistory: 1, count: 30 }],
      ["im.singlemsg.GetNewMsg", { ccode: ccode }],
      ["im.singlemsg.PeekNewMsg", { ccode: ccode }],
    ];
    var entry = knownConversations[ccode];
    if (entry) entry.polled_at_ms = Date.now();
    for (var i = 0; i < attempts.length; i++) {
      try {
        var api = attempts[i][0];
        var param = attempts[i][1];
        var res = await invokeWithTimeout(api, param, 1800);
        if (!res || res.ok === false) continue;
        var payload = res.result != null ? res.result : res;
        var emitted = emitChatPayload({ api: api, param: param, result: payload }, lastSellerNick, "poll:" + api);
        if (Array.isArray(payload)) {
          payload.forEach(function (message) { emitChatPayload(message, lastSellerNick, "poll:" + api); });
        }
        if (emitted.details > 0) break;
      } catch (e) {}
    }
  }

  function nextPollTargets(current) {
    var targets = [];
    function add(ccode) {
      if (ccode && targets.indexOf(ccode) < 0 && targets.length < MAX_POLL_TARGETS) targets.push(ccode);
    }
    add(current);
    while (urgentConversations.length && targets.length < MAX_POLL_TARGETS) add(urgentConversations.shift());
    var checked = 0;
    while (conversationOrder.length && targets.length < MAX_POLL_TARGETS && checked < conversationOrder.length) {
      if (pollCursor >= conversationOrder.length) pollCursor = 0;
      add(conversationOrder[pollCursor]);
      pollCursor += 1;
      checked += 1;
    }
    return targets;
  }

  async function pollRecentMessages() {
    if (pollRunning || !window.imsdk || typeof window.imsdk.invoke !== "function") return;
    pollRunning = true;
    var started = Date.now();
    try {
      await refreshIdentity(false);
      scanConversationDom(false);
      var pair = await Promise.all([currentConversationId(), discoverRecentConversations()]);
      var current = pair[0] || "";
      var targets = nextPollTargets(current);
      await Promise.all(targets.map(function (ccode) { return pollConversation(ccode, ccode === current); }));
      diagnostics.last_poll_at_ms = Date.now();
      diagnostics.last_poll_duration_ms = Date.now() - started;
    } finally {
      pollRunning = false;
    }
  }

  function startPoll() {
    if (pollTimer) clearInterval(pollTimer);
    startDomObserver();
    pollTimer = setInterval(pollRecentMessages, POLL_INTERVAL_MS);
    setTimeout(pollRecentMessages, 500);
  }

  async function runExpression(expression) {
    return await eval(expression);
  }

  function setup() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    try {
      socket = new WebSocket(WS_URL);
    } catch (e) {
      console.error("[qn-bridge] new WebSocket fail", e);
      scheduleReconnect();
      return;
    }
    socket.onopen = function () {
      console.log("[qn-bridge] connected", WS_URL, BRIDGE_VERSION);
      diagnostics.websocket_connected = true;
      window.chatWebsocket = socket;
      startHeartbeat();
      hookImSdk();
      startPoll();
      safeSend({ type: "hi", response: BRIDGE_VERSION, diagnostics: diagnostics });
    };
    socket.onmessage = async function (event) {
      var param;
      try { param = JSON.parse(event.data); } catch (e) { return; }
      if (!param || param.method !== "execute") return;
      try {
        var res = await runExpression(param.expression);
        safeSend({ type: "execute", response: JSON.stringify(res === undefined ? null : res) });
      } catch (err) {
        safeSend({
          type: "execute",
          response: JSON.stringify({ ok: false, err: String(err && err.message ? err.message : err) }),
        });
      }
    };
    socket.onclose = function () {
      diagnostics.websocket_connected = false;
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      if (window.chatWebsocket === socket) window.chatWebsocket = null;
      socket = null;
      scheduleReconnect();
    };
    socket.onerror = function () {
      try { socket.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(setup, 2000);
  }

  function boot() {
    hookImSdk();
    setup();
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (hookImSdk() || tries > 60) clearInterval(timer);
    }, 1000);
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(boot, 300);
  } else {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(boot, 300); });
  }

  window.__qn_bridge_reconnect = setup;
  window.__qn_bridge_poll = pollRecentMessages;
  window.__qn_bridge_add_conversation = function (ccode) { return registerConversation(ccode, "debug", true); };
})();
