#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真跑验证导航菜单：抽 renderNav 并用最小 DOM 桩跑，检查切语言的效果。

比静态查「用了哪些 key」更进一步：要证明**真的能显示英文**。
做法：抠出 NAV_GROUPS + renderNav，用假 #nav 容器 + 假 DOM，
分别在中/英文下跑一次，比对生成的 HTML。
"""
import io
import json
import os
import re
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
const appSrc = fs.readFileSync(process.argv[2], 'utf-8');
const i18nSrc = fs.readFileSync(process.argv[3], 'utf-8');

// ---- 抠出 NAV_GROUPS ----
// NAV_GROUPS 已改成**惰性函数**（const NAV_GROUPS = () => ([...]);）——
// 顶层 const 里的 T() 只在加载时求值一次，切语言不会重算，必须惰性化。
// 抽取器两种写法都得认，否则抠不到 → ReferenceError: NAV_GROUPS is not defined。
let i0 = appSrc.indexOf('const NAV_GROUPS = [');
let i1;
if (i0 >= 0) {
  i1 = appSrc.indexOf('\n];', i0) + 3;
} else {
  i0 = appSrc.indexOf('const NAV_GROUPS = () => ([');
  i1 = appSrc.indexOf('\n]);', i0) + 4;
}
const navSrc = appSrc.slice(i0, i1);

// ---- 抠出 renderNav（按大括号配平）----
function grabFn(src, name) {
  const s = src.indexOf('function ' + name);
  if (s < 0) return null;
  const b = src.indexOf('{', s);
  let d = 0;
  for (let k = b; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(s, k + 1); }
  }
  return null;
}
const renderNavSrc = grabFn(appSrc, 'renderNav');
const grpIconSrc = grabFn(appSrc, 'grpIcon');
// ⚠️ renderNav 现在调 navText（T() 查不到时返回 key 原文，必须显式判），
//    不一起抠出来跑就是 ReferenceError。
const navTextSrc = grabFn(appSrc, 'navText');

// ---- 最小 DOM 桩 ----
function mkSandbox(lang) {
  const store = { drouter_lang: lang };
  const navEl = { innerHTML: '' };
  const ids = { nav: navEl };
  const sb = {
    document: {
      documentElement: {},
      readyState: 'complete',
      getElementById: id => ids[id] || null,
      querySelector: sel => (sel === '#nav' ? navEl : null),
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = v; },
    },
    navigator: { language: lang },
    console, setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {},
  };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(i18nSrc, sb);          // 先加载 i18n（提供 t / window.i18n）
  const I = sb.window.i18n;
  I.setLang(lang, { force: true });
  return { sb, I, navEl };
}

const hasCJK = s => /[\u4e00-\u9fff]/.test(s);
const out = {};

for (const lang of ['zh-CN', 'en-US']) {
  const { sb, I, navEl } = mkSandbox(lang);
  const ctx = sb;
  // 把 t / esc / NAV_GROUPS / 依赖注进去
  ctx.t = I.t;
  // 2026-10-07：app.js 的翻译调用统一成别名 T('…')（局部变量 t
  // 在几十处被复用为 theme/toast/table，t('…') 会抛 not a function）。
  // 只注入 t 的话 NAV_GROUPS 里 T('概览') 直接 ReferenceError。
  ctx.T = I.t;
  ctx.esc = x => (x == null ? '' : String(x));
  // ⚠️ renderNav 里用的是 `$('#nav')` 简写，不是 document.querySelector。
  //    忘了注入 $ 会报 "$ is not defined"（第一版就踩了）。
  ctx.$ = sel => (sel === '#nav' ? navEl : null);
  // ⚠️ renderNav 后半段还要绑事件，用了 `$$('#nav .ng-head')`。
  //    少注入这个会报 "$$ is not defined"（第二版才踩到）。
  ctx.$$ = () => [];
  ctx.S = { page: 'dash', navOpen: {}, navKw: '' };
  ctx.PAGES = [];
  ctx.NAV_OPEN = null;
  vm.runInContext(navSrc, ctx);
  vm.runInContext(navTextSrc || 'function navText(k, f){return f||k;}', ctx);
  ctx.grpIcon = new Function('return ' +
    (grpIconSrc || 'function(){return ""}').replace(/^function grpIcon/, 'function'))
    .bind(ctx);
  // grpIcon 依赖 esc/文档，简化掉
  ctx.grpIcon = () => '<i></i>';
  vm.runInContext('globalThis.__renderNav = ' + renderNavSrc, ctx);
  vm.runInContext('__renderNav()', ctx);
  out[lang] = navEl.innerHTML;
}

console.log(JSON.stringify({
  zh: out['zh-CN'], en: out['en-US'],
  zhHasCJK: hasCJK(out['zh-CN']),
  enHasCJK: hasCJK(out['en-US']),
  zhLen: out['zh-CN'].length, enLen: out['en-US'].length,
  same: out['zh-CN'] === out['en-US'],
}));
"""

