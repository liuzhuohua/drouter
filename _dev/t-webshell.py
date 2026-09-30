#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 终端（#4 重做）的契约测试。

守住这几条最容易回退的约定：
  1) helper 只做 socket 客户端：未知 op / 非法用户 / 超长输入都要被挡住；
  2) shelld 的帧处理：UTF-8 多字节字符不能被分帧切成乱码；
  3) shelld 的会话编号必须唯一（曾用「时间戳+pid」，同秒连两次会互相顶掉）；
  4) shelld 的 op 分发：不存在会话要返回 NOSESS，满额返回 BUSY；
  5) web.py 里 read/write/resize 的超时必须短（这几个在打字路径上，慢了就卡手）。

Windows 上没有 pty 模块，所以这里不 import 整个 shelld，只用 ast 抽函数片段。
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')

fails = 0


def chk(label, cond, extra=''):
    global fails
    if not fails and not cond:
        pass
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


def src_of(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


HELPER_SRC = src_of('backend/drouter-helper.py')
SHELD_SRC = src_of('backend/drouter-shelld.py')
WEB_SRC = src_of('backend/drouter-web.py')
APP_JS = src_of('web/app.js')
APP_CSS = src_of('web/app.css')


def pick(src, names):
    """按名字从源码里抽函数定义，返回可 exec 的代码块。"""
    tree = ast.parse(src)
    out, got = [], set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append(ast.get_source_segment(src, node))
            got.add(node.name)
    missing = set(names) - got
    assert not missing, '未能抽取：%s' % ', '.join(sorted(missing))
    return '\n\n'.join(out)


# =========================================================================
# 1. helper 的 act_webshell（socket 客户端）
# =========================================================================
NS = {}
exec('def ok(data=None, msg="ok", code="OK"):\n'
     '    return {"ok": True, "code": code, "msg_cn": msg, "data": data}\n', NS)
exec('def fail(msg, code="ERR", data=None):\n'
     '    return {"ok": False, "code": code, "msg_cn": msg, "data": data}\n', NS)

CALLS = []


def fake_shelld_call(req, timeout=25):
    CALLS.append((req, timeout))
    return {'ok': True, 'msg_cn': 'stub', 'data': {'sid': 'sh1-1'}}


def fake_log(*a, **k):
    pass


NS.update({'sh': lambda *a, **k: (0, '', ''), 'log': fake_log,
           '_shelld_call': fake_shelld_call,
           'SHELL_UNIT': 'drouter-shelld.service', 'time': __import__('time')})
exec(pick(HELPER_SRC, ['act_webshell']), NS)
act_webshell = NS['act_webshell']

r = act_webshell({'op': 'nonsense'})
chk('未知终端操作被拒绝', (not r['ok']) and r['code'] == 'BAD_OP', '→ ' + r['msg_cn'])

r = act_webshell({'op': 'connect', 'user': 'hacker'})
chk('非法登录身份被拒绝', (not r['ok']) and r['code'] == 'BAD_USER', '→ ' + r['msg_cn'])

for u in ('root', 'ajeef', 'drouter'):
    CALLS[:] = []
    r = act_webshell({'op': 'connect', 'user': u, 'cols': 120, 'rows': 40})
    chk('允许以 %s 身份开启终端' % u, r['ok'] and CALLS[0][0]['user'] == u)

CALLS[:] = []
r = act_webshell({'op': 'connect', 'user': 'root'})
chk('connect 会把 cols/rows 透传', CALLS[0][0].get('cols') == 100 and CALLS[0][0].get('rows') == 30,
    '→ cols=%s rows=%s' % (CALLS[0][0].get('cols'), CALLS[0][0].get('rows')))

r = act_webshell({'op': 'write', 'sid': 'x', 'data': 'A' * 70000})
chk('超长输入被拒绝', (not r['ok']) and r['code'] == 'TOO_LONG', '→ ' + r['msg_cn'])

CALLS[:] = []
r = act_webshell({'op': 'write', 'sid': 'x', 'data': 'ls\r'})
chk('正常输入放行', r['ok'] and CALLS[0][0]['data'] == 'ls\r')

# 真交互式 shell 不再做命令黑名单：子串匹配既拦不住也会误伤
# （表现为「终端突然打不出字」），所以这里反而要确认没有残留拦截。
chk('已移除危险命令黑名单', 'SHELL_BLOCK' not in HELPER_SRC)

# =========================================================================
# 2. shelld：UTF-8 分帧 / 会话编号 / op 分发
# =========================================================================
SNS = {}
exec(pick(SHELD_SRC, ['_utf8_tail']), SNS)
utf8_tail = SNS['_utf8_tail']

full = '中文abc'.encode('utf-8')
safe, tail = utf8_tail(full)
chk('完整 UTF-8 整段通过', safe == full and tail == b'', '→ tail=%r' % tail)

part = '中文'.encode('utf-8')[:4]        # 「中」3 字节 + 「文」的第 1 字节
safe, tail = utf8_tail(part)
chk('半个汉字被留在缓冲区', safe == '中'.encode('utf-8') and tail == part[3:],
    '→ safe=%r tail=%r' % (safe, tail))

safe, tail = utf8_tail(b'abc')
chk('纯 ASCII 不会被切', safe == b'abc' and tail == b'')
safe, tail = utf8_tail('中'.encode('utf-8'))
chk('单个汉字完整时通过', safe == '中'.encode('utf-8') and tail == b'')

# 会话编号：不能只用时间戳，秒内连两次会撞
chk('会话编号带自增序列', 'next(_seq)' in SHELD_SRC and 'itertools.count' in SHELD_SRC)

SNS2 = {'_SESSIONS': {}, 'MAX_SESSIONS': 8, 'log': fake_log, 'time': __import__('time'),
        'os': os, '_lock': __import__('threading').Lock(), '_reap_idle': lambda: None,
        'itertools': __import__('itertools')}
exec(pick(SHELD_SRC, ['handle']), SNS2)
handle = SNS2['handle']

r = handle({'op': 'read', 'sid': 'nope'})
chk('读不存在的会话返回 NOSESS', (not r['ok']) and r['code'] == 'NOSESS')
r = handle({'op': 'write', 'sid': 'nope', 'data': 'x'})
chk('写不存在的会话返回 NOSESS', (not r['ok']) and r['code'] == 'NOSESS')
r = handle({'op': 'resize', 'sid': 'nope'})
chk('改尺寸不存在的会话返回 NOSESS', (not r['ok']) and r['code'] == 'NOSESS')
r = handle({'op': 'whatever'})
chk('未知操作返回 BAD_OP', (not r['ok']) and r['code'] == 'BAD_OP')
r = handle({'op': 'sessions'})
chk('sessions 返回列表', r['ok'] and isinstance(r['data'], list))

# 满额保护：8 个假会话后再 connect 必须被拒（否则小内存机器上能开出一片 shell）
class _Fake(object):
    created = 0.0
    last = 0.0
    alive = True


SNS2['_SESSIONS'] = {('sh%d' % i): _Fake() for i in range(8)}
r = handle({'op': 'connect', 'user': 'root'})
chk('会话数达上限被拒绝', (not r['ok']) and r['code'] == 'BUSY', '→ ' + r['msg_cn'])

# =========================================================================
# 3. web.py：超时分级
# =========================================================================
# read 刻意用较长超时：它走长轮询，服务端在无输出时会挂起最多 5 秒等数据，
# 客户端超时必须大于这个值，否则每次都会提前断开、前端退化成空转轮询。
chk('write/resize 走短超时分支', "sub in ('read', 'write', 'resize')" in WEB_SRC
    and re.search(r"tmo\s*=\s*20\s+if\s+sub\s*==\s*'read'\s+else\s+15", WEB_SRC) is not None)
chk('read 用长轮询超时（>服务端 5s 挂起）',
    re.search(r"tmo\s*=\s*20\s+if\s+sub\s*==\s*'read'", WEB_SRC) is not None
    and 'timeout=tmo' in WEB_SRC)
chk('connect 用较长超时', 'timeout=40' in WEB_SRC)
chk('终端接口要求登录', WEB_SRC.count("if not self.auth()") > 0)

# =========================================================================
# 4. 前端：真终端的关键契约
# =========================================================================
chk('前端不再有单条命令输入框', 'ws-cmd' not in APP_JS and 'wsTermWrite' not in APP_JS)
chk('前端不再保留 SSH 主机/端口/密码表单',
    'ws-host' not in APP_JS and 'ws-port' not in APP_JS and 'ws-pass' not in APP_JS)
for fn in ('wsFeed', 'wsCsi', 'wsSgr', 'wsRenderScreen', 'wsKeyDown', 'wsPump',
           'wsMeasure', 'wsResizeGrid', 'wsScrollbackPush'):
    chk('前端定义了 %s' % fn, ('function %s(' % fn) in APP_JS)
for cls in ('.tl{', '.tcur{', '.ws-capture{'):
    chk('样式含 %s' % cls.rstrip('{'), cls in APP_CSS)
chk('终端区禁止自动折行（否则行列对不上）', 'white-space:pre;word-break:normal' in APP_CSS)
chk('Enter 映射为回车', "Enter: '\\r'" in APP_JS)
chk('Backspace 映射为 DEL', "Backspace: '\\x7f'" in APP_JS)
chk('Ctrl+C 走控制码', "k.charCodeAt(0) - 96" in APP_JS)
chk('有选中文字时 Ctrl+C 不中断命令', "k === 'c' && wsSelection()" in APP_JS)
chk('粘贴不会被当成控制码吞掉', "k === 'v') return;" in APP_JS)

# =========================================================================
# 5. 前端终端外观：无滚动条 / 跟随最新 / 清屏按钮
# =========================================================================
# 需求：右侧不要那根滚动条，长回显自动跟随显示，另给一个小的清屏按钮。
# 做法是「隐藏滚动条 + 继续自动滚到底」，而不是彻底禁掉滚动 ——
# 禁掉的话用户就再也翻不了历史了，属于把功能改没了。
chk('隐藏了 WebKit 滚动条', '::-webkit-scrollbar' in APP_CSS and 'display:none' in APP_CSS)
chk('隐藏了 Firefox 滚动条', 'scrollbar-width:none' in APP_CSS)
chk('滚动能力保留（不是把 overflow 关掉）',
    'overflow-y:auto' in APP_CSS and 'overflow-y:hidden' not in APP_CSS)
chk('终端高度随视口自适应', 'clamp(' in APP_CSS and 'vh' in APP_CSS)
chk('栏内小按钮样式存在', '.term-btn{' in APP_CSS)
chk('标题栏有清屏按钮', 'id="ws-clearbar"' in APP_JS)
chk('标题栏有回到底部按钮', 'id="ws-tobottom"' in APP_JS)
chk('定义了 wsSyncTailBtn', 'function wsSyncTailBtn(' in APP_JS)
chk('滚动时同步尾部按钮', "addEventListener('scroll', wsSyncTailBtn" in APP_JS)

# 阈值只能有一份：wsAutoScroll（要不要继续跟随）和 wsSyncTailBtn（要不要显示
# 按钮）若各写各的，就会出现「按钮没出现、输出也不跟着滚了」的死角。
chk('跟随阈值只有一个定义', APP_JS.count('const WS_TAIL_GAP') == 1)
# pick() 走 ast，只能喂 Python 源码（app.js 里带 em dash，会解析失败）。
# 这里要的是「函数体」这段文本，直接按偏移切片即可。
_i = APP_JS.find('function wsAutoScroll(')
chk('wsAutoScroll 存在', _i >= 0)
_autos_src = APP_JS[_i:_i + 700] if _i >= 0 else ''
chk('wsAutoScroll 用同一个阈值', 'WS_TAIL_GAP' in _autos_src and '< 40' not in _autos_src)

# 历史遗留：旧的一套终端样式（.tdot/.tt/.term-in/.tk-*）已被完全取代，
# 留在文件里会让人改错地方。加了新样式后也不要把它带回来。
# 匹配带 `{` 的选择器而不是裸类名 —— 上面那段说明注释里就写着这些名字，
# 用裸子串会命中注释，得到「明明删干净了却报失败」的假警报。
for dead in ('.tdot{', ' .tt{', '.term-in{', '.tk-dir{', '.tk-cmd{', '.tk-prompt{'):
    chk('已清掉失效样式 %s' % dead.rstrip('{').strip(), dead not in APP_CSS)
# 同一个选择器只允许出现一次。之前 .term-wrap / .term-body 各有两条声明
# （后一条只是补一个属性），改样式时极易只改到其中一条。
for sel in ('.term-wrap{', '.term-body{', '.term-bar{'):
    chk('%s 只定义一次' % sel.rstrip('{'), APP_CSS.count(sel) == 1)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)
