// 快照列表渲染回归：真调 listSnapshots()，验证内嵌渲染 + 备注编辑 +
// 上锁开关 + 受保护删除的二次确认。
//
// 为什么单独一个文件而不并进 check-render.js
// ------------------------------------------
// check-render.js 跑的是**全部 47 个视图**，它只检查「不抛异常、
// 不空渲染」。而这一版快照的坑全在细节里：
//   · 备注输入框在不在、值对不对
//   · 上锁开关的 checked 有没有正确反映后端的 protected
//   · 点「存备注」发的请求体对不对
//   · 删受保护快照时有没有真走 needs_force → 二次确认 → 带 force 这条链
// 这些 check-render 全都看不见 —— 它只数「有没有渲染出东西」。
//
// 三条踩过的坑，写在这里免得再犯
// ------------------------------
// 1. **桩没接上**：第一版用 ctx.listSnapshots() 调，fetch 桩一次都没
//    被调用，15 条断言全拿到「暂无快照」。症状是「数据为空」而不是
//    报错，根本猜不出是桩的问题。所以下面第一条断言就是「fetch 桩
//    被调用过」—— 这种静默失效比判据红掉难查得多。
// 2. **必须 vm.runInContext**：app.js 里的 fetch 靠作用域链查到我
//    注入的桩，从外部 ctx.listSnapshots() 调走的是另一条路径。
// 3. **用例必须串行**：第一版四个 IIFE 并发跑，全都往同一个
//    SNAP_STATE 上写 —— 竞态。每个用例的 await 都会让出控制权，
//    后一个用例已经把 SNAP_STATE 换掉了。改成一条 await 链。
//
// 用法：node devtools/check-snap-render.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DEFAULT_APP = path.join(__dirname, '..', 'web', 'app.js');
const code = fs.readFileSync(process.argv[2] || DEFAULT_APP, 'utf8');

let pass = 0, fail = 0;
const bad = [];
function chk(desc, cond, extra) {
  if (cond) { pass++; console.log('[OK] ' + desc); }
  else {
    fail++; bad.push(desc);
    console.log('[NG] ' + desc);
    if (extra !== undefined) console.log('     ' + String(extra).slice(0, 300));
  }
}

// ---------------------------------------------------------------- DOM 桩
function mkEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, checked: false,
    className: '', tagName: 'DIV', dataset: {}, style: {}, children: [],
    attributes: {},
    classList: {
      add(c) { el.className += ' ' + c; },
      remove(c) { el.className = el.className.replace(c, ''); },
      toggle() { return false; }, contains() { return false; },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { el.children.push(c); },
    removeChild() {},
    querySelector() { return mkEl('sub'); },
    querySelectorAll() { return []; },
    get parentElement() { return mkEl('parent'); },
    get firstChild() { return mkEl('first'); },
    insertBefore() {}, replaceChild() {}, cloneNode() { return mkEl('clone'); },
    focus() {}, select() {}, scrollIntoView() {}, click() {},
    onclick: null, onchange: null, oninput: null, onkeydown: null,
    getContext() { return {}; },
    closest() { return null; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    setAttribute(k, v) { el.attributes[k] = v; },
    getAttribute(k) { return el.attributes[k]; },
    removeAttribute(k) { delete el.attributes[k]; },
  };
  return el;
}

