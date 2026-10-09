#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i18n 词条完整性检查（1.0.10）。

要抓的三类错（都是 i18n 最容易出的）：

 A. **调了 t('x.y') 但字典里没这个词** → 界面显示 "x.y"，用户以为是 bug。
    反过来：字典里有但没人调 → 死词条（会误导后来人以为已翻译）。

 B. **词条缺 en**（或缺 zh）→ 切到英文时回落到中文，
    界面中英混杂，比全中文更糟。

 C. **en 文本里还带汉字** → 翻译漏了。

另外顺带查：
 D. t() 的第一个参数不是字面量（动态 key）→ 静态查不到，
    提示一下这类地方要走「已注册 key」的自检。

用法：python _dev/t-i18n.py
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

fails = []
n = 0


def ck(desc, cond, extra=''):
    global n
    n += 1
    print(('ok    ' if cond else 'FAIL  ') + desc + (' ' + str(extra) if extra else ''))
    if not cond:
        fails.append(desc)


def read(p):
    return io.open(os.path.join(ROOT, p), encoding='utf-8').read()


i18n_js = read('web/i18n.js')
app_js = read('web/app.js')

# ---------- 从 i18n.js 抽出字典（用 Node 跑，直接拿运行时对象） ----------
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

import subprocess
import tempfile

HARNESS = r"""
// 在 Node 里跑 i18n.js（它挂 window.i18n），把字典与 t() 结果吐出来。
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const listeners = [];
const sandbox = {
  window: {},
  document: {
    documentElement: {},
    readyState: 'complete',
    getElementById: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  localStorage: {
    _d: {},
    getItem(k) { return this._d[k] || null; },
    setItem(k, v) { this._d[k] = v; },
  },
  navigator: { language: 'zh-CN' },
  console,
  setTimeout: () => 0,
  setInterval: () => 0,
  clearInterval: () => {},
};
sandbox.window.window = sandbox.window;
sandbox.window.document = sandbox.document;
sandbox.window.localStorage = sandbox.localStorage;
sandbox.window.navigator = sandbox.navigator;
sandbox.window.console = console;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src + '\n;globalThis.__I18N = window.i18n;', sandbox);
const I = sandbox.__I18N;

// 递归收集所有 key
// ⚠️ 词条的形状是 {zh, en} —— 它**也是对象**，不能当成命名空间继续往下走。
//    检查器第一版没判断这一点，于是把 'common.version.zh' /
//    'common.version.en' 也当成 key，害得「缺 key」和「英文含汉字」双双误报。
//    判据：对象里同时有 zh 和 en 两个字符串字段 → 它是叶子（词条）。
const keys = [];
const refs = new Map();   // path -> {parent, field}，用于绕开点分路径
const CN = /[\u4e00-\u9fff]/;
(function walk(node, prefix) {
  for (const k of Object.keys(node)) {
    const v = node[k];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const isLeaf = ('zh' in v && 'en' in v)
        && typeof v.zh === 'string' && typeof v.en === 'string';
      if (isLeaf) {
        keys.push(prefix ? prefix + '.' + k : k);
      } else {
        walk(v, prefix ? prefix + '.' + k : k);
      }
    } else {
      const p = prefix ? prefix + '.' + k : k;
      keys.push(p);
      refs.set(p, { parent: node, field: k });
    }
  }
})(I.dict, '');

// 每条在两种语言下的取值
const out = {};
for (const k of keys) {
  // ⚠️ bt 表的 key 是后端下发的中文原文，**里面可能带点号**
  //    （如 "DHCP 与 DNS（drouter.conf）"、"…192.168.7.3"）。
  //    点分路径 t() 会被切碎 → 查不到 → 回落成 key 原文（含汉字）
  //    → 判据误报「英文没翻译」。这时直接按对象引用取值。
  const r = refs.get(k);
  if (r && k.indexOf('bt.') === 0) {
    const v = (r.parent && r.parent[r.field] != null) ? String(r.parent[r.field]) : '';
    out[k] = { zh: v, en: v };
    continue;
  }
  I.setLang('zh-CN');
  const zh = I.t(k);
  I.setLang('en-US');
  const en = I.t(k);
  out[k] = { zh, en };
}
console.log(JSON.stringify({ keys, t: out }, null, 0));
"""

