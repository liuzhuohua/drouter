// 手机适配（#14）专项检查
//   1) 静态要素：viewport、抽屉遮罩、断点规则、抽屉开关逻辑、表格包裹逻辑
//   2) 功能验证：用最小 DOM 桩跑一遍 wrapTables，确认真的包出了 .tw 且不会重复包
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'web', 'app.js');
const CSS = path.join(__dirname, '..', 'web', 'app.css');
const HTML = path.join(__dirname, '..', 'web', 'index.html');
const code = fs.readFileSync(APP, 'utf8');
const css = fs.readFileSync(CSS, 'utf8');
const html = fs.readFileSync(HTML, 'utf8');

let PASS = 0, FAIL = 0;
const MSG = [];
function has(name, cond, extra) {
  if (cond) PASS++;
  else { FAIL++; MSG.push('  ✘ ' + name + (extra ? '  → ' + extra : '')); }
}

// ---------------- 1. 静态要素 ----------------
has('index.html 有 viewport', /<meta\s+name="viewport"/.test(html));
has('index.html 有抽屉遮罩 #m-mask', /id="m-mask"/.test(html));
has('index.html 有侧栏开关 #btn-side', /id="btn-side"/.test(html));

has('CSS 有 .tw 横向滚动容器', /\.tw\{[^}]*overflow-x:auto/.test(css));
has('CSS 有 m-mask 遮罩样式', /\.m-mask\{/.test(css));
has('CSS 有 760px 断点', /@media \(?max-width:760px\)?/.test(css));
has('CSS 抽屉滑入规则', /#app\.m-drawer \.sidebar\{[^}]*transform:translateX\(0\)/.test(css));
has('CSS 侧栏在窄屏为 fixed 抽屉',
  /@media \(?max-width:760px\)?\{[\s\S]*?\.sidebar\{[^}]*position:fixed/.test(css));
has('CSS 窄屏输入框 16px（防 iOS 缩放）',
  /@media \(?max-width:760px\)?\{[\s\S]*?input,select,textarea\{font-size:16px\}/.test(css));
has('CSS 有安全区适配', /env\(safe-area-inset/.test(css));
has('CSS 弹窗窄屏适配', /\.modal-box\{[^}]*margin:0 12px/.test(css));

has('JS 有 isNarrow 判定', /const isNarrow\s*=/.test(code));
has('JS 有 closeDrawer', /function closeDrawer\(/.test(code));
has('JS 切换 m-drawer', /classList\.toggle\('m-drawer'\)/.test(code));
has('JS go() 里会关抽屉', /function go\([\s\S]{0,200}?closeDrawer\(\)/.test(code));
has('JS 遮罩点击关闭', /mMask\.onclick\s*=\s*closeDrawer/.test(code));
has('JS Esc 关闭抽屉', /e\.key === 'Escape'\) closeDrawer\(\)/.test(code));
has('JS 有 wrapTables', /function wrapTables\(/.test(code));
has('JS 用 MutationObserver 兜异步表格', /new MutationObserver\(/.test(code));
has('JS 窗口变宽会清抽屉', /mq\.matches\) closeDrawer\(\)/.test(code));

// ---------------- 2. wrapTables 功能验证 ----------------
// 最小 DOM 桩：只要能表达「节点 / 父节点 / class 列表 / 插入」即可
function mk(tag, cls) {
  const el = {
    tag, children: [], parentNode: null,
    classList: {
      _s: new Set(cls ? [cls] : []),
      add(c) { this._s.add(c); }, contains(c) { return this._s.has(c); },
    },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      node.parentNode = this;
      if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
      return node;
    },
    appendChild(node) {
      // 从原父节点摘下，再挂到新父节点
      if (node.parentNode) {
        const j = node.parentNode.children.indexOf(node);
        if (j >= 0) node.parentNode.children.splice(j, 1);
      }
      node.parentNode = this;
      this.children.push(node);
      return node;
    },
    querySelectorAll(sel) {
      const out = [];
      (function walk(n) {
        n.children.forEach(c => {
          if (sel === 'table' && c.tag === 'table') out.push(c);
          walk(c);
        });
      })(this);
      return out;
    },
  };
  // 真实 DOM 里 el.className = 'tw' 会同步反映到 classList，
  // 桩必须复刻这个联动，否则 wrapTables 的「已包过」判断永远失效。
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el.classList._s).join(' '); },
    set(v) { el.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

const documentStub = { createElement: t => mk(t) };
const sandbox = { document: documentStub, console };
vm.createContext(sandbox);

// 只抽取 wrapTables 函数体，避免牵扯整个 app.js
const m = code.match(/function wrapTables\(root\) \{[\s\S]*?\n\}/);
has('能抽到 wrapTables 源码', !!m);
if (m) {
  vm.runInContext(m[0], sandbox);

  const root = mk('div');
  const holder = mk('div', 'card');
  const t1 = mk('table'), t2 = mk('table');
  root.appendChild(holder); holder.appendChild(t1); holder.appendChild(t2);

  // 第一次：两个表都该被包上一层 .tw
  sandbox.wrapTables(root);
  const wrapped = holder.children.filter(c => c.classList.contains('tw'));
  has('首次包裹：两个表格都套上 .tw', wrapped.length === 2, wrapped.length);
  has('包裹后 table 的父节点是 .tw',
    wrapped.length === 2 && wrapped.every(w => w.children.length === 1 && w.children[0].tag === 'table'));

  // 第二次：幂等，不能重复套（否则每次刷新 DOM 都多一层）
  sandbox.wrapTables(root);
  const wrapped2 = holder.children.filter(c => c.classList.contains('tw'));
  has('二次包裹：幂等不重复', wrapped2.length === 2, wrapped2.length);
  has('二次包裹后结构仍是一层',
    wrapped2.every(w => w.children.length === 1 && w.children[0].tag === 'table'));

  // 空树不应报错
  let threw = false;
  try { sandbox.wrapTables(mk('div')); } catch (e) { threw = true; }
  has('空容器不抛异常', !threw);
}

console.log('='.repeat(60));
console.log(`手机适配检查：通过 ${PASS}｜失败 ${FAIL}`);
if (FAIL) { console.log('-'.repeat(60)); MSG.forEach(x => console.log(x)); }
process.exit(FAIL ? 1 : 0);