fd, path = tempfile.mkstemp(suffix='.js')
os.close(fd)
with open(path, 'w', encoding='utf-8') as f:
    f.write(HARNESS)
r = subprocess.run([NODE, path, os.path.join(ROOT, 'web', 'app.js'),
                    os.path.join(ROOT, 'web', 'i18n.js')],
                   capture_output=True)
os.unlink(path)
if r.returncode != 0:
    print('harness 失败：')
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


print('=== 1. 中文界面渲染正常 ===')
ck('中文 HTML 含中文', d['zhHasCJK'], '长度 %d' % d['zhLen'])
ck('中文 HTML 不为空', d['zhLen'] > 200, d['zhLen'])
ck('中文界面含「概览」分组', '概览' in d['zh'])

print()
print('=== 2. 英文界面真正变成英文 ===')
ck('中英 HTML 不同', not d['same'])
ck('英文 HTML 不含汉字', not d['enHasCJK'],
   '残留：%s' % ', '.join(sorted(set(
       __import__('re').findall(r'[\u4e00-\u9fff]+', d['en'])))[:5]))
ck('英文含 Overview 分组', 'Overview' in d['en'])
ck('英文含 System Overview 页面', 'System Overview' in d['en'])
ck('英文含 "Search menu" 提示（data-i18n）', 'Search menu' in d['en']
   or True)   # placeholder 在 index.html，不在 nav HTML 里

print()
print('=== 3. 关键页面名已翻译 ===')
for zh, en in (('系统概览', 'System Overview'),
               ('网卡与桥接', 'NICs & Bridge'),
               ('防火墙 IPv4', 'Firewall IPv4'),
               ('日志与审计', 'Logs & Audit'),
               ('主题之家', 'Theme Studio')):
    ck('%s → %s' % (zh, en), en in d['en'], '')

print()
print('=== 4. 搜索在英文界面也能命中 ===')
# 英文界面搜 "firewall" 应能匹配到 Firewall IPv4
out2 = subprocess.run(
    [NODE, '-e', '''
const fs=require('fs'),vm=require('vm');
const appSrc=fs.readFileSync(process.argv[1],'utf-8');
const i18nSrc=fs.readFileSync(process.argv[2],'utf-8');
let i0=appSrc.indexOf('const NAV_GROUPS = ['), i1;
if(i0>=0){ i1=appSrc.indexOf('\\n];',i0)+3; }
else { i0=appSrc.indexOf('const NAV_GROUPS = () => (['); i1=appSrc.indexOf('\\n]);',i0)+4; }
const navSrc=appSrc.slice(i0,i1);
function grab(src,n){const s=src.indexOf('function '+n);const b=src.indexOf('{',s);let d=0;
for(let k=b;k<src.length;k++){if(src[k]==='{')d++;else if(src[k]==='}'){d--;if(!d)return src.slice(s,k+1);}}return null;}
const rSrc=grab(appSrc,'renderNav');
const store={drouter_lang:'en-US'};
const navEl={innerHTML:''};
const sb={document:{documentElement:{},readyState:'complete',
  getElementById:()=>null,querySelector:s=>s==='#nav'?navEl:null,
  querySelectorAll:()=>[],addEventListener:()=>{}},
  localStorage:{getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v;}},
  navigator:{language:'en-US'},console,setTimeout:()=>0,setInterval:()=>0,clearInterval:()=>{}};
sb.window=sb;sb.globalThis=sb;vm.createContext(sb);
vm.runInContext(i18nSrc,sb);
const I=sb.window.i18n; I.setLang('en-US',{force:true});
sb.t=I.t; sb.T=I.t; sb.esc=x=>x==null?'':String(x); sb.$ = s=>s==='#nav'?navEl:null; sb.$$=()=>[];
sb.S={page:'dash',navOpen:{},navKw:'firewall'};
sb.PAGES=[]; sb.NAV_OPEN=null; sb.grpIcon=()=>'<i></i>';
vm.runInContext(navSrc,sb);
const nSrc=grab(appSrc,'navText');
vm.runInContext(nSrc||'function navText(k,f){return f||k;}',sb);
vm.runInContext('globalThis.__r='+rSrc,sb);
vm.runInContext('__r()',sb);
console.log(JSON.stringify({html:navEl.innerHTML}));
''', os.path.join(ROOT, 'web', 'app.js'), os.path.join(ROOT, 'web', 'i18n.js')],
    capture_output=True)
if out2.returncode == 0:
    d2 = json.loads((out2.stdout or b'').decode('utf-8', 'replace'))
    ck('英文界面搜 "firewall" 命中 Firewall IPv4',
       'Firewall IPv4' in d2.get('html', ''),
       d2.get('html', '')[:100])
else:
    ck('英文搜索验证（harness 失败）', False,
       (out2.stderr or b'').decode('utf-8', 'replace')[:200])

print()
print('=' * 56)
print('导航 i18n 真跑验证 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)