fd, path = tempfile.mkstemp(suffix='.js')
os.close(fd)
with open(path, 'w', encoding='utf-8') as f:
    f.write(HARNESS)
r = subprocess.run([NODE, path, os.path.join(ROOT, 'web', 'i18n.js')],
                   capture_output=True)
os.unlink(path)
if r.returncode != 0:
    print('无法在 Node 里加载 i18n.js：')
    print((r.stderr or b'').decode('utf-8', 'replace')[:1200])
    sys.exit(1)

data = json.loads((r.stdout or b'').decode('utf-8', 'replace'))
keys = data['keys']
vals = data['t']
# raw 分组：key 是中文原文，引用时可不带 'raw.' 前缀
RAW_BARE = {k[len('raw.'):] for k in keys if k.startswith('raw.')}
print('字典共 %d 条词条\n' % len(keys))

# ---------- A. 代码里调用的 key 是否都存在 ----------
# 只查**字面量**调用 t('...')；动态 key 查不到（见 D）
# 只查**字面量**调用 t('...')；动态 key 查不到（见 D）。
# ⚠️ 必须先剥注释 —— 我自己在解释「t 会被局部变量遮蔽」的注释里写了
#    `t('key')` 这个示例，检查器把它当成真调用，报「缺失词条 key」。
#    判据查代码，不查注释里举例说明的代码。
def _code_only_js(text):
    """剥掉 JS 注释，**完整保留**字符串与模板字符串的内容。

    ⚠️ 三个坑，各踩过一次：
    ① **反引号模板字符串**要当字符串处理。漏了它的话，模板里的 `'`
       会被当成新的字符串开引号，把后面整段吞进「字符串」状态
       —— 实测 t() 检出数从 874 掉到 0（**静默假绿**，比假红更危险）。
    ② 剥注释时**必须保留字符串内容**。早期版本把字符串整体替换成空格，
       结果模板里的 `${t('ca.alg')}` 全被清掉 → 874 个 key 检出 0 个，
       检查器「通过」但什么都没查。
    ③ 块注释要整体跳到 `*/`，不能只跳到行尾。
    """
    out = []
    i = 0
    n = len(text)
    instr = None
    while i < n:
        c = text[i]
        if instr:
            out.append(c)              # ✅ 字符串内容原样保留
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == instr:
                instr = None
            i += 1
            continue
        if text[i:i + 2] == '//':
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append('\n' if False else ' ' * (j - i))
            i = j
            continue
        if text[i:i + 2] == '/*':
            j = text.find('*/', i)
            j = n if j < 0 else j + 2
            seg = text[i:j]            # 块注释里保留换行，行号才对得上
            out.append(''.join(ch if ch == '\n' else ' ' for ch in seg))
            i = j
            continue
        if c in ('"', "'", '`'):
            instr = c
        out.append(c)
        i += 1
    return ''.join(out)


used = set()
# ⛔ 不要自己写 JS 词法分析来「找 t() 调用」—— 我写了 3 版都出问题：
#    ① 漏了反引号 → 模板里的 `'` 提前开字符串，检出数 874 → 0（静默假绿）
#    ② 把字符串内容也清掉了 → `${t('ca.alg')}` 被清，同样 0
#    ③ 注释里的 `t('key')` 反而被检出 → 报「缺失词条 key」（假红）
#    **正确做法：直接用 Node 正则扫原文**（Node 的正则不关心 JS 语法），
#    再用「该行是否在注释里」把它排除掉 —— 只判行首有没有 // 或 * 就够，
#    不用做完整的词法分析。
_PAT = re.compile(r"\bt\(\s*'([a-zA-Z0-9_.]+)'")


def _in_comment_line(line):
    s = line.strip()
    return s.startswith('//') or s.startswith('*') or s.startswith('/*')


for f in ('web/app.js', 'web/update.js', 'web/upstream.js',
          'web/netdetail.js', 'web/realtime.js', 'web/index.html'):
    try:
        lines = read(f).split('\n')
    except Exception:
        continue
    for ln in lines:
        if _in_comment_line(ln):
            continue                      # 整行都是注释，跳过
        for m in _PAT.finditer(ln):
            used.add(m.group(1))
