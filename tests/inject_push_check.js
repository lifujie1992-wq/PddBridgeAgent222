/* 在 node 里加载真的 inject.js，验证推送/缓冲两条路径。
   跑法: node tests/inject_push_check.js （成功打印 OK） */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', 'bridge', 'pddbridge', 'inject.js'), 'utf8'
);

function load(fetchImpl) {
  const store = (function () {
    const s = {};
    return {
      getItem: (k) => (k in s ? s[k] : null),
      setItem: (k, v) => { s[k] = String(v); },
      removeItem: (k) => { delete s[k]; },
    };
  })();
  const win = { pinnotification: { message: function () {} },
                addEventListener: function (t) { win.__added.push(t); },
                localStorage: store };
  win.__added = [];
  win.__reloads = 0;
  function FakeWebSocket() {}
  FakeWebSocket.prototype = { addEventListener: function () {}, onmessage: null };
  const sandbox = {
    window: win,
    document: { title: 'check' },
    navigator: {},
    location: { reload: function () { win.__reloads++; } },
    console: console,
    fetch: fetchImpl,
    WebSocket: FakeWebSocket,
    Blob: function () {},
    ArrayBuffer: function () {},
    Promise: Promise,
    JSON: JSON, Date: Date, Object: Object, Array: Array, String: String,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    localStorage: store,
  };
  vm.createContext(sandbox);
  win.__sandbox = sandbox;
  win.__result = vm.runInContext(SRC, sandbox);
  return win;
}

/* 同一个窗口再跑一次注入脚本（模拟桥接重启/再次注入） */
function rerun(win) {
  win.__result = vm.runInContext(SRC, win.__sandbox);
  return win.__result;
}

function frame(i) {
  return '{"response":"push","message":{"from":{"role":"user","uid":"u' + i +
         '"},"msg_id":"m' + i + '","content":"hi' + i + '"}}';
}

function fail(msg) {
  console.log('FAIL ' + msg);
  process.exit(1);
}

const tick = () => new Promise((r) => setTimeout(r, 20));

