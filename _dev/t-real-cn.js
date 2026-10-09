#!/usr/bin/env node
// -*- coding: utf-8 -*-
// 扫描真机 API 抓取数据（_real_api_*.json）里的中文字符串，
// 比对 i18n.js 的 bt 表键集合，列出「英文界面必然残留」的未覆盖串。
// 用法: node _dev/t-real-cn.js [--all]
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ROOT = path.resolve(__dirname, '..');

const HAS_CN = /[一-鿿]/;

// ---------- 加载 i18n ----------
function loadI18n() {
  const src = fs.readFileSync(path.join(ROOT, 'web', 'i18n.js'), 'utf8');
  const sandbox = {
    console,
    window: {}, document: { documentElement: { lang: 'zh-CN' } },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { language: 'zh-CN' }, location: { href: '' },
    setTimeout, clearTimeout, setInterval, clearInterval,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  try { vm.runInContext(src, sandbox, { filename: 'i18n.js' }); } catch (e) {
    console.log('i18n 加载失败: ' + e.message);
  }
  const i18n = sandbox.window && sandbox.window.i18n;
  if (!i18n) { console.log('未取到 window.i18n'); return { bt: {}, raw: {} }; }
  const dict = i18n._dict || i18n.dict || i18n.DICT || {};
  return { bt: dict.bt || {}, raw: dict };
}

// 收集 bt 表全部键（中文原文）+ 全部值
function collectBtKeys(bt) {
  const keys = new Set();
  const tables = {};
  for (const t of Object.keys(bt || {})) {
    tables[t] = Object.keys(bt[t] || {}).length;
    for (const k of Object.keys(bt[t] || {})) keys.add(k);
  }
  return { keys, tables };
}

// ---------- 遍历 JSON ----------
function walk(node, p, out, file) {
  if (node === null || node === undefined) return;
  if (typeof node === 'string') {
    if (HAS_CN.test(node)) out.push({ file, path: p, s: node });
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((v, i) => walk(v, p + '[' + i + ']', out, file));
    return;
  }
  if (typeof node === 'object') {
    for (const k of Object.keys(node)) walk(node[k], p ? p + '.' + k : k, out, file);
  }
}

function main() {
  const showAll = process.argv.includes('--all');
  const { bt, raw } = loadI18n();
  const { keys: btKeys, tables } = collectBtKeys(bt);
  console.log('bt 表: ' + Object.keys(tables).length + ' 张, 键总数 ' + btKeys.size);
  console.log('');

  const files = fs.readdirSync(ROOT).filter(f => /^_real_api_.*\.json$/.test(f));
  const uncovered = new Map(); // s -> [{file,path}]
  const covered = new Set();
  let total = 0;

  for (const f of files) {
    let data;
    try { data = JSON.parse(fs.readFileSync(path.join(ROOT, f), 'utf8')); }
    catch (e) { console.log('解析失败 ' + f + ': ' + e.message); continue; }
    const out = [];
    walk(data, '', out, f);
    total += out.length;
    for (const it of out) {
      const s = it.s;
      if (btKeys.has(s)) { covered.add(s); continue; }
      if (!uncovered.has(s)) uncovered.set(s, []);
      uncovered.get(s).push({ file: f, path: it.path });
    }
  }

  console.log('真机数据中文串: ' + total + ' 条 (去重 ' + (covered.size + uncovered.size) + ')');
  console.log('已被 bt 表覆盖: ' + covered.size + ' 条');
  console.log('未覆盖(去重): ' + uncovered.size + ' 条');
  console.log('');

  // 按出现次数排序输出
  const rows = [...uncovered.entries()].sort((a, b) => b[1].length - a[1].length);
  const LIMIT = showAll ? rows.length : 120;
  for (let i = 0; i < Math.min(LIMIT, rows.length); i++) {
    const [s, hits] = rows[i];
    const where = [...new Set(hits.map(h => h.file.replace('_real_api_', '').replace('.json', '') + ':' + h.path))];
    console.log('[' + hits.length + '] ' + s.slice(0, 160));
    console.log('      ' + where.slice(0, 3).join(' | '));
  }
  if (!showAll && rows.length > LIMIT) console.log('... 其余 ' + (rows.length - LIMIT) + ' 条，加 --all 查看');
}

main();
