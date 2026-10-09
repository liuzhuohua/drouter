#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块健康总览（#5）的静态守卫。

守的是「三张表必须对齐」这件事 —— 后端 read:status 只回**稳定枚举**，
所有展示文案都在前端 i18n 里；一旦后端加了新模块/新状态码而前端没补词条，
英文界面就会露出 key 原文（如 st.k.xxx），而**没有**这条守卫的话，
t-en-render 也发现不了（它只看「有没有中文字符」，key 原文是 ASCII）。
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ok    %s' % name)
    else:
        FAIL += 1
        print('  FAIL  %s %s' % (name, ('→ ' + str(extra)) if extra else ''))


def rd(rel):
    return io.open(os.path.join(ROOT, rel), encoding='utf-8').read()




HELPER = rd('backend/drouter-helper.py')
WEBPY = rd('backend/drouter-web.py')
APP = rd('web/app.js')
I18N = rd('web/i18n.js')
FIX = rd('_real_fixtures.js')

def st_block(name):
    """取 i18n.js 里 st: { <name>: { ... } } 这一层的文本。

    ⚠️ 必须按子块取，不能用整文件正则：`system` 既是 k 组的模块名，
    也是 h 组的提示语键。整文件搜索时删掉 k.system，h.system 会顶上，
    判据照样变绿（实测踩过）。
    """
    i = I18N.find('\n    st: {\n')
    if i < 0:
        return ''
    j = I18N.find('\n      %s: {\n' % name, i)
    if j < 0:
        return ''
    k = I18N.find('\n      },\n', j + 1)
    return I18N[j:k] if k > 0 else ''


ST_K = st_block('k')
ST_H = st_block('h')
ST_C = st_block('c')
ST_M = st_block('m')

# ---------- 1. 后端 ----------
m = re.search(r'\ndef read_status\(_\):\n([\s\S]*?)\n\ndef ', HELPER)
chk('helper 里有 read_status', bool(m))
chk('i18n 里 st.k / st.h / st.c / st.m 四个子块都取到了',
    all(len(x) > 200 for x in (ST_K, ST_H, ST_C, ST_M)),
    'len=%s' % [len(x) for x in (ST_K, ST_H, ST_C, ST_M)])
body = m.group(1) if m else ''
chk("ACTIONS 注册了 read:status", "'read:status': read_status," in HELPER)
chk("web 路由注册了 /api/status", "'/api/status': 'read:status'," in WEBPY)

# 去掉注释与 docstring 后再查中文：read_status 只该回枚举与数值
stripped = re.sub(r'""".*?"""', '', body, flags=re.S)
stripped = re.sub(r'#[^\n]*', '', stripped)
# log(...) 是**内部日志行**（进系统日志，不下发给前端），中文是允许的；
# 不排掉的话这条判据会被「健康总览 X 判定失败」这种日志串误伤。
stripped = re.sub(r"log\((?:[^()]|\([^()]*\))*\)", '', stripped)
cjk = re.findall(r'[\u4e00-\u9fff]', stripped)
chk('read_status 不下发任何中文文案', not cjk, ''.join(cjk[:20]))

# 轮询安全：必须有结果缓存 + TTL
chk('有结果缓存（_STATUS_CACHE）', '_STATUS_CACHE' in HELPER)
tm = re.search(r'_STATUS_TTL\s*=\s*(\d+)', HELPER)
chk('_STATUS_TTL 是正数', bool(tm) and int(tm.group(1)) > 0,
    tm.group(1) if tm else 'missing')
# ⚠️ read_metrics 内含 ping 测 RTT，实测 1.2s —— 轮询接口里绝不能出现它
chk('read_status 不调 read_metrics（含 1.2s 的 ping 探测）',
    'read_metrics(' not in stripped)
chk('资源采集走零 fork 的 _st_resources', '_st_resources()' in stripped)

# ---------- 2. 模块键 → i18n st.k.* / st.h.* ----------
keys = re.findall(r"_run\('([a-z0-9_]+)'", body)
chk('read_status 至少覆盖 15 个模块', len(keys) >= 15, '实际 %d' % len(keys))
miss_k = [k for k in keys if ("      %s:" % k) not in I18N or
          ("st.k." + k) not in APP]
bad_k = []
for k in keys:
    if not re.search(r"\n        %s: *\{ zh: '[^']*', en: '[^']*' \}"
                     % re.escape(k), ST_K):
        bad_k.append(k)
chk('每个模块键都有 st.k.<键> 词条', not bad_k, str(bad_k))

