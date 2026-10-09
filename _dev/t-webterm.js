// Web 终端（#4 重做）的行为测试：在 node 里用最小 DOM 桩真实执行 app.js，
// 然后驱动内置的终端模拟器，验证 ANSI 光标 / 颜色 / 换行 / 分帧中文 等处理。
// 用法：node _dev/t-webterm.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'web', 'app.js');
const code = fs.readFileSync(APP, 'utf8');

function mkEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, checked: false,
    className: '', tagName: 'DIV', dataset: {}, style: {}, children: [],
    classList: {
      add(c) { el.className += ' ' + c; },
      remove(c) { el.className = el.className.replace(c, ''); },
      toggle() { return false; }, contains() { return false; },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { el.children.push(c); return c; },
    removeChild() {},
    remove() {},
    querySelector() { return mkEl('sub'); },
    querySelectorAll() { return []; },
    get parentElement() { return mkEl('parent'); },
    get firstChild() { return mkEl('first'); },
    focus() {}, select() {}, scrollIntoView() {}, click() {},
    onclick: null, onchange: null, oninput: null, onkeydown: null,
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
  querySelector: getEl,
  querySelectorAll: () => [],
  createElement: t => mkEl('new:' + t),
  body: mkEl('body'),
  addEventListener() {},
};
const sandbox = {
  document, console,
  // ⚠️ app.js 顶层就有 t('…') 调用（NAV 常量在模块加载时求值），
  //    缺这个桩会在 `g: t('概览')` 那行直接 ReferenceError。
  //    2026-10-05 部署时被它拦过。恒等桩即可：本判据不验文案内容。
  t: (k) => k,
  i18n: { t: (k) => k, setLang() {}, getLang: () => 'zh-CN', toggle() {},
          onChange() {}, register() {}, applyDom() {}, rerenderAll() {} },
  fetch: async () => ({ status: 200, ok: true, json: async () => ({ ok: true, data: {} }) }),
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: () => 0, alert() {}, confirm: () => false,
  location: { reload() {}, href: '' },
  navigator: { userAgent: 'node' },
  TextEncoder, TextDecoder, URL, Blob: function () {}, FormData: function () {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(code, ctx, { filename: APP });

let fails = 0;
function chk(label, cond, extra) {
  if (!cond) fails++;
  console.log('[' + (cond ? 'OK' : 'FAIL') + '] ' + label + (extra ? '  → ' + extra : ''));
}
function run(expr) { return vm.runInContext(expr, ctx); }
function cell(x, y) { return run('WS.grid[' + y + '][' + x + ']'); }
function rowText(y) {
  return run('(function(){var s="";for(var x=0;x<WS.cols;x++)s+=WS.grid[' + y + '][x].c;return s;})()');
}

console.log('=== 终端模拟器行为测试 ===');

// ---------- 基础输出 ----------
run('wsGridInit(20, 4)');
run("wsFeed('ab')");
chk('普通字符写入屏幕', cell(0, 0).c === 'a' && cell(1, 0).c === 'b',
  'row0=' + JSON.stringify(rowText(0)));
chk('光标随字符右移', run('WS.cx') === 2, 'cx=' + run('WS.cx'));

run("wsFeed('\\r\\n')");
chk('\\r\\n 换行并回到行首', run('WS.cy') === 1 && run('WS.cx') === 0);
run("wsFeed('cd')");
chk('第二行写入', rowText(1).indexOf('cd') === 0, 'row1=' + JSON.stringify(rowText(1)));

// ---------- 光标移动 ----------
run("wsFeed('\\b')");
chk('退格只移动光标不清字符', run('WS.cx') === 1);
run("wsFeed('\\t')");
chk('Tab 跳到 8 的倍数', run('WS.cx') === 8, 'cx=' + run('WS.cx'));
run("wsFeed('\\x1b[10;5H')");
chk('光标定位被限制在屏幕内', run('WS.cy') === 3 && run('WS.cx') === 4,
  'cy=' + run('WS.cy') + ' cx=' + run('WS.cx'));
run("wsFeed('\\x1b[2A')");
chk('上移两行', run('WS.cy') === 1);

// ---------- 颜色与属性 ----------
run('wsGridInit(20, 3)');
// 注意：颜色断言一律用 ASCII 字符。中文是宽字符、占两格，用汉字会把光标
// 推到偶数列，断言就变成在测「宽度」而不是「颜色」了。
run("wsFeed('\\x1b[31mR')");
chk('红色前景写入样式键', cell(0, 0).k.indexOf('f1') >= 0, 'k=' + cell(0, 0).k);
run("wsFeed('\\x1b[1mB')");
chk('粗体写入样式键', cell(1, 0).k.indexOf('b') >= 0, 'k=' + cell(1, 0).k);
run("wsFeed('\\x1b[0mZ')");
chk('复位后样式清空', cell(2, 0).k === '', 'k=' + JSON.stringify(cell(2, 0).k));
run("wsFeed('\\x1b[42mG')");
chk('绿色背景写入样式键', cell(3, 0).k.indexOf('g2') >= 0, 'k=' + cell(3, 0).k);

// ---------- 宽字符：中文 / 全角占两格 ----------
run('wsGridInit(20, 3)');
run("wsFeed('中文ab')");
chk('宽字符占两格（光标跳过第二格）', run('WS.cx') === 6, 'cx=' + run('WS.cx'));
chk('宽字符第二格是占位，不重复渲染', cell(1, 0).pad === 1, 'pad=' + JSON.stringify(cell(1, 0).pad));
chk('宽字符后面的字符位置正确', cell(4, 0).c === 'a' && cell(5, 0).c === 'b',
  'c4=' + cell(4, 0).c + ' c5=' + cell(5, 0).c);
chk('wsCharWidth 判定正确',
  run('wsCharWidth("中")') === 2 && run('wsCharWidth("a")') === 1
  && run('wsCharWidth("　")') === 2);

// ---------- 擦除 ----------
run('wsGridInit(10, 3)');
run("wsFeed('abcdef\\x1b[3D\\x1b[K')");
chk('ESC[K 从光标处擦到行尾', rowText(0).replace(/\s+$/, '') === 'abc',
  'row0=' + JSON.stringify(rowText(0)));
run("wsFeed('\\x1b[2J')");
chk('ESC[2J 清空整屏', rowText(0).trim() === '' && rowText(2).trim() === '');
run('wsGridInit(10, 3)');
run("wsFeed('xy\\x1b[H\\x1b[2P')");
chk('ESC[P 删除字符', rowText(0).trim() === '', 'row0=' + JSON.stringify(rowText(0)));

// ---------- OSC / 未知序列 ----------
run('wsGridInit(30, 3)');
run("wsFeed('\\x1b]0;窗口标题\\x07ok')");
chk('OSC（窗口标题）内容不落到屏幕上', rowText(0).indexOf('ok') === 0 && rowText(0).indexOf('标题') < 0,
  'row0=' + JSON.stringify(rowText(0)));
run('wsGridInit(30, 3)');
run("wsFeed('A\\x1b[?25lB')");
chk('未知/模式类序列被忽略且不影响后续输出', rowText(0).indexOf('AB') === 0,
  'row0=' + JSON.stringify(rowText(0)));

// ---------- 自动换行与滚动 ----------
run('wsGridInit(5, 3)');
run("wsFeed('abcdefg')");
chk('行尾自动换行', rowText(0) === 'abcde' && rowText(1).indexOf('fg') === 0,
  'row0=' + JSON.stringify(rowText(0)) + ' row1=' + JSON.stringify(rowText(1)));
run("wsFeed('\\r\\n1\\r\\n2\\r\\n3')");
chk('超出底部时滚动', run('WS.cy') === 2, 'cy=' + run('WS.cy'));
chk('滚出的行进入回滚区', run("$('#ws-scroll').children.length") >= 1,
  '回滚行数=' + run("$('#ws-scroll').children.length"));

// ---------- 分帧中文 ----------
run('wsGridInit(20, 3)');
const zh = '中文';
run('wsFeed(' + JSON.stringify(zh[0]) + ')');   // 「中」
run('wsFeed(' + JSON.stringify(zh[1]) + ')');   // 「文」，分两帧送
chk('多字节字符分帧不乱码', rowText(0).indexOf(zh) === 0, 'row0=' + JSON.stringify(rowText(0)));

// ---------- 渲染 ----------
run('wsGridInit(10, 2)');
run("wsFeed('\\x1b[31mab\\x1b[0mcd')");
const html = run('wsRowHtml(WS.grid[0], 0)');
chk('渲染出颜色 span', html.indexOf('span') >= 0 && html.indexOf('color:') >= 0, html.slice(0, 90));
const htmlCur = run('wsRowHtml(WS.grid[0], 1)');
chk('渲染出光标方块', htmlCur.indexOf('tcur') >= 0);
chk('渲染对 HTML 做了转义', run("wsRowHtml([{c:'<',k:''},{c:'>',k:''}], -1)").indexOf('&lt;') >= 0);

// ---------- 键盘映射 ----------
sandbox.__sent = [];
run('WS.sid = "x"; wsWrite = function(d){ __sent.push(d); };');
run("wsKeyDown({key:'c', ctrlKey:true, preventDefault:function(){}})");
chk('Ctrl+C 发送中断控制码', sandbox.__sent[0] === '\x03', JSON.stringify(sandbox.__sent[0]));
sandbox.__sent = [];
run("wsKeyDown({key:'v', ctrlKey:true, preventDefault:function(){}})");
chk('Ctrl+V 不被吞掉（留给浏览器粘贴）', sandbox.__sent.length === 0);
run("wsKeyDown({key:'Enter', preventDefault:function(){}})");
run("wsKeyDown({key:'ArrowUp', preventDefault:function(){}})");
run("wsKeyDown({key:'Backspace', preventDefault:function(){}})");
chk('Enter / 方向键 / 退格 都有映射',
  sandbox.__sent[0] === '\r' && sandbox.__sent[1] === '\x1b[A' && sandbox.__sent[2] === '\x7f',
  JSON.stringify(sandbox.__sent.slice(0, 3)));
sandbox.__sent = [];
run("wsKeyDown({key:'x', preventDefault:function(){}})");
chk('普通字符交给 input 事件（keydown 不重复发送）', sandbox.__sent.length === 0);

console.log('\n结果: ' + (fails === 0 ? '全部通过' : fails + ' 项失败'));
process.exit(fails ? 1 : 0);
