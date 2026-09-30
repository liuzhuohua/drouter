#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真·公网 IP 判定：静态契约 + 判定逻辑单测。

盯两件事：
  ① 判定分级是真的按「入向实测」拍板，不是只看地址段 ——
     用户的 101.70.131.227 就栽在这：地址是公网段，但从 VPS ping 不通。
  ② 后端动作 / 路由 / 前端三处接线齐全。
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
APP = os.path.join(ROOT, 'web', 'app.js')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


HELPER_SRC = io.open(HELPER, encoding='utf-8').read()
WEB_SRC = io.open(WEB, encoding='utf-8').read()
APP_SRC = io.open(APP, encoding='utf-8').read()


def main():
    print('--- 后端动作 ---')
    chk('定义了 act_pubip', 'def act_pubip(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'pubip':\s*act_pubip,", HELPER_SRC, re.M) is not None)
    for op in ('check', 'probe_start', 'probe_status', 'probe_stop'):
        chk('支持 op=%s' % op, ("op == '%s'" % op) in HELPER_SRC
            or ("op = (p.get('op') or 'check')" in HELPER_SRC and op == 'check'))

    print('\n--- 判定分级 ---')
    tree = ast.parse(HELPER_SRC)
    verdicts = None
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == 'PUBIP_VERDICT':
                    ns = {}
                    mod = ast.Module(body=[ast.Assign(
                        targets=[ast.Name('PUBIP_VERDICT', ast.Store())],
                        value=n.value)], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, '<V>', 'exec'), ns)
                    verdicts = ns['PUBIP_VERDICT']
    chk('PUBIP_VERDICT 可解析', verdicts is not None)
    if verdicts:
        for k in ('real', 'likely', 'blocked', 'cgnat', 'private', 'unknown'):
            v = verdicts.get(k) or {}
            chk('分级 %-8s 有 title/level/conclusion/advice' % k,
                bool(v.get('title')) and bool(v.get('level'))
                and bool(v.get('conclusion')) and isinstance(v.get('advice'), list))
        # 用户遇到的正是这种：公网地址 + 入向不通
        blk = verdicts['blocked']['conclusion']
        chk('blocked 的结论点明「连不上」', ('连不' in blk) or ('ping 不通' in blk),
            '→ %s…' % blk[:30])
        chk('blocked 给出内网穿透建议',
            any('穿透' in a for a in verdicts['blocked']['advice']))

    print('\n--- 判定逻辑：入向实测才是拍板依据 ---')
    # 关键分支：地址是公网段时，结论必须由 inbound 决定，不能停在 likely
    chk('公网地址 + 入向可达 → real',
        re.search(r"inbound is True:\s*\n\s*verdict = 'real'", HELPER_SRC) is not None)
    chk('公网地址 + 入向实测失败 → blocked',
        re.search(r"inbound is False:\s*\n\s*verdict = 'blocked'", HELPER_SRC) is not None)
    chk('未做入向实测 → likely（不是直接判公网）',
        re.search(r"else:\s*\n\s*verdict = 'likely'", HELPER_SRC) is not None)
    chk('CGNAT 地址段直接判 cgnat',
        re.search(r"cls == 'cgnat':\s*\n\s*verdict = 'cgnat'", HELPER_SRC) is not None)
    chk('外部回显全失败 → unknown（不乱下结论）',
        "verdict = 'unknown'" in HELPER_SRC)

    print('\n--- 入向实测的实现安全性 ---')
    chk('优先用 tcpdump 抓 ICMP（零监听端口）', 'icmp[icmptype] = 8' in HELPER_SRC)
    # 局域网内的 ping 不算数，否则隔壁电脑一 ping 就误判成公网可达
    chk('排除局域网来源（避免误判）', 'not src net' in HELPER_SRC
        and 'def _local_subnets' in HELPER_SRC)
    chk('备用 TCP 端口只用高位端口', '_PROBE_PORTS = (41' in HELPER_SRC)
    chk('TCP 退路不碰 5900 / 8443',
        '5900' not in HELPER_SRC.split('_PROBE_PORTS')[1][:200])
    chk('实测有超时上限', '_PROBE_TIMEOUT =' in HELPER_SRC)

    print('\n--- HTTP 路由 ---')
    chk('注册 /api/pubip', "'/api/pubip'" in WEB_SRC)
    chk('需要登录', re.search(r"p in \('/api/pubip'.*?if not self\.auth\(\)", WEB_SRC, re.S) is not None)
    chk('check 用长超时（要打多个外部服务）',
        re.search(r"to = 90 if op == 'check'", WEB_SRC) is not None)
    chk('probe_* 用短超时（轮询要秒回）',
        re.search(r"to = 90 if op == 'check' else 15", WEB_SRC) is not None)

    print('\n--- 前端接线 ---')
    chk('定义了 viewPubip', 'function viewPubip(' in APP_SRC)
    chk('注册到 VIEWS', re.search(r"^\s*pubip: viewPubip,", APP_SRC, re.M) is not None)
    chk('菜单里有 pubip 项', "k: 'pubip'" in APP_SRC)
    chk('菜单文案点明入向', '入向可达性实测' in APP_SRC or '连进来' in APP_SRC)
    chk('有开始/停止实测按钮', "id=\"pi-start\"" in APP_SRC and "id=\"pi-stop\"" in APP_SRC)
    chk('轮询状态', "pubipProbePoll" in APP_SRC)
    chk('离开页面停掉轮询（否则一直打接口）',
        # 现在统一在 stopPageTimers() 里收口，go() 第一件事就是调它。
        # 这样比原来「go() 里内联一行」更不容易漏 —— 新增页面定时器只要
        # 登记进 stopPageTimers 即可，不用再去改 go()。
        'function stopPageTimers(' in APP_SRC
        and 'pubipProbeStopPoll' in APP_SRC
        and re.search(r"function go\(k\) \{\s*\n?\s*(//[^\n]*\n\s*)*stopPageTimers\(\);", APP_SRC) is not None)
    chk('DDNS 页有跳转入口', "dd-gopubip" in APP_SRC)
    chk('DDNS 页提示「地址是公网段 ≠ 能连进来」',
        '不能说明外网能不能连进来' in APP_SRC)

    print('\n--- 四条证据都渲染 ---')
    for k in ('class', 'echo', 'path', 'inbound'):
        chk('证据 key=%s' % k, ("'key': '%s'" % k) in HELPER_SRC)

    print('\n--- 地址分类逻辑单测（真实执行，不是正则匹配）---')
    # 只抽 _is_private_v4 与 _addr_class_of 两个纯函数到隔离命名空间执行
    ns = {'re': re}
    tree2 = ast.parse(HELPER_SRC)
    want = ('_is_private_v4', '_addr_class_of')
    body = [n for n in tree2.body
            if isinstance(n, ast.FunctionDef) and n.name in want]
    mod2 = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod2)
    exec(compile(mod2, '<pubip-logic>', 'exec'), ns)
    classify = ns['_addr_class_of']
    cases = [
        ('101.70.131.227', 'public', '用户实际遇到的联通地址：公网段'),
        ('8.8.8.8', 'public', 'Google DNS'),
        ('100.64.0.1', 'cgnat', 'CGNAT 段下边界'),
        ('100.127.255.255', 'cgnat', 'CGNAT 段上边界'),
        ('100.63.255.255', 'public', '紧邻 CGNAT 之外，仍是公网'),
        ('100.128.0.1', 'public', '紧邻 CGNAT 之上，仍是公网'),
        ('192.168.7.3', 'private', '局域网'),
        ('10.0.0.1', 'private', 'RFC1918 A 类'),
        ('172.16.0.1', 'private', 'RFC1918 B 类下边界'),
        ('172.31.255.255', 'private', 'RFC1918 B 类上边界'),
        ('172.32.0.1', 'public', '172.32 已超出私有段'),
        ('169.254.1.1', 'reserved', '链路本地'),
        ('224.0.0.1', 'reserved', '组播'),
        ('127.0.0.1', 'private', '回环'),
        ('', 'empty', '空地址'),
    ]
    for ip, expect, why in cases:
        got = classify(ip)
        chk('%-18s → %-8s (%s)' % (ip or '（空）', got, why), got == expect,
            '' if got == expect else '期望 %s' % expect)

    print('\n' + '=' * 60)
    if FAIL:
        print('失败 %d 项' % FAIL)
        sys.exit(1)
    print('结果: 全部通过（通过 %d 项）' % PASS)
    sys.exit(0)


if __name__ == '__main__':
    main()
