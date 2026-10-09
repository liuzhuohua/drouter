#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验 realtime.js 的绘制逻辑（不依赖浏览器）。

用 Node + 最小 canvas 桩跑一遍 draw()，检查：
  · 点数不足时不会越界 / 不会把旧点挤到左边
  · Y 轴有下限（负载 0.05 不会被自适应放大成满格）
  · 超上限时降采样确实丢一半而不是每次都丢
  · 值为 0 时柱高为 0（不画 1px 假柱）
  · 0 长度数组不抛异常

为什么不用真浏览器：sandbox 里没有 canvas 实现，
而这些纯计算分支用桩就能验 —— 测的是**逻辑**，不是像素。
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(os.path.dirname(HERE), 'web', 'realtime.js')
NODE = None
for base in (r'C:\Users\lyrz-pve-win10\.workbuddy\binaries\node\versions',
             '/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions'):
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, d, 'node.exe' if os.name == 'nt' else 'node')
            if os.path.isfile(p):
                NODE = p
                break
    if NODE:
        break
if not NODE:
    print('找不到 node，可执行验证')
    sys.exit(0)

HARNESS = r"""
// ---- 最小 canvas 桩：记录所有绘制调用，便于断言 ----
function mkCanvas() {
  const calls = { fillRect: [], fillText: [], stroke: 0, clear: 0, w: 0, h: 0 };
  return {
    width: 0, height: 0, style: {},
    getContext() {
      return {
        setTransform() {}, clearRect() { calls.clear++; },
        beginPath() {}, moveTo() {}, lineTo() { calls.stroke++; }, stroke() {},
        fillRect(x, y, w, h) { calls.fillRect.push({ x, y, w, h }); },
        fillText(t) { calls.fillText.push(t); },
        fillStyle: '', font: '', textAlign: '',
      };
    },
  };
}

const MAX_POINTS = 60;
const results = [];
function ck(name, cond, extra) {
  results.push({ name, ok: !!cond, extra: extra === undefined ? '' : String(extra) });
}

// 把 realtime.js 里的纯函数抠出来跑（IIFE 里的东西不导出，
// 所以用 vm 跑一遍并从 canvas 调用里观察）
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8')
  // 去掉依赖 DOM 的初始化块，只留 HIST / push / yRange / draw
  .replace(/document\.addEventListener[\s\S]*$/m, '');

const ctxObj = { window: {}, document: { getElementById: () => null,
                                        querySelector: () => null,
                                        readyState: 'complete' },
                 api: () => Promise.resolve({ data: null }),
                 setInterval: () => 0, clearInterval: () => {},
                 devicePixelRatio: 1 };

// 手工提取：HIST/push/yRange/draw 定义在 IIFE 内，
// 所以我们构造一个最小宿主把整个文件跑起来，再从 canvas 桩观察。
const sandbox = {
  window: ctxObj.window, document: ctxObj.document,
  setInterval: ctxObj.setInterval, clearInterval: ctxObj.clearInterval,
  console,
};
sandbox.window.d = sandbox.window.document = ctxObj.document;
// 暴露内部函数供断言
const wrapped = src.replace(
  'window.__drouterRealtime = true;',
  'window.__drouterRealtime = true; window.__rt_test = true;');

// 真正跑：用 vm + 一份把内部符号挂到 window 的包装
const vm = require('vm');
const exposed = {};
const code = `
(function(){
  ${src}
  // IIFE 内的 HIST/push/yRange/draw 通过 this 暴露给测试
})();
`;
// 简单起见：把关键函数抽出来单独测
const grab = (name) => {
  const m = re_search(src, name);
  if (!m) return null;
  const start = m.index;
  // 找到函数体结束（按大括号配平）
  let i = src.indexOf('{', start), d = 0;
  for (let k = i; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(start, k + 1); }
  }
  return null;
};
function re_search(s, name) {
  const i = s.indexOf('function ' + name);
  return i < 0 ? null : { index: i };
}

const fns = {};
['push', 'yRange', 'draw'].forEach(n => {
  const body = grab(n);
  if (body) fns[n] = body;
});
ck('三个关键函数都抠到了', Object.keys(fns).length === 3, Object.keys(fns).join(','));

// 三个函数体拼在同一作用域里求值 —— draw 内部会调 yRange，
// 分开 new Function 会 ReferenceError（1.0.10 踩过）。
const allSrc = ['yRange', 'push', 'draw']
  .map(n => fns[n]).filter(Boolean).join('\n');
const api = new Function('MAX_POINTS', 'window', 'DPR_CAP',
  allSrc + '\nreturn { yRange: yRange, push: push, draw: draw };')(
    MAX_POINTS, { devicePixelRatio: 1 }, 2);
Object.keys(api).forEach(k => { fns[k] = api[k]; });

// ---- push：未超上限时不丢点 ----
const h1 = { series: [[], []], labels: [] };
for (let i = 0; i < 10; i++) fns.push(h1, [i, i * 2], 't' + i);
ck('push 不丢点（未超上限）', h1.series[0].length === 10, h1.series[0].length);
ck('push 保序', h1.series[0][0] === 0 && h1.series[0][9] === 9);

// ---- push：超过 MAX_POINTS*2 才降采样，且只丢一半 ----
const h2 = { series: [[]], labels: [] };
for (let i = 0; i < MAX_POINTS * 2; i++) fns.push(h2, [i], 't');
ck('恰好到上限不降采样', h2.series[0].length === MAX_POINTS * 2, h2.series[0].length);
fns.push(h2, [999], 't');
ck('超上限后降采样（丢一半）', h2.series[0].length < MAX_POINTS * 2,
   h2.series[0].length);
// ⚠️ 判据第一版写「降采样后仍是偶数长度」是**错的**：
//    121 个点隔一个取一个 = 61 个，61 是奇数，属正常。
//    真正要保证的是「长度大致减半」且「不越上限」。
ck('降采样后长度减半',
   h2.series[0].length > MAX_POINTS
   && h2.series[0].length <= MAX_POINTS + 1,
   h2.series[0].length);
// ⛔ 真 bug 防线：多条 series 各自判断降采样会导致长度不一致，
//    时间轴错位（柱子画到错误的时间点）。1.0.10 真跑抓到过（长度 121）。
ck('降采样后各 series 长度一致',
   h2.series.every(a => a.length === h2.series[0].length),
   h2.series.map(a => a.length).join(','));
ck('降采样后 series 与 labels 长度一致',
   h2.series[0].length === h2.labels.length,
   h2.series[0].length + ' vs ' + h2.labels.length);
// 多条 series 的场景
const h3 = { series: [[], [], []], labels: [] };
for (let i = 0; i < MAX_POINTS * 2 + 5; i++) fns.push(h3, [i, i * 2, i * 3], 't' + i);
ck('3 条 series 长度始终一致',
   h3.series[0].length === h3.series[1].length
   && h3.series[1].length === h3.series[2].length,
   h3.series.map(a => a.length).join(','));
ck('3 条 series 都与 labels 一致',
   h3.series.every(a => a.length === h3.labels.length),
   h3.labels.length);

// ---- yRange：必须保留下限 ----
ck('空数据不崩', typeof fns.yRange([], 1).hi === 'number');
const r1 = fns.yRange([0.05, 0.1, 0.06], 1);
ck('有 hardMin 时上限不小于下限的 1.5 倍', r1.hi >= 1.5, r1.hi.toFixed(2));
const r2 = fns.yRange([0, 0, 0], 1);
ck('全 0 时也不塌成 0', r2.hi > 0, r2.hi);
const r3 = fns.yRange([5], 1);
ck('有峰值时留 15% 余量', r3.hi >= 5 * 1.15 - 1e-9, r3.hi);
ck('lo 恒为 0（柱从底部起）', r3.lo === 0, r3.lo);

// ---- draw：用桩跑，检查不抛异常且产出合理的柱 ----
const mkSeries = (arr, color) => ({ name: 's', color, data: arr });
function tryDraw(series, hardMin) {
  const cv = mkCanvas();
  cv.clientWidth = 300; cv.clientHeight = 74;
  fns.draw(cv, { series: series, hardMin: hardMin || 0,
                 fmt: v => String(v) });
  return cv;
}
let ok = true, err = '';
try { tryDraw([mkSeries([], '#111')], 1); } catch (e) { ok = false; err = e.message; }
ck('空数组不抛异常', ok, err);

ok = true; err = '';
let cv = null;
try {
  cv = tryDraw([mkSeries([0, 0, 0], '#111')], 1);
} catch (e) { ok = false; err = e.message; }
ck('全 0 值不抛异常', ok, err);

ok = true; err = '';
try {
  const arr = [];
  for (let i = 0; i < 200; i++) arr.push(Math.random() * 100);
  cv = tryDraw([mkSeries(arr, '#111'), mkSeries(arr, '#222')], 1);
} catch (e) { ok = false; err = e.message; }
ck('点超上限（200 > 120）不抛异常', ok, err);


// 用闭包捕获的 calls 看不到，改用 getContext 返回对象的记录
const cap = [];
const c3 = {
  width: 0, height: 0, clientWidth: 300, clientHeight: 74,
  getContext() {
    return { setTransform(){}, clearRect(){}, beginPath(){}, moveTo(){},
             lineTo(){}, stroke(){}, fillStyle:'', font:'', textAlign:'',
             fillRect(x,y,w,h){ cap.push({x,y,w,h}); },
             fillText(){} };
  },
};
fns.draw(c3, { series: [mkSeries([0, 5, 0], '#111')], hardMin: 1,
               fmt: v => String(v) });
ck('确实画出了柱子', cap.length === 3, cap.length);
const zeroBars = cap.filter(b => Math.abs(b.h) < 0.001);
ck('值为 0 的点柱高为 0（不画假柱）', zeroBars.length === 2, zeroBars.length);
const nonzero = cap.find(b => b.h > 1);
ck('非 0 的点有可见高度', !!nonzero, JSON.stringify(cap));
ck('柱子高度不超过画布高', cap.every(b => b.h <= 74), Math.max.apply(null, cap.map(b=>b.h)));

// ---- 越界检查：柱子 x 不能超出画布 ----
ck('柱子 x 不越界（padL..cssW-padR）',
   cap.every(b => b.x >= -1 && b.x + b.w <= 301), JSON.stringify(cap[0]));

const bad = results.filter(r => !r.ok);
results.forEach(r => console.log((r.ok ? 'ok    ' : 'FAIL  ') + r.name
  + (r.ok ? '' : '  ' + r.extra)));
console.log('');
console.log('绘制逻辑验证 ' + results.length + ' 条，失败 ' + bad.length + ' 条');
process.exit(bad.length ? 1 : 0);
"""

fd, path = tempfile.mkstemp(suffix='.js')
os.close(fd)
with open(path, 'w', encoding='utf-8') as f:
    f.write(HARNESS)

r = subprocess.run([NODE, path, JS], capture_output=True)
out = (r.stdout or b'').decode('utf-8', 'replace')
err = (r.stderr or b'').decode('utf-8', 'replace')
os.unlink(path)
print(out)
if err.strip():
    print('--- stderr ---')
    print(err[:1500])
sys.exit(r.returncode)