(async function main() {
  /* 1. 推送成功: 帧不进缓冲, 批量合并成一个请求, ack 后 pushed 增加 */
  let posts = 0;
  const bodies = [];
  let win = load(function (url, opts) {
    posts++;
    try { bodies.push(JSON.parse(opts.body)); } catch (e) { bodies.push(null); }
    return Promise.resolve({ ok: true });
  });
  win.__pddBridge_push_url = 'http://127.0.0.1:1/push/tok';
  win.__pddBridge_session = '57165|1';
  win.pinnotification.message('socket_message', frame(1));
  win.pinnotification.message('socket_message', frame(2));
  if (win.__pddBridge_buf.length !== 0) fail('推送成功却进了缓冲: ' + win.__pddBridge_buf.length);
  if (posts !== 0) fail('应攒批而非立刻发, 实际已发 ' + posts);
  win.__pddBridge_flush_push();                       /* 批量推送: 显式触发 flush */
  if (posts !== 1) fail('2 帧应合并为 1 个请求, 实际 ' + posts);
  if (!Array.isArray(bodies[0]) || bodies[0].length !== 2) {
    fail('请求体应为 2 条数组: ' + JSON.stringify(bodies[0]).slice(0, 100));
  }
  await tick();
  let st = win.__pddBridge_stats();
  if (st.pushed !== 2) fail('推送成功后 pushed 应为 2, 实际 ' + st.pushed);
  if (st.recorded !== 2 || st.hooked !== 1) fail('stats 字段不对: ' + JSON.stringify(st));
  console.log('PASS 1 批量推送成功: 2 帧合并为 1 请求 buf=0 pushed=' + st.pushed);

  /* 1b. 攒满 PUSH_BATCH_MAX 立即发, 不需要等定时器 */
  let postsB = 0;
  const winB = load(function () { postsB++; return Promise.resolve({ ok: true }); });
  winB.__pddBridge_push_url = 'http://127.0.0.1:1/push/tok';
  winB.__pddBridge_session = '57165|1';
  for (let i = 0; i < 200; i++) winB.pinnotification.message('socket_message', frame(2000 + i));
  if (postsB !== 1) fail('攒满 200 帧应立即发 1 次, 实际 ' + postsB);
  console.log('PASS 1b 攒满一批即时 flush: 200 帧 -> 1 请求');

  /* 2. 推送失败: 帧补进缓冲, 由 drain 兜底 */
  win = load(function () { return Promise.reject(new Error('down')); });
  win.__pddBridge_push_url = 'http://127.0.0.1:1/push/tok';
  win.__pddBridge_session = '57165|1';
  win.pinnotification.message('socket_message', frame(3));
  win.__pddBridge_flush_push();
  await tick();
  st = win.__pddBridge_stats();
  if (win.__pddBridge_buf.length !== 1) fail('推送失败没补缓冲: ' + win.__pddBridge_buf.length);
  if (st.push_retry !== 1) fail('push_retry 应为 1, 实际 ' + st.push_retry);
  if (st.push_state !== 'fail') fail('push_state 应为 fail: ' + st.push_state);
  // 之后的帧直接走缓冲, drain 能拿到
  win.pinnotification.message('socket_message', frame(4));
  st = win.__pddBridge_stats();
  if (win.__pddBridge_buf.length !== 2) fail('降级后没走缓冲');
  if (st.push_skip !== 1) fail('push_skip 应为 1, 实际 ' + st.push_skip);
  const drained = win.__pddBridge_drain();
  if (drained.length !== 2) fail('drain 拿到的帧数不对: ' + drained.length);
  if (win.__pddBridge_stats().drained !== 2) fail('drained 计数不对');
  console.log('PASS 2 推送失败路径: 补缓冲 ' + drained.length + ' 条, retry=' + st.push_retry);

  /* 3. 连续同长度消息一条都不能丢（旧实现按「长度+前120字符」判重会整批误吞） */
  win.__pddBridge_push_url = '';
  const sameLen = [];
  for (let i = 1; i <= 6; i++) {
    sameLen.push('{"response":"push","message":{"from":{"role":"user","uid":"4764375385604",'
      + '"csid":"cs_12345:678"},"to":{"role":"mall_cs"},"msg_id":"17896333168' + i
      + '","content":"消息' + i + '","type":0,"ts":1756000000000}}');
  }
  if (new Set(sameLen.map((f) => f.length)).size !== 1) fail('测试帧长度不一致');
  if (sameLen[0].indexOf('msg_id') < 120) fail('测试帧的 msg_id 没落在 120 之外, 复现不了旧 bug');
  const n0 = win.__pddBridge_buf.length;
  sameLen.forEach((f) => win.pinnotification.message('socket_message', f));
  const kept = win.__pddBridge_buf.length - n0;
  if (kept !== 6) fail('连续同长度消息丢了: 只留下 ' + kept + '/6');
  /* 旧逻辑对照: 同样的 6 条, 旧 key 只会留下 1 条 */
  const oldArr = [];
  let oldKept = 0;
  sameLen.forEach((f) => {
    const key = f.length + ':' + f.slice(0, 120);
    if (oldArr.indexOf(key) >= 0) return;
    oldArr.push(key);
    if (oldArr.length > 30) oldArr.shift();
    oldKept++;
  });
  if (oldKept !== 1) fail('旧逻辑对照不符合预期: ' + oldKept);
  console.log('PASS 3 连续同长消息 ' + kept + '/6 全留下（旧逻辑只留 ' + oldKept + '/6）');

  /* 3b. 判重表守卫: 旧包装即使命中也不丢, 只计数 */
  const guard = win.__pddBridge_dedup;
  if (!guard || !guard.__pddBridgeGuard) fail('判重表没装 guard');
  guard.push('k1');
  if (guard.indexOf('k1') !== -1) fail('guard 仍在判重');
  if (guard._blocked !== 1) fail('guard 未统计拦下的条数: ' + guard._blocked);
  const dupBefore = win.__pddBridge_stats().exact_repeat;
  win.pinnotification.message('socket_message', sameLen[0]);
  if (win.__pddBridge_stats().exact_repeat !== dupBefore + 1) fail('重复帧未计入 exact_repeat');
  if (win.__pddBridge_buf.length - n0 !== 7) fail('重复帧被丢掉了（应只计数不丢）');
  console.log('PASS 3b 守卫拦下旧判重, 重复帧只计不丢');

  /* 4. 没有推送通道时溢出必须计数（上限由脚本自己暴露的 buf_max 决定） */
  const big = load(function () { return Promise.resolve({ ok: true }); });
  const cap = big.__pddBridge_stats().buf_max;
  if (!(cap > 1000)) fail('buf_max 不合理: ' + cap);
  const extra = 200;
  for (let i = 0; i < cap + extra; i++) big.pinnotification.message('socket_message', frame(100000 + i));
  const bst = big.__pddBridge_stats();
  if (big.__pddBridge_buf.length !== cap) fail('缓冲上限失效: ' + big.__pddBridge_buf.length + ' != ' + cap);
  if (bst.overflow_drop !== extra) fail('overflow_drop 应为 ' + extra + ', 实际 ' + bst.overflow_drop);
  console.log('PASS 4 无推送时溢出计数: cap=' + cap + ' dropped=' + bst.overflow_drop);

  /* 5. 无损取帧 peek/ack: peek 不删, ack 才删 —— 传输失败时数据必须还在 */
  const loss = load(function () { return Promise.resolve({ ok: true }); });
  loss.__pddBridge_push_url = '';                     /* 强制走缓冲 */
  for (let i = 0; i < 5; i++) loss.pinnotification.message('socket_message', frame(3000 + i));
  if (loss.__pddBridge_buf.length !== 5) fail('缓冲应有 5 帧');
  const peeked1 = loss.__pddBridge_peek(2);
  const peeked2 = loss.__pddBridge_peek(2);
  if (peeked1.length !== 2) fail('peek(2) 应返回 2 帧');
  if (loss.__pddBridge_buf.length !== 5) fail('peek 不能删数据, 实际剩 ' + loss.__pddBridge_buf.length);
  if (JSON.stringify(peeked1) !== JSON.stringify(peeked2)) fail('两次 peek 应拿到同一批');
  if (loss.__pddBridge_pending() !== 5) fail('pending 应为 5');
  loss.__pddBridge_ack(2);                            /* 模拟 Python 处理成功 */
  if (loss.__pddBridge_buf.length !== 3) fail('ack(2) 后应剩 3, 实际 ' + loss.__pddBridge_buf.length);
  if (loss.__pddBridge_stats().drained !== 2) fail('ack 后 drained 计数不对');
  const rest = loss.__pddBridge_drain();              /* 老接口仍可用 */
  if (rest.length !== 3) fail('drain 应拿到剩余 3 帧');
  console.log('PASS 5 peek 不删 / ack 才删 / pending 正确 （传输失败不丢数据）');


  /* 6. 历史快拉助手: 页大小可自定, 无接口时优雅降级 */
  const hist = load(function () { return Promise.resolve({ ok: true }); });
  let seen = null;
  hist.socketUtil = { getChatRecord: function () { seen = Array.prototype.slice.call(arguments); } };
  const hr1 = hist.__pddBridge_pullHistory('4764375385604', 100, 0, 0, 0);
  if (!hr1.ok || hr1.size !== 100) fail('历史快拉返回不对: ' + JSON.stringify(hr1));
  if (!seen || seen[0] !== '4764375385604' || seen[4] !== 100) {
    fail('getChatRecord 参数不对: ' + JSON.stringify(seen));
  }
  const hr2 = hist.__pddBridge_pullHistory('1');            /* 不传 size -> 默认 100 */
  if (hr2.size !== 100 || seen[4] !== 100) fail('默认页大小应为 100');
  const hr3 = hist.__pddBridge_pullHistory('1', -5);        /* 非法值也走默认 */
  if (hr3.size !== 100) fail('非法 size 应回落默认值');
  hist.socketUtil = {};                                     /* 页面没有该接口 */
  const hr4 = hist.__pddBridge_pullHistory('1', 100);
  if (hr4.ok !== false) fail('无 getChatRecord 时应返回 ok:false');
  console.log('PASS 6 历史快拉助手: 页大小自定/默认 100/无接口优雅降级');


  /* 7. localStorage 兕底：溢出帧先落盘，重放时带 replayed 标记 */
  const spillWin = load(function () { return Promise.resolve({ ok: true }); });
  spillWin.__pddBridge_push_url = '';                    /* 强制走缓冲 */
  const capS = spillWin.__pddBridge_stats().buf_max;
  for (let i = 0; i < capS + 30; i++) spillWin.pinnotification.message('socket_message', frame(5000 + i));
  const spilled = JSON.parse(spillWin.localStorage.getItem('__pddBridge_spill'));
  if (!Array.isArray(spilled) || spilled.length !== 30) fail('溢出 30 帧应全部落盘, 实际 ' + (spilled ? spilled.length : '无'));
  if (spillWin.__pddBridge_stats().spilled !== 30) fail('spilled 计数不对');
  /* 重放: 新加载一份注入, 预置同一份 localStorage */
  const replayWin = load(function () { return Promise.resolve({ ok: true }); });
  replayWin.localStorage.setItem('__pddBridge_spill', JSON.stringify(spilled));
  for (let i = 0; i < 5; i++) replayWin.pinnotification.message('socket_message', frame(6000 + i));
  replayWin.__pddBridge_spill_replay();               /* 手动触发重放（注入时存储还是空的） */
  const replayN = replayWin.__pddBridge_stats().spill_replayed;
  if (replayN !== 30) fail('重放条数应为 30, 实际 ' + replayN);
  const repBuf = replayWin.__pddBridge_buf;
  if (repBuf.length !== 35) fail('重放+新帧应共 35, 实际 ' + repBuf.length);
  const replayedFlags = repBuf.filter((e) => e.replayed === 1).length;
  if (replayedFlags !== 30) fail('重放帧应带 replayed=1, 实际 ' + replayedFlags);
  /* 再次加载: localStorage 已清空, 不重复重放 */
  const again = load(function () { return Promise.resolve({ ok: true }); });
  if (again.__pddBridge_stats().spill_replayed !== 0) fail('重放后应清除, 不该重复重放');
  console.log('PASS 7 溢出落盘 30 帧 + 重放 30 帧(replayed 标记) + 不重复重放');

  /* 7b. 页面刷新前: 缓冲未推送的帧写 localStorage */
  const unWin = load(function () { return Promise.reject(new Error('down')); });
  unWin.__pddBridge_push_url = 'http://127.0.0.1:1/push/tok';
  unWin.__pddBridge_session = '57165|1';
  unWin.pinnotification.message('socket_message', frame(7001));
  unWin.__pddBridge_flush_push();                        /* 推送失败 -> 补进缓冲 */
  await tick();                                          /* 失败回调是异步微任务 */
  if (unWin.__pddBridge_buf.length !== 1) fail('前置: 失败帧应在缓冲');
  unWin.__pddBridge_spill_flush();                       /* 模拟 beforeunload */
  const unspilled = JSON.parse(unWin.localStorage.getItem('__pddBridge_spill'));
  if (!Array.isArray(unspilled) || unspilled.length !== 1) fail('unload 兑底没落盘');
  console.log('PASS 7b 刷新前兑底: 未推送帧已落盘');

  /* 8. 升级自愈: 页面上还挂着旧脚本（没有 __pddBridge_ver）-> 整页刷新，不叠加监听 */
  const up = load(function () { return Promise.resolve({ ok: true }); });
  up.__reloads = 0;
  up.__added.length = 0;
  up.__pddBridge_hooked = 1;                   /* 旧脚本留下的钩子 */
  delete up.__pddBridge_ver;
  const upRes = rerun(up);
  if (up.__reloads !== 1) fail('旧版本应触发整页刷新, 实际 ' + up.__reloads);
  if (up.__added.length !== 0) fail('刷新前不该再挂监听/事件: ' + up.__added.join(','));
  if (!upRes || upRes.reloading !== true) fail('应返回 reloading 让 Python 不当成注入失败');
  console.log('PASS 8 升级自愈: 旧版本注入 -> 触发刷新且不叠加监听');

  /* 8b. 同版本重复注入（桥接重启）: 不刷新，改为摘掉上一条监听 */
  const same = load(function () { return Promise.resolve({ ok: true }); });
  if (typeof same.__pddBridge_ver !== 'number') fail('首次注入应记下脚本版本');
  same.__reloads = 0;
  same.__added.length = 0;
  same.__pddBridge_hooked = 1;
  let offCalls = 0;
  const prevOff = same.__pddBridge_off;
  same.__pddBridge_off = function () { offCalls++; return prevOff(); };
  rerun(same);
  if (same.__reloads !== 0) fail('同版本不该刷新页面');
  if (offCalls !== 1) fail('同版本应摘掉上一条监听, 实际 ' + offCalls);
  if (same.__added.indexOf('beforeunload') < 0) fail('同版本应继续完成注入');
  console.log('PASS 8b 同版本重复注入: 不刷新, 摘掉旧监听');

  console.log('OK');
})().catch(function (e) { fail('异常: ' + (e && e.message)); });