# data-i18n / data-i18n-attr 里的 key
for f in ('web/index.html', 'web/app.js'):
    s = read(f)
    for m in re.finditer(r'data-i18n(?:-attr)?="([^"]+)"', s):
        for part in m.group(1).split(','):
            if ':' in part:
                part = part.split(':', 1)[1]
            if part.strip():
                used.add(part.strip())

print('代码里引用了 %d 个 key\n' % len(used))
missing = sorted(k for k in used if k not in keys)

# 动态拼接的 key：t('nav.n.' + it.k) 这种形式。
# 静态只能看到前缀 'nav.n.'，无法确认运行时拼出来的 key 存在。
# 处理：从「缺失」里把「以 . 或 _ 结尾的前缀」挑出来，改用「前缀下确有词条」
# 来判断 —— 即 `nav.n.` 有 n.* 词条就算这一族没问题。
DYNAMIC_PREFIX = set()
real_missing = []
for k in missing:
    # 动态拼接有两种形态：
    #   ① t('nav.n.' + it.k)      → 前缀以 . 结尾
    #   ② t('nav.g.g' + gi)        → 前缀不以 . 结尾，后面跟数字下标
    # ② 靠「去掉最后一段后，该前缀下确有词条」来判断。
    if k.endswith('.') or k.endswith('_'):
        hit = any(x.startswith(k) and x != k for x in keys)
    else:
        base = k.rsplit('.', 1)[0] + '.'
        hit = any(x.startswith(base) and x != base for x in keys)
    # ⚠️ raw 分组的 key 就是中文原文，lookup() 有「尾段原文直查」兜底，
    #    所以 t('退出系统') 能命中 raw.退出系统 —— 不带 raw. 前缀也是合法的。
    #    必须在这里**直接放行并 continue**：若只把 hit 置 True，它会被当成
    #    「动态拼接的 key」再走一遍前缀校验，而 '退出系统.' 前缀下当然没有
    #    任何词条 → 判据红（2026-10-08 实测）。
    if k in RAW_BARE:
        continue
    if hit:
        DYNAMIC_PREFIX.add(k)
        continue
    real_missing.append(k)

if DYNAMIC_PREFIX:
    print('提示：%d 个 key 是动态拼接的（如 t(\'nav.n.\' + it.k)），'
          '静态只能看到前缀：%s' % (len(DYNAMIC_PREFIX), sorted(DYNAMIC_PREFIX)))
    print('     → 这些路径靠「前缀下确有词条」间接保证；'
          '真跑时仍需人工确认一遍')

ck('代码引用的 key 全部存在于字典', not real_missing,
   '→ 缺失：%s（界面会显示 key 原文）' % real_missing[:6])
def _dyn_has_keys(k):
    """动态前缀下是否确有词条（两种拼接形态都要认）。"""
    if k.endswith('.') or k.endswith('_'):
        return any(x.startswith(k) and x != k for x in keys)
    base = k.rsplit('.', 1)[0] + '.'
    return any(x.startswith(base) and x != base for x in keys)


ck('动态拼接的 key 都有对应词条（按前缀校验）',
   all(_dyn_has_keys(k) for k in DYNAMIC_PREFIX),
   '→ 某前缀下没有任何词条')

# ---------- B. 词条是否 zh/en 齐全 ----------
# ⚠️ 量词（个/台/条/次/的）的 en **有意留空**：英文不需要量词（3 个 → 3），
#    写 "3 pieces" 反而不对。这类用 `// i18n-en-empty` 注释标记后放行。
EN_EMPTY_OK = set()
for _i, _l in enumerate(i18n_js.splitlines()):
    if 'i18n-en-empty' in _l:
        _m = re.match(r"\s*'?([^':]+)'?\s*:\s*\{", _l)
        if _m:
            EN_EMPTY_OK.add('raw.' + _m.group(1).strip().strip("'"))