# st.h.<键>：提示语。允许缺失（前端会留空），但至少覆盖 80%
hits = [k for k in keys
        if re.search(r"\n        %s: *\{ zh: '[^']*', en: '[^']*' \}" % re.escape(k), ST_H)]
chk('模块提示语 st.h.* 覆盖 >= 80%%', len(hits) >= len(keys) * 0.8,
    '%d/%d' % (len(hits), len(keys)))

# ---------- 3. 状态码 → i18n st.c.* ----------
codes = set()
codes |= set(re.findall(r"return '[a-z_]+', '([a-z_]+)'", body))
# ⚠️ 这个形状里**第一个**字符串是等级、第二个才是状态码：
#    lv, code = 'warn', 'noaddr'  → 码是 noaddr 而不是 warn。
codes |= set(re.findall(r"lv, code = '[a-z_]+', '([a-z_]+)'", body))
codes |= set(re.findall(r"it\['lv'\], it\['c'\] = '[a-z_]+', '([a-z_]+)'", body))
# ⚠️ 不要再加 `\['lv'\] = '(...)'` 或 `return \('(...)' if ..., '...'\)` 这类
#    取**第一个**字符串的模式 —— 第一个永远是等级，会把 'warn'/'err'
#    当成状态码，然后误报「缺 st.c.warn 词条」（实测踩过）。
codes |= set(re.findall(r"return \('[a-z_]+' if [^\n]*? else '[a-z_]+'\), "
                        r"\('([a-z_]+)' if [^\n]*? else '[a-z_]+'\)", body))
codes |= set(re.findall(r"\['c'\] = '([a-z_]+)'", body))
# _svc_lv 的返回
codes |= {'running', 'starting', 'failed', 'stopped', 'disabled', 'unknown'}
chk('识别到 >= 12 个状态码', len(codes) >= 12, '实际 %d' % len(codes))
miss_c = sorted(c for c in codes
                if not re.search(r"\n        %s: *\{ zh: '[^']*', en: '[^']*' \}"
                                 % re.escape(c), ST_C))
chk('每个状态码都有 st.c.<码> 词条', not miss_c, str(miss_c))

# ---------- 4. 指标名：真机 fixture 的键必须都能展示 ----------
mf = re.search(r'const REAL_STATUS = (\{[\s\S]*?\n\});', FIX)
chk('fixture 里有 REAL_STATUS', bool(mf))
if mf:
    data = json.loads(mf.group(1))
    real_keys = set()
    for it in data.get('items') or []:
        real_keys |= set((it.get('m') or {}).keys())
    chk('真机 payload 至少 20 个指标键', len(real_keys) >= 20, '实际 %d' % len(real_keys))

    stm = re.search(r'const ST_METRICS = \[([\s\S]*?)\n\];', APP)
    chk('app.js 有 ST_METRICS 表', bool(stm))
    listed = set(re.findall(r"\['([a-z0-9_]+)',", stm.group(1))) if stm else set()
    hidden = sorted(real_keys - listed)
    chk('真机所有指标键都在 ST_METRICS 里（不会静默不显示）', not hidden, str(hidden))
    miss_m = sorted(k for k in listed
                    if not re.search(r"\n        %s: *\{ zh: '[^']*', en: '[^']*' \}"
                                     % re.escape(k), ST_M))
    chk('ST_METRICS 每项都有 st.m.<名> 词条', not miss_m, str(miss_m))

# ---------- 5. 前端接线 ----------
chk('VIEWS 注册了 health', re.search(r'\n  health: viewHealth,', APP) is not None)
chk('导航里有健康总览入口', "{ k: 'health', n: T('st.nav')" in APP)
chk('HEALTH_TIMER 进 stopPageTimers（切页要停轮询）',
    'DK_TIMER, HEALTH_TIMER]' in APP and 'DK_TIMER = HEALTH_TIMER = null' in APP)
chk('顶栏状态灯挂进 boot()', 'initHealthLight();' in APP)
chk('本页状态灯容器存在（index.html）', 'id="page-health"' in rd('web/index.html'))
chk('本页灯在 go() 里同步', 'paintPageLight();     // 本页模块状态灯' in APP)

# 单位不能写中文（会原样漏进英文界面）
unit_cjk = re.findall(r"\['[a-z0-9_]+', ' ([^']*[\u4e00-\u9fff][^']*)'\]", APP)
chk('ST_METRICS 的单位不含中文', not unit_cjk, str(unit_cjk))

print('\n==== t-status: %d 条判据, %d 失败 ====' % (PASS + FAIL, FAIL))
if FAIL:
    print('STATUS_GUARD_FAIL')
    sys.exit(1)
print('STATUS_GUARD_OK')
