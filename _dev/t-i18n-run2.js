/* 端到端验证 i18n：eval 真 i18n.js，把 app.js 里**所有** t('中文') 调用
   在两种语言下各跑一遍。
   A 段：英文界面下不得出现汉字（漏词会回落中文 —— 用户直接能看见）
   B 段：中文界面下返回值必须**逐字等于**代码里的原文
        （换行形态别名命中时若返回词条的 zh，会多出/少掉反斜杠）

   ⛔ 为什么必须真跑：静态判据（正则查 key 是否在字典里）看不出
      「换行形态不一致」这类问题 —— 2026-10-05 静态覆盖率显示 100%，
      真跑却抓到 24 条英文下仍显示中文。
   用法：node _dev/t-i18n-run2.js    （在项目根目录下跑） */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const IJ = path.join(ROOT, 'web', 'i18n.js');
const APP = path.join(ROOT, 'web', 'app.js');

/* 最小 DOM 桩：i18n.js 里的 applyDom / setLang 会用到。
   ⛔ 别用「什么方法都返回自己」的空对象 —— 那样
   document.documentElement.lang = x 这种赋值会静默成功，
   但 querySelectorAll 返回 undefined 时 forEach 直接抛，
   错误信息会指向与被测点无关的地方。 */
const noop = () => 0;
const mkEl = () => ({
  classList: { add: noop, remove: noop, contains: () => false },
  style: {}, dataset: {},
  appendChild: noop, setAttribute: noop, removeAttribute: noop,
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener: noop, textContent: '', innerHTML: '',
});
global.window = { addEventListener: noop, i18n: null };
global.document = {
  documentElement: {},
  body: mkEl(), head: mkEl(),
  querySelectorAll: () => [],
  getElementById: () => null,
  createElement: mkEl,
  addEventListener: noop,
};
global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
// Node 22 的 globalThis.navigator 是只读 getter（赋值会抛 TypeError），
// i18n.js 不用它，这里不设。

const src = fs.readFileSync(IJ, 'utf8');
// eslint-disable-next-line no-eval
eval(src);
const I = global.window.i18n;
if (!I) { console.error('FAIL: i18n.js 没有挂到 window.i18n'); process.exit(1); }

// 抽 app.js 里所有 t('…') 的字面量参数
const app = fs.readFileSync(APP, 'utf8');
const keys = new Set();
for (const m of app.matchAll(/t\('((?:[^'\\]|\\.)*)'/g)) {
  if (/[\u4e00-\u9fff]/.test(m[1])) keys.add(m[1]);
}
const arr = [...keys];

let fails = 0;
function ck(name, cond, detail) {
  if (cond) {
    console.log('  ok    ' + name);
  } else {
    fails++;
    console.log('  FAIL  ' + name + (detail ? '  ' + detail : ''));
  }
}

console.log('[t-i18n-run2] t() 中文 key 共 ' + arr.length + ' 条');
const origWarn = console.warn;
const warnings = [];
console.warn = (m) => { warnings.push(String(m)); };

// A 段：英文界面
I.setLang('en-US', { force: true });
const badEn = [];
for (const k of arr) {
  const v = I.t(k);
  if (/[\u4e00-\u9fff]/.test(v)) badEn.push([k, v]);
}
// B 段：中文界面
I.setLang('zh-CN', { force: true });
const badZh = [];
for (const k of arr) {
  const v = I.t(k);
  if (v !== k) badZh.push([k, v]);
}
console.warn = origWarn;

ck('英文界面无中文残留', badEn.length === 0,
   badEn.length + ' 条：' + badEn.slice(0, 3)
     .map(([k, v]) => JSON.stringify(k.slice(0, 34)) + ' -> ' + JSON.stringify(v.slice(0, 34))).join(' | '));
ck('中文界面逐字等于代码原文', badZh.length === 0,
   badZh.length + ' 条：' + badZh.slice(0, 3)
     .map(([k, v]) => JSON.stringify(k.slice(0, 34)) + ' -> ' + JSON.stringify(v.slice(0, 34))).join(' | '));
ck('无「缺少词条」告警',
   !warnings.some((w) => w.includes('缺少词条')),
   warnings.filter((w) => w.includes('缺少词条')).slice(0, 2).join(' | '));

console.log('\n==== t-i18n-run2: 3 条判据, ' + fails + ' 失败 ====');
if (fails) process.exit(1);
console.log('I18N_RUN2_OK');
