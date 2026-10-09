#!/usr/bin/env node
// -*- coding: utf-8 -*-
// 真机 payload 中文串覆盖分析（v2）
//  对每条中文串判定是否已被下列任一机制覆盖：
//   1) bt 表键（bt4 查表）
//   2) en-US 词典键（t() 直达，key 为中文原文）
//   3) 前端变换函数（ulogMsgI18n / audDetail / 等级映射 等）
//  三者皆无 => 英文界面必然残留，列为待修。
// 用法: node _dev/t-real-cn2.js [分组名...]
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ROOT = path.resolve(__dirname, '..');
const HAS_CN = /[一-鿿]/;

function loadI18n() {
  const src = fs.readFileSync(path.join(ROOT, 'web', 'i18n.js'), 'utf8');
  const sb = { console };
  sb.window = sb; sb.globalThis = sb;
  sb.document = { documentElement: { lang: 'zh-CN' } };
  sb.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  sb.navigator = { language: 'zh-CN' }; sb.location = { href: '' };
  sb.setTimeout = setTimeout; sb.clearTimeout = clearTimeout;
  sb.setInterval = setInterval; sb.clearInterval = clearInterval;
  vm.createContext(sb);
  vm.runInContext(src, sb, { filename: 'i18n.js' });
  const i18n = sb.i18n || sb.window.i18n;
  const dict = (i18n && (i18n._dict || i18n.dict || i18n.DICT)) || {};
  return dict;
}

const dict = loadI18n();
const bt = dict.bt || {};
const btKeys = new Set();
for (const t of Object.keys(bt)) for (const k of Object.keys(bt[t] || {})) btKeys.add(k);

// en-US 词典的全部键（含点分路径）
const enKeys = new Set();
function collectKeys(o, p) {
  if (o === null || typeof o !== 'object') { if (p) enKeys.add(p); return; }
  for (const k of Object.keys(o)) collectKeys(o[k], p ? p + '.' + k : k);
}
if (dict['en-US']) collectKeys(dict['en-US'], '');

const appSrc = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8');
// 前端已内置变换的中文串：出现在这些函数体里的字面量
function inTransform(s) {
  const idx = appSrc.indexOf(s);
  if (idx < 0) return false;
  // 往前 1500 字符里是否落在已知的变换函数内
  const pre = appSrc.slice(Math.max(0, idx - 1500), idx);
  return /function\s+(ulogMsgI18n|audDetail|ulLevel|lvLabel|sevLabel)/.test(pre)
    || /ulogMsgI18n|audDetail/.test(pre);
}

function walk(node, p, out) {
  if (node === null || node === undefined) return;
  if (typeof node === 'string') { if (HAS_CN.test(node)) out.push({ p, s: node }); return; }
  if (Array.isArray(node)) { node.forEach((v, i) => walk(v, p + '[' + i + ']', out)); return; }
  if (typeof node === 'object') for (const k of Object.keys(node)) walk(node[k], p ? p + '.' + k : k, out);
}

const only = process.argv.slice(2);
const files = ['_real_api_more.json', '_real_api_audit.json', '_real_api_ulog.json',
  '_real_api_deps_check.json', '_real_api_dpi.json', '_real_api_qos.json',
  '_real_api_share.json', '_real_api_storage.json', '_real_api_accel.json',
  '_real_api_acl.json', '_real_api_sysinfo.json', '_real_api_wan_log.json',
  '_real_api_opensoho.json', '_real_api_pppoe-multi.json', '_real_api_vlan.json',
  '_real_api_wol.json', '_real_api_docker.json', '_real_api_nat_last.json'];

const un = new Map();
for (const f of files) {
  const fp = path.join(ROOT, f);
  if (!fs.existsSync(fp)) continue;
  let data; try { data = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch (e) { continue; }
  // _real_api_more.json 是按 action 分组的 dict
  const groups = (f === '_real_api_more.json') ? data : { '(root)': data };
  for (const g of Object.keys(groups)) {
    if (only.length && !only.includes(g)) continue;
    const out = [];
    walk(groups[g], '', out);
    for (const it of out) {
      const s = it.s;
      if (btKeys.has(s) || enKeys.has(s) || inTransform(s)) continue;
      if (!un.has(s)) un.set(s, new Set());
      un.get(s).add(g + ' → ' + it.p);
    }
  }
}

const rows = [...un.entries()].sort((a, b) => a[0].length - b[0].length);
console.log('未覆盖中文串: ' + rows.length + ' 条');
console.log('');
for (const [s, where] of rows) {
  console.log('◆ ' + s.slice(0, 200));
  console.log('   ' + [...where].slice(0, 2).join('\n   '));
}