const ELS = new Map();
function getEl(sel) {
  const k = String(sel).replace(/^#/, '').split(/[\s>]/)[0];
  if (!ELS.has(k)) ELS.set(k, mkEl(k));
  return ELS.get(k);
}

const document = {
  getElementById: id => getEl(id),
  querySelector: sel => getEl(sel),
  querySelectorAll: () => [],
  createElement: t => mkEl('new:' + t),
  body: mkEl('body'),
  addEventListener() {},
  documentElement: mkEl('html'),
  head: mkEl('head'),
};

// ------------------------------------------------------------ fetch 桩
const REQS = [];
let FETCH_HITS = 0;
let SNAP_STATE = { items: [], root: '/opt/drouter/snapshots', scope: [], disk: null };
let DELETE_NEEDS_FORCE = true;      // 模拟「后端对受保护快照要 force」
let PROTECT_FAILS = false;          // 模拟「后端拒绝上锁」

function mkSnap(ts, tag, protectedFlag, extra) {
  return Object.assign({
    ts, tag, protected: !!protectedFlag, size_kb: 128.0, db_included: true,
    created_at: ts, files: ['a'], count: 1,
  }, extra || {});
}

// ⚠️ 桩必须返回 **Response 形状**（带 .json()），不能直接返回对象。
// app.js 的 api() 走的是 `await res.json()`，我第一版返回裸对象
// `{ok, data}` —— 于是 .json is not a function 抛进 catch，api() 落到
// PARSE 分支返回 `{ok:false}`，listSnapshots 拿到的 items 是 undefined。
// 症状又是「数据为空」而不是报错：19 条断言里 17 条红，根因却是
// 桩的返回类型不对。跟第 1 条「桩没接上」是同一类病 ——
// **桩的形状不对时，症状永远是「数据为空」**。
function resp(obj, status) {
  return {
    ok: obj && obj.ok !== false,
    status: status || 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
    blob: async () => ({}),
  };
}

async function fetchStub(url, opts) {
  FETCH_HITS++;
  const method = (opts && opts.method) || 'GET';
  let body = null;
  if (opts && opts.body) {
    try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; }
  }
  REQS.push({ url, method, body });
  if (/\/api\/snapshots?(\?|$)/.test(url) && method === 'GET') {
    return resp({ ok: true, data: JSON.parse(JSON.stringify(SNAP_STATE)) });
  }
  if (/\/api\/snapshot\/note/.test(url)) {
    return resp({ ok: true, data: { ts: body && body.ts, tag: body && body.tag },
      msg_cn: (body && body.tag) ? '已保存快照备注' : '已清空快照备注' });
  }
  if (/\/api\/snapshot\/protect/.test(url)) {
    if (PROTECT_FAILS) return resp({ ok: false, msg_cn: '快照不存在', data: {} });
    return resp({ ok: true, data: { ts: body && body.ts, protected: !!(body && body.protected) },
      msg_cn: (body && body.protected) ? '已上锁' : '已解除' });
  }
  if (/\/api\/snapshot\/delete/.test(url)) {
    if (body && body.force) {
      return resp({ ok: true, data: { ts: body.ts }, msg_cn: '已删除快照' });
    }
    if (DELETE_NEEDS_FORCE) {
      return resp({ ok: false,
        msg_cn: '这份快照已上锁，自动清理不会删除它。若确实要删除，请确认后重试。',
        data: { needs_force: true, ts: body && body.ts, protected: true } });
    }
    return resp({ ok: true, data: { ts: body && body.ts }, msg_cn: '已删除快照' });
  }
  return resp({ ok: true, data: {}, msg_cn: 'ok' });
}

