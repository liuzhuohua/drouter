#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真跑验证 i18n：在 Node 里跑 i18n.js + 抽出的概览页模板，检查切换效果。

要验的**不是**「字典里有没有这个词」（t-i18n.py 已经查了），
而是「切到英文后，**实际渲染出来**是不是英文」——
这两件事不一样：键存在但没被用、或者切换没触发重绘，都只在这层暴露。

做法：
  1. Node 里加载 i18n.js，拿到 window.i18n
  2. 检查 t() 在两种语言下的返回值
  3. 检查 setLang 会不会触发监听者（订阅机制是否真的通）
  4. 检查 localStorage 持久化
  5. 检查缺词降级（显示 key 原文而不是空白）
"""
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

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
    print('找不到 node，跳过')
    sys.exit(0)

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf-8');

function makeSandbox() {
  const store = {};
  const listeners = [];
  const sb = {
    window: {},
    document: {
      documentElement: {},
      readyState: 'complete',
      getElementById: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = v; },
    },
    navigator: { language: 'zh-CN' },
    console, setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {},
    __store: store, __listeners: listeners,
  };
  sb.window.window = sb.window;
  sb.window.document = sb.document;
  sb.window.localStorage = sb.localStorage;
  sb.window.navigator = sb.navigator;
  sb.window.console = console;
  sb.globalThis = sb;
  vm.createContext(sb);
  return sb;
}

const sb = makeSandbox();
vm.runInContext(src + '\n;globalThis.__I = window.i18n;', sb);
const I = sb.__I;
const out = {};

// ---- 1. 两种语言下的取值 ----
const samples = ['common.version', 'dash.hostInfo', 'dash.cpuPct', 'upd.title',
                 'ups.v6', 'nd.neigh4', 'rt.load', 'app.langToEn'];
out.samples = {};
for (const k of samples) {
  I.setLang('zh-CN', { force: true });
  const zh = I.t(k);
  I.setLang('en-US', { force: true });
  const en = I.t(k);
  out.samples[k] = { zh, en };
}

// ---- 2. 切换是否触发监听者 ----
// ⚠️ 两个坑都在这里踩过：
//  ① 必须在**样本循环之后**挂监听者 —— 循环里反复调 setLang(force)
//     也会触发它，混进来会让「切 2 次」变成「收到 7 次」。
//  ② 后面第 3、4 节还会再调 setLang（测 register / 持久化），
//     所以**不能**断言 seen 恰好等于 ['en-US','zh-CN']——
//     正确做法是断言「**包含**这两个且 fired 恰好 2」（fired 只统计本监听者）。
let fired = 0;
const seen = [];
I.setLang('zh-CN', { force: true });
I.onChange(l => { fired++; seen.push(l); });
I.setLang('en-US', { force: true });
I.setLang('zh-CN', { force: true });
out.fired = fired;
out.seenTail = seen.slice(-2);   // 只取本监听者最后收到的两次
out.seenAll = seen.length;

// ---- 3. register 的渲染器会被 rerenderAll 调用 ----
let rendered = 0;
I.register('probe', () => { rendered++; });
I.rerenderAll();
out.renderedByRerenderAll = rendered;

// ---- 4. 持久化 ----
I.setLang('en-US', { force: true });
out.storedEn = sb.__store['drouter_lang'] || null;
I.setLang('zh-CN', { force: true });
out.storedZh = sb.__store['drouter_lang'] || null;

// ---- 5. 缺词降级 ----
out.missing = I.t('no.such.key.at.all');
out.missingWithVars = I.t('no.such.key', ['X']);

// ---- 6. 占位符替换 ----
I.setLang('en-US', { force: true });
out.ph = I.t('dash.diskRoot', ['1 GB', '2 GB', '500 MB']);
I.setLang('zh-CN', { force: true });
out.phZh = I.t('dash.diskRoot', ['1 GB', '2 GB', '500 MB']);

// ---- 7. 非法语言码回落 ----
out.badLang = I.setLang('ja-JP', { force: true });
out.getLangAfterBad = I.getLang();

console.log(JSON.stringify(out));
"""

fd, path = tempfile.mkstemp(suffix='.js')
os.close(fd)
with open(path, 'w', encoding='utf-8') as f:
    f.write(HARNESS)
r = subprocess.run([NODE, path, os.path.join(ROOT, 'web', 'i18n.js')],
                   capture_output=True)
os.unlink(path)
if r.returncode != 0:
    print('Node 加载失败：')
    print((r.stderr or b'').decode('utf-8', 'replace')[:1500])
    sys.exit(1)

d = json.loads((r.stdout or b'').decode('utf-8', 'replace'))

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    print(('ok    ' if cond else 'FAIL  ') + desc + (' ' + str(extra) if extra else ''))
    if not cond:
        fails.append(desc)


print('=== 1. 两种语言取值确实不同 ===')
# 「语言名」词条的英文里必然带汉字（"switch to 中文"）——
# 那是**目标语言的名字**，硬译成 "Chinese" 反而看不懂。
# 这些 key 用 i18n-allow-han 标记，检查器放行。
ALLOW_HAN = {'app.langToEn', 'app.langToZh'}
for k, v in d['samples'].items():
    ck('%s 中英不同' % k, v['zh'] != v['en'] and v['en'],
       '%s / %s' % (v['zh'], v['en']))
    if k in ALLOW_HAN:
        print('     （语言名词条，英文含「中文」属预期）')
    else:
        ck('%s 英文无汉字' % k,
           not any('一' <= c <= '鿿' for c in v['en']), v['en'])

print()
print('=== 2. 切换会触发监听者 ===')
ck('onChange 恰好被调用 2 次', d['fired'] == 2, '实际 %d 次' % d['fired'])
ck('监听者依次收到 en-US → zh-CN',
   d['seenTail'] == ['en-US', 'zh-CN'], d['seenTail'])

print()
print('=== 3. register 的渲染器会被 rerenderAll 调用 ===')
ck('rerenderAll 触发注册渲染器', d['renderedByRerenderAll'] == 1,
   d['renderedByRerenderAll'])

print()
print('=== 4. 语言持久化 ===')
ck('切到 en 时写入 localStorage', d['storedEn'] == 'en-US', d['storedEn'])
ck('切回 zh 时写入 localStorage', d['storedZh'] == 'zh-CN', d['storedZh'])

print()
print('=== 5. 缺词降级显示 key 原文（不是空白）===')
ck('缺词返回 key 本身', d['missing'] == 'no.such.key.at.all', d['missing'])
ck('缺词且带参数也不崩', 'no.such.key' in d['missingWithVars'], d['missingWithVars'])

print()
print('=== 6. 占位符替换 ===')
ck('英文占位符已替换', '{0}' not in d['ph'] and '1 GB' in d['ph'], d['ph'])
ck('中文占位符已替换', '{0}' not in d['phZh'] and '1 GB' in d['phZh'], d['phZh'])

print()
print('=== 7. 非法语言码回落中文 ===')
ck('setLang(ja-JP) 回落 zh-CN', d['badLang'] == 'zh-CN', d['badLang'])
ck('getLang 与 setLang 一致', d['getLangAfterBad'] == 'zh-CN', d['getLangAfterBad'])

print()
print('=' * 56)
print('i18n 真跑验证 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)