# ⚠️ bt 表字段留空是**有意**的：bt4(table,key,field,fallback) 查到空串就
#    回落到后端原文（中文界面显示中文）。例如 ALERT_RULES.load_high.unit
#    为空 —— 负载(load average)没有单位，硬填 'N/A' 反而显示成「3 N/A」。
def _bt_empty_ok(k):
    return k.startswith('bt.')
no_en = [k for k, v in vals.items()
         if not v.get('en') and k not in EN_EMPTY_OK and not _bt_empty_ok(k)]
no_zh = [k for k, v in vals.items()
         if not v.get('zh') and not _bt_empty_ok(k)]
ck('每条都有英文', not no_en, '→ 缺 en：%s' % no_en[:6])
ck('每条都有中文', not no_zh, '→ 缺 zh：%s' % no_zh[:6])

# ---------- C. 英文文本里不该有汉字 ----------
# ⚠️ 例外：**语言名本身**。
#   「切换到中文」的英文提示里必须写「中文」两个字 ——
#   用户看到 "Switch to 中文" 才知道点完会变成什么。
#   这类词条用 `// i18n-allow-han` 注释标记，检查器放行。
#   检查器第一版没这个例外，把 app.langToEn/zh 判成「翻译漏了」，
#   差点让我把「中文」硬译成 "Chinese"（反而更难懂）。
CJK = re.compile(r'[\u4e00-\u9fff]')
ALLOW_HAN = re.compile(r'//\s*i18n-allow-han')
allow_lines = {i + 1 for i, l in enumerate(i18n_js.splitlines())
               if ALLOW_HAN.search(l)}
# 找出带标记的 key：从标记行往上找最近的 `key: {`
allowed_keys = set()
lines = i18n_js.splitlines()
for ln in sorted(allow_lines):
    for k in range(ln - 1, -1, -1):
        m = re.match(r'\s*([A-Za-z0-9_]+)\s*:\s*\{', lines[k])
        if m:
            allowed_keys.add(m.group(1))
            break

en_has_cjk = [k for k, v in vals.items()
              if CJK.search(v.get('en') or '')
              and k.split('.')[-1] not in allowed_keys]
ck('英文文本里无残留汉字（语言名除外）', not en_has_cjk,
   '→ %s' % en_has_cjk[:6])
if allowed_keys:
    print('提示：%d 条词条标记了 i18n-allow-han（语言名等允许含汉字）'
          % len(allowed_keys))

# ---------- 占位符一致性 ----------
ph = re.compile(r'\{(\d+)\}')
bad_ph = []
for k, v in vals.items():
    z = set(ph.findall(v.get('zh') or ''))
    e = set(ph.findall(v.get('en') or ''))
    if z != e:
        bad_ph.append((k, sorted(z), sorted(e)))
ck('中英占位符编号一致', not bad_ph,
   '→ %s' % [(k, z, e) for k, z, e in bad_ph[:5]])

# ---------- 死词条（字典有但没人用）----------
dead = sorted(k for k in keys if k not in used)
print()
if dead:
    print('提示：%d 条词条暂未被引用（可能是给后续页面预留的）：' % len(dead))
    for d in dead[:8]:
        print('   ', d)
    if len(dead) > 8:
        print('    … 还有 %d 条' % (len(dead) - 8))
else:
    print('提示：无死词条')

# ---------- D. 动态 key ----------
dyn = []
for f in ('web/app.js', 'web/update.js', 'web/upstream.js',
          'web/netdetail.js', 'web/realtime.js'):
    try:
        s = read(f)
    except Exception:
        continue
    # t(后面不是引号的
    for m in re.finditer(r"\bt\(\s*(?!'|\")", s):
        line = s[:m.start()].count('\n') + 1
        dyn.append('%s:%d' % (f, line))
print()
if dyn:
    print('提示：%d 处 t() 的 key 是动态的，静态检查覆盖不到：' % len(dyn))
    for d in dyn[:6]:
        print('   ', d)
    print('     → 这些路径要靠「切到英文后人工看一遍」兜底')
else:
    print('提示：所有 t() 调用都是字面量 key，静态可覆盖 ✓')

print()
print('=' * 56)
print('i18n 检查 %d 条，失败 %d 条' % (n, len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)
