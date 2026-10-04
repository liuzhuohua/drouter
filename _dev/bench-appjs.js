// 量 app.js 的真实解析/执行成本，以及各种压缩格式的实际收益。
// 目的：优化前先有数字，别凭感觉改。
const fs = require('fs');
const zlib = require('zlib');
const path = require('path');

const ROOT = path.dirname(__dirname);
const src = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8');

function kb(n) { return (n / 1024).toFixed(0) + ' KB'; }

console.log('=== 体积 ===');
console.log('  原始        :', kb(src.length));
console.log('  gzip -9     :', kb(zlib.gzipSync(src, { level: 9 }).length));
try {
  console.log('  brotli q11  :', kb(zlib.brotliCompressSync(src, {
    params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 11 } }).length));
} catch (e) { console.log('  brotli      : 不可用', e.message); }

// 语法编译：V8 解析 674KB 源码的真实耗时
let t = Date.now();
try {
  new Function(src);
} catch (e) {
  console.log('  语法错误:', e.message);
  process.exit(1);
}
console.log('=== 成本 ===');
console.log('  语法编译    :', (Date.now() - t), 'ms');

// 顶层执行：把 DOM 打桩，量「首屏前」跑多少 JS
function stub() {
  const el = () => ({
    style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, removeChild() {}, setAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], insertAdjacentHTML() {}, focus() {}, click() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    children: [], childNodes: [], innerHTML: '', textContent: '', value: '',
  });
  global.document = {
    getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    createElement: el, createElementNS: el, createTextNode: () => ({}),
    body: el(), head: el(), documentElement: el(),
    addEventListener() {}, removeEventListener() {}, cookie: '',
    createDocumentFragment: () => el(),
  };
  global.window = {
    addEventListener() {}, removeEventListener() {},
    location: { href: '', hash: '', search: '', pathname: '/', reload() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, innerWidth: 1280, innerHeight: 800,
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({}), scrollTo() {}, alert() {}, confirm: () => true,
  };
  global.localStorage = global.window.localStorage;
  global.sessionStorage = global.window.sessionStorage;
  global.location = global.window.location;
  global.navigator = { userAgent: 'node', language: 'zh-CN', platform: 'linux' };
  global.history = { pushState() {}, replaceState() {} };
  global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  global.XMLHttpRequest = function () { this.open = () => {}; this.send = () => {}; };
  global.alert = () => {};
  global.confirm = () => true;
  global.CustomEvent = function () {};
  global.Event = function () {};
}
stub();

t = Date.now();
try {
  (new Function('return (function(){' + src + '\n})()'))();
} catch (e) {
  console.log('  顶层执行异常:', e.message);
}
console.log('  顶层执行    :', (Date.now() - t), 'ms');

// 全文里各「视图函数」占多少行 —— 决定要不要拆包
const funcs = [...src.matchAll(/^(?:async\s+)?function\s+(\w+)/gm)].map(m => m[1]);
console.log('=== 结构 ===');
console.log('  顶层函数数  :', funcs.length);
console.log('  总行数      :', src.split('\n').length);