// ---------------------------------------------------------------- 沙箱
// ---- 取 i18n.js 的 DICT（只取纯数据，不执行它的 IIFE）----
const SNAP_I18N_DICT = (() => {
  const fs = require('fs');
  const p = require('path').join(__dirname, '..', 'web', 'i18n.js');
  const src = fs.readFileSync(p, 'utf-8');
  const sb = { window: {}, console,
    document: { documentElement: {}, readyState: 'complete',
      getElementById: () => null, querySelectorAll: () => [],
      addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { language: 'zh-CN' },
    setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {} };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(src, sb, { filename: p });
  return (sb.window.i18n && sb.window.i18n.dict) || {};
})();

const ctx = {
  document, fetch: fetchStub, console,
  setTimeout: () => 0, clearTimeout() {},
  setInterval: () => 0, clearInterval() {},
  location: { hash: '', href: '' },
  history: { replaceState() {} },
  navigator: { userAgent: 'node' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  Blob: function () {},
  alert() {}, confirm: () => true, prompt: () => '',
  EventSource: function () { return { close() {} }; },
  WebSocket: function () { return { close() {}, send() {} }; },
  FormData: function () { return {}; },
  crypto: { randomUUID: () => 'uuid' },
  CSS: { escape: s => String(s) },
  // 1.0.10 起 app.js 里的文案走 t()（i18n.js 提供）。本检查器**不加载**
  // i18n.js（它要 DOM + localStorage），所以给一个恒等兜底，
  // 保证渲染流程能跑完、不会因「t 未定义」而误报成快照功能坏了。
  // ⚠️ 必须是兜底而不是加载真 i18n.js —— 后者会让本检查器依赖字典，
  // 而字典天天在改，改一个词就可能连带让它失败，**掩盖真正的渲染问题**。
  // 1.0.10 起 app.js 的文案走 i18n 的 t()（i18n.js 提供）。
  //
  // ⚠️ 本检查器**与其它 check-* 相反**：它专门验「弹窗文案是不是
  //    『仍要删除』而不是『确定』」，所以**必须拿到真实中文** ——
  //    恒等兜底（t: k => k）会让所有文案断言拿到 key 原文而全红。
  //
  // ✅ 做法：用 vm 跑 i18n.js 只取 DICT（纯数据），再造查 DICT 的 t()。
  //    不执行它的 IIFE（那要 DOM + localStorage），只读数据 ——
  //    字典改了这里自动跟着变，不会「因为改了个词就打不开检查器」。
  t: (key, vars) => {
    const parts = String(key).split('.');
    let node = SNAP_I18N_DICT;
    for (const p of parts) {
      if (node == null || typeof node !== 'object') return key;
      node = node[p];
    }
    if (node == null) return key;
    // 词条形状是 {zh, en}
    let s = (node && typeof node === 'object' && 'zh' in node) ? node.zh : node;
    if (s == null) return key;
    if (Array.isArray(vars)) {
      vars.forEach((v, ix) => {
        s = String(s).split('{' + ix + '}').join(v == null ? '' : String(v));
      });
    }
    return s;
  },
  i18n: {
    t: (k) => k, setLang() {}, getLang: () => 'zh-CN', toggle() {},
    onChange() {}, applyDom() {}, dict: SNAP_I18N_DICT,
    register() {}, rerenderAll() {},
  },
  __TOASTS: [],
};
ctx.window = ctx;
ctx.globalThis = ctx;
ctx.self = ctx;

vm.createContext(ctx);
try {
  vm.runInContext(code, ctx, { filename: 'app.js' });
} catch (e) {
  console.error('加载 app.js 失败：' + e.message);
  process.exit(1);
}

const run = expr => vm.runInContext(expr, ctx);
const callListSnapshots = () => run('listSnapshots()');
const snapHtml = () => getEl('#sy-snapout').innerHTML;
const modals = () => run('globalThis.__modals');

// 捕获 modal / toast
vm.runInContext(
  'globalThis.__modals = [];' +
  'var __origModal = modal; modal = function(t,h,cb,txt){' +
  '  globalThis.__modals.push({t:t, h:String(h), txt:txt, cb:cb}); };' +
  'globalThis.__origToast = toast; toast = function(m,k,d){' +
  '  globalThis.__TOASTS.push({m:String(m), k:String(k)});' +
  '  return __origToast(m,k,d); };', ctx);

// 用一批假元素接管 querySelector/querySelectorAll，
// 好在渲染完成后手动触发 listSnapshots 绑上的 handler。
function withFakes(fakeMap, inputEl, fn) {
  const oQsa = document.querySelectorAll, oQs = document.querySelector;
  document.querySelectorAll = sel => {
    for (const key of Object.keys(fakeMap)) {
      if (sel.indexOf(key) >= 0) return fakeMap[key];
    }
    return [];
  };
  document.querySelector = sel => {
    if (inputEl && /\[data-tag=/.test(String(sel))) return inputEl;
    return oQs(sel);
  };
  return Promise.resolve()
    .then(fn)
    .finally(() => { document.querySelectorAll = oQsa; document.querySelector = oQs; });
}

// ---------------------------------------------------------------- 主体
async function main() {
  console.log('--- 前置 ---');
  chk('app.js 里 listSnapshots 是顶层函数声明',
    run('typeof listSnapshots') === 'function');
  chk('app.js 里存在 $$ 辅助函数（事件绑定依赖它）',
    run('typeof $$') === 'function', 'typeof $$ = ' + run('typeof $$'));

  // ================================================================
  console.log('--- 用例 1：内嵌渲染 + 备注框 + 上锁开关 ---');
  SNAP_STATE = {
    items: [
      mkSnap('20260901-101010', '切换双栈前', true),
      mkSnap('20260902-202020', 'auto-interval', false),
      mkSnap('20260903-303030', '', false),
    ],
    root: '/opt/drouter/snapshots',
    scope: [{ group: '网络', items: ['/etc/drouter/wan.conf'] }],
    disk: { total_mb: 8192, used_mb: 2048, free_mb: 6144 },
  };
  getEl('#sy-snapout')._html = '';
  REQS.length = 0;
  const before1 = FETCH_HITS;
  await callListSnapshots();
  const h1 = snapHtml();

  chk('fetch 桩真的被调用了（否则下面那些「空」全是假的）',
    FETCH_HITS > before1, 'FETCH_HITS 没涨');
  chk('列表写进了 #sy-snapout（内嵌，不是弹窗）', h1.length > 0);
  chk('确实向 /api/snapshots 要了数据',
    REQS.some(r => /\/api\/snapshots/.test(r.url) && r.method === 'GET'),
    JSON.stringify(REQS));
  // ⛔ 数<tr> 时必须排除表头那一行，否则「3 份快照」会数出 4
  // （thead 里也有一个 <tr>）。第一版就是这么写的，注释还特意
  // 标注了「不含表头」而正则根本没排除 —— 注释与判据不一致，
  // 是最容易让人怀疑产品有 bug 的一种错。
  chk('渲染出 3 行数据行（不含表头）',
    ((h1.match(/<tr>/g) || []).length - 1) === 3,
    '含表头共 ' + (h1.match(/<tr>/g) || []).length);
  chk('备注是可编辑输入框（data-tag × 3）',
    (h1.match(/data-tag=/g) || []).length === 3, (h1.match(/data-tag=/g) || []).length);
  chk('输入框里带上了后端返回的备注值', h1.includes('切换双栈前'));
  chk('空备注渲染成占位符而不是留白', h1.includes('（未命名）'));
  chk('上锁开关 3 个（data-lock × 3）',
    (h1.match(/data-lock=/g) || []).length === 3, (h1.match(/data-lock=/g) || []).length);
  const lock1 = (h1.match(/<input[^>]*data-lock="20260901-101010"[^>]*>/) || [''])[0];
  chk('受保护那份的开关带 checked', /\bchecked\b/.test(lock1), lock1);
  const lock2 = (h1.match(/<input[^>]*data-lock="20260902-202020"[^>]*>/) || [''])[0];
  chk('未上锁的开关没有 checked', !/\bchecked\b/.test(lock2), lock2);
  chk('表头有「保护」这一列', />保护</.test(h1));
  chk('统计行说明了已上锁份数', /份已上锁/.test(h1), h1.slice(0, 200));
  chk('写出了存放路径', h1.includes('/opt/drouter/snapshots'));
  chk('磁盘信息渲染出来', /剩余/.test(h1));
  chk('下载/回滚/删除按钮各 3 个',
    (h1.match(/data-dl=/g) || []).length === 3 &&
    (h1.match(/data-rb=/g) || []).length === 3 &&
    (h1.match(/data-del=/g) || []).length === 3);
  chk('「存备注」按钮 3 个', (h1.match(/data-save-tag=/g) || []).length === 3);
  // 覆盖范围说明渲染进的是**另一个容器** #sy-scope-box（它属于
  // 「配置快照与回滚」卡片，不在救援通道里）。第一版在 snapHtml()
  // 里找它当然找不到 —— 判据找错了容器，不是产品没渲染。
  chk('覆盖范围说明渲染进了 #sy-scope-box',
    /快照包含以下内容/.test(getEl('#sy-scope-box').innerHTML),
    getEl('#sy-scope-box').innerHTML.slice(0, 120));
  chk('没有回退到 modal 分支', !/modal\(/i.test(h1));

  // ================================================================
  console.log('--- 用例 2：空列表 ---');
  SNAP_STATE = { items: [], root: '/opt/drouter/snapshots', scope: [], disk: null };
  getEl('#sy-snapout')._html = '';
  await callListSnapshots();
  chk('空列表给出可操作提示（不是空白）',
    /暂无快照/.test(snapHtml()) && /创建/.test(snapHtml()), snapHtml());

  // ================================================================
  console.log('--- 用例 3：XSS ---');
  SNAP_STATE = {
    items: [mkSnap('20260904-404040', '<img src=x onerror=alert(1)>', false)],
    root: '/x', scope: [], disk: null,
  };
  getEl('#sy-snapout')._html = '';
  await callListSnapshots();
  const h3 = snapHtml();
  chk('恶意备注被转义（没有裸 <img）',
    !h3.includes('<img src=x') && /&lt;img/.test(h3), h3.slice(0, 400));
  chk('转义后仍能看出原文（&lt;img）', /&lt;img/.test(h3));

  // ================================================================
  console.log('--- 用例 4：存备注与上锁的请求体 ---');
  SNAP_STATE = { items: [mkSnap('20260905-505050', 'old', false)],
    root: '/x', scope: [], disk: null };
  const saveBtn = mkEl('save'); saveBtn.dataset.saveTag = '20260905-505050';
  const lockBox = mkEl('lock'); lockBox.dataset.lock = '20260905-505050';
  const tagInput = mkEl('tagin'); tagInput.value = '新备注';

  await withFakes({
    'data-save-tag': [saveBtn], 'data-lock': [lockBox],
  }, tagInput, async () => {
    REQS.length = 0;
    await callListSnapshots();

    chk('「存备注」绑上了 onclick', typeof saveBtn.onclick === 'function');
    chk('上锁开关绑上了 onchange', typeof lockBox.onchange === 'function');
    chk('上锁开关走 onchange 而非 onclick（避免重复提交）',
      typeof lockBox.onchange === 'function' && typeof lockBox.onclick !== 'function');

    if (typeof saveBtn.onclick === 'function') {
      await saveBtn.onclick();
      const r = REQS.find(x => x.url === '/api/snapshot/note');
      chk('存备注打到 /api/snapshot/note（POST）', !!r && r.method === 'POST',
        JSON.stringify(REQS));
      chk('备注请求体带上 ts 与新的 tag',
        !!r && r.body && r.body.ts === '20260905-505050' && r.body.tag === '新备注',
        JSON.stringify(r && r.body));
      chk('提交后按钮恢复可用（没卡在 disabled）', saveBtn.disabled === false);
    }

    REQS.length = 0;
    if (typeof lockBox.onchange === 'function') {
      lockBox.checked = true;
      await lockBox.onchange();
      const r = REQS.find(x => x.url === '/api/snapshot/protect');
      chk('上锁开关打到 /api/snapshot/protect（POST）', !!r && r.method === 'POST',
        JSON.stringify(REQS));
      chk('上锁请求体带 ts 与 protected:true',
        !!r && r.body && r.body.ts === '20260905-505050' && r.body.protected === true,
        JSON.stringify(r && r.body));
    }

    // 后端拒绝时，界面要把勾去掉
    REQS.length = 0;
    PROTECT_FAILS = true;
    if (typeof lockBox.onchange === 'function') {
      lockBox.checked = true;
      await lockBox.onchange();
      PROTECT_FAILS = false;
      chk('上锁被拒时界面把勾去掉（不显示后端不认同的状态）',
        lockBox.checked === false, 'checked=' + lockBox.checked);
    }
  });

  // ================================================================
  console.log('--- 用例 5：删受保护快照 → 二次确认 → 带 force 重试 ---');
  getEl('#sy-snapout')._html = '';
  SNAP_STATE = { items: [mkSnap('20260906-606060', 'locked', true)],
    root: '/x', scope: [], disk: null };
  DELETE_NEEDS_FORCE = true;
  run('globalThis.__modals.length = 0');
  const delLocked = mkEl('dl'); delLocked.dataset.del = '20260906-606060';

  await withFakes({ 'data-del': [delLocked] }, null, async () => {
    REQS.length = 0;
    await callListSnapshots();
    chk('删除按钮绑上了', typeof delLocked.onclick === 'function');
    if (typeof delLocked.onclick !== 'function') return;

    await delLocked.onclick();                 // 第一次：后端回 needs_force
    const first = REQS.find(x => /snapshot\/delete/.test(x.url));
    chk('第一次删除不带 force', !!first && !first.body.force,
      JSON.stringify(first && first.body));
    const ms = modals();
    chk('needs_force 触发了二次确认弹窗', ms.length === 1, '弹窗数 ' + ms.length);
    if (ms.length === 1) {
      chk('弹窗标题点明「已上锁」', /已上锁/.test(ms[0].t), ms[0].t);
      chk('弹窗说明上锁只挡自动清理', /自动清理不会删除它/.test(ms[0].h));
      chk('弹窗按钮文案是「仍要删除」（不是「确定」）',
        ms[0].txt === '仍要删除', ms[0].txt);
      REQS.length = 0;
      await ms[0].cb();                        // 点确认 → 带 force 重试
      const second = REQS.find(x => /snapshot\/delete/.test(x.url));
      chk('确认后带 force=true 重试', !!second && second.body.force === true,
        JSON.stringify(second && second.body));
    }
  });

  // ================================================================
  console.log('--- 用例 6：删未受保护快照 —— 一次就删，不多问 ---');
  getEl('#sy-snapout')._html = '';
  SNAP_STATE = { items: [mkSnap('20260907-707070', 'plain', false)],
    root: '/x', scope: [], disk: null };
  DELETE_NEEDS_FORCE = false;
  run('globalThis.__modals.length = 0');
  const delPlain = mkEl('dp'); delPlain.dataset.del = '20260907-707070';

  await withFakes({ 'data-del': [delPlain] }, null, async () => {
    REQS.length = 0;
    await callListSnapshots();
    if (typeof delPlain.onclick !== 'function') {
      chk('删除按钮绑上了', false);
      return;
    }
    await delPlain.onclick();
    chk('未受保护的快照不弹二次确认', modals().length === 0,
      '弹窗数 ' + modals().length);
    const r = REQS.find(x => /snapshot\/delete/.test(x.url));
    chk('一次就删（不带 force）', !!r && !r.body.force,
      JSON.stringify(r && r.body));
  });

  // ================================================================
  console.log('--- 用例 7：表格结构自洽 ---');
  getEl('#sy-snapout')._html = '';
  SNAP_STATE = { items: [mkSnap('20260908-808080', 'a', false),
    mkSnap('20260909-909090', 'b', true)], root: '/x', scope: [], disk: null };
  await callListSnapshots();
  const h7 = snapHtml();
  // ⛔ `<th[^>]*>` 连 `<thead>` 一起匹配了（thead 的 t-h-e-a-d
  // 恰好满足 `<th` + `>`）。所以要么带上前导空白限定，要么直接
  // 剔掉 thead。数出来 7 而产品明明是 6 列 —— 判据自己的锅。
  const thCount = (snapHtml().match(/<th[\s>]/g) || []).length;
  chk('表头 6 列（时间/备注/保护/大小/配置库/操作）',
    thCount === 6, '实际 ' + thCount);
  chk('每行 6 个单元格（2 行 = 12）',
    (h7.match(/<td[^>]*>/g) || []).length === 12,
    (h7.match(/<td[^>]*>/g) || []).length);
  chk('两行里恰好一行为checked', (h7.match(/data-lock="[^"]*"[^>]*checked/g) || []).length === 1,
    (h7.match(/data-lock="[^"]*"[^>]*checked/g) || []).length);
}

main().then(() => {
  console.log();
  console.log('='.repeat(66));
  console.log('通过 ' + pass + ' 项，失败 ' + fail + ' 项');
  if (fail) { console.log(); bad.forEach(d => console.log('  · ' + d)); }
  console.log('='.repeat(66));
  process.exit(fail ? 1 : 0);
}).catch(e => {
  console.error('主流程异常：');
  console.error(e.stack || e.message);
  console.log();
  console.log('通过 ' + pass + ' 项，失败 ' + (fail + 1) + ' 项（主流程异常）');
  process.exit(1);
});
