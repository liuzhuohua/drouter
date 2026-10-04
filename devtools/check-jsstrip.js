/**
 * jsstrip.py 的**独立交叉验证**（node 侧）。
 *
 * 分工：jsstrip.py 自己跑全部陷阱测试（那是它的主场，见那边）。
 * node 这边只做一件 python 做不到的事：**把剥完的 app.js 真的
 * 交给 V8 解析一遍**。如果 jsstrip 把哪段真代码误当注释剥掉了，
 * 或者剥到一半把字符串/模板串的边界搞坏了，产物就会语法错误 ——
 * 这条判据抓的是「语义层面的破坏」，比「某个字符串还在不在」硬得多。
 *
 * ⛔ 第一版在这里 spawn python 去要 jsstrip 的输出，结果撞上
 * Windows 的 EBUSY / rc=null（execFileSync 与 spawnSync 对「被占用
 * 的 exe」都直接失败），白跑两轮。**判据脚本自己因为环境抖动崩掉，
 * 比它要检的 bug 更让人分神。** 现在改成：node 读一份由调用方
 * （devtools 的运行脚本 / 人工）预先用 python 剥好的文件。
 *
 * ⛔ 另外这份文件的 docstring 第一版里写了块注释的起止字面量，
 * node 直接报 "Invalid regular expression: missing /" —— 正是本轮
 * 在修的那个 bug，我自己又犯了一次。在注释里描述注释语法时，
 * 得把它们拆开写。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.dirname(__dirname);
const APP = path.join(ROOT, 'web', 'app.js');

let pass = 0, fail = 0;
const detail = [];
function chk(desc, cond, extra) {
  if (cond) { pass++; console.log('[OK] ' + desc); }
  else {
    fail++; detail.push(desc);
    console.log('[NG] ' + desc);
    if (extra !== undefined) console.log('     ' + String(extra).slice(0, 400));
  }
}

const src = fs.readFileSync(APP, 'utf8');

// ---------- 1) 原文件本身必须是合法 JS ----------
try {
  new Function(src);
  chk('app.js 是合法 JS（V8 接受原文）', true);
} catch (e) {
  chk('app.js 是合法 JS（V8 接受原文）', false, e.message);
}

// ---------- 2) 剥后的代码必须仍是合法 JS ----------
// 产物由调用方预先生成：node 自己跑 python 这条路在 Windows 上不通。
const STRIPPED = process.env.JSSTRIP_OUT ||
  path.join(ROOT, '_dev', '.jsstrip-appjs.out');
if (!fs.existsSync(STRIPPED)) {
  chk('剥后产物存在（先跑 _dev/jsstrip.py 生成 ' +
    path.relative(ROOT, STRIPPED) + '）', false);
} else {
  const out = fs.readFileSync(STRIPPED, 'utf8');
  try {
    new Function(out);
    chk('剥后的代码仍是合法 JS（jsstrip 没有剥坏结构）', true);
  } catch (e) {
    chk('剥后的代码仍是合法 JS（jsstrip 没有剥坏结构）', false, e.message);
  }

  // ---------- 3) 剥后关键 token 数量不该变多 ----------
  // ⚠️ 判据是「只多不少」，**不是**「完全相等」。
  // 写死相等会红：注释里经常提到这些标识符（比如「这里之前用
  // innerHTML 于是…这种讲 bug 来历的话），剥注释会连带剥掉它们。
  // 而「变多」才是误吃的信号 —— 原文被切开重组时 token 数会膨胀。
  const TOKENS = ['function', 'const', 'innerHTML',
    'addEventListener', 'querySelector'];
  for (const t of TOKENS) {
    const re = new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
    const a = (src.match(re) || []).length;
    const b = (out.match(re) || []).length;
    chk('剥后 token 数量只多不少：' + t, b <= a, '原 ' + a + ' → 剥后 ' + b);
  }

  // ---------- 4) 剥后不含注释（否则等于没剥） ----------
  for (const marker of ['eatComment', 'CSS 注释']) {
    chk('剥后无注释残留 ' + marker, out.indexOf(marker) < 0);
  }

  // ---------- 5) 块注释起止必须配平 ----------
  const opens = (out.match(/\/\*/g) || []).length;
  const closes = (out.match(/\*\//g) || []).length;
  chk('剥后块注释起止配平（没有半开的残留）',
    opens === closes, '开 ' + opens + ' / 闭 ' + closes);
}

console.log();
console.log('='.repeat(60));
console.log('通过 ' + pass + ' 项，失败 ' + fail + ' 项');
if (fail) { console.log(); detail.forEach(d => console.log('  · ' + d)); }
console.log('='.repeat(60));
process.exit(fail ? 1 : 0);
