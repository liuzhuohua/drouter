#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护进程重启契约：改了常驻守护的源码，部署时必须真的重启它。

背景（真实事故）：deploy.sh 原本这样判断要不要重启 shelld ——
    install $SRC/backend/drouter-shelld.py $OPT/backend/drouter-shelld.py
    ...
    if ! cmp -s $SRC/backend/drouter-shelld.py $OPT/backend/drouter-shelld.py
但 install 已经把 SRC 覆盖到 OPT 了，cmp 两边同源、恒等，
于是「代码变了就重启」这个分支永远不会进。结果给 shelld 加了长轮询，
真机测出来还是 6ms 立刻返回 —— 跑的是 16:09 启动的旧进程。

本测试盯两件事：
  ① 指纹取样点在 install 之前；
  ② 每个常驻守护都有「必要时重启」的分支。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, 'scripts', 'deploy.sh')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def main():
    src = io.open(DEPLOY, encoding='utf-8').read()
    lines = src.splitlines()

    def line_of(pat):
        for i, l in enumerate(lines):
            if re.search(pat, l):
                return i
        return -1

    inst_shelld = line_of(r'install -m 0644 \$SRC/backend/drouter-shelld\.py')
    hash_new = line_of(r'SHELLD_NEW=')
    mark = line_of(r'SHELLD_MARK=')
    cmp_use = line_of(r'SHELLD_NEW.*!=.*SHELLD_SEEN')

    print('--- shelld 重启契约 ---')
    chk('deploy.sh 里存在 shelld 的 install', inst_shelld >= 0,
        '→ 第 %d 行' % (inst_shelld + 1))
    chk('存在 SHELLD_NEW 指纹取样', hash_new >= 0, '→ 第 %d 行' % (hash_new + 1))
    chk('存在 SHELLD_MARK 状态文件', mark >= 0, '→ 第 %d 行' % (mark + 1))
    chk('重启判断用「本次指纹 vs 上次已重启的指纹」', cmp_use >= 0,
        '→ 第 %d 行' % (cmp_use + 1))
    # 拿 SRC 和 OPT 比对是错的：install 之后两边同源，cmp 恒等，永远不重启
    bad = re.search(r'cmp\s+-s\s+"\$SRC/[^"]+"\s+"\$OPT/', src)
    chk('没有 install 后 cmp 的错误写法', bad is None,
        '→ %s' % (bad.group(0) if bad else '无'))
    # 也不能拿 install 前的 $OPT 比对：上一轮拷进去但没重启成功时两边又相等
    bad2 = re.search(r'SHELLD_OLD\s*=\s*\$\(sha_of\s+"\$OPT/', src)
    chk('没有拿 install 前的 $OPT 做基准（会漏掉重启失败的情况）', bad2 is None,
        '→ %s' % (bad2.group(0) if bad2 else '无'))
    chk('重启成功才更新 mark（失败下轮重试）',
        re.search(r'is-active.*drouter-shelld', src, re.S) is not None
        and re.search(r'echo\s+"\$SHELLD_NEW"\s*>\s*"\$SHELLD_MARK"', src) is not None)

    print('\n--- 其它常驻守护 ---')
    # helpd 无条件 restart（它每请求都从内存跑 helper，必须换代码就重启）
    chk('helpd 每次部署都 restart',
        re.search(r'systemctl\s+restart\s+drouter-helpd', src) is not None)
    chk('web 每次部署都 restart',
        re.search(r'systemctl\s+restart\s+\$?\{?UNIT_WEB', src) is not None)
    # logd / snapshotd 是 timer 触发的 oneshot，靠 timer 拉起即可，
    # 但部署后至少要保证单元被 enable
    # logd 定时器是 helper 的 act_logd_sync 在 Python 里 enable 的，
    # 不是 shell 里的 systemctl，所以两种写法都算数。
    chk('drouter-logd.timer 被 enable/拉起',
        re.search(r'systemctl\s+(enable|restart).*drouter-logd\.timer', src) is not None
        or 'act_logd_sync' in src)
    chk('drouter-snapshot.timer 被 enable/拉起',
        re.search(r'systemctl\s+(enable|restart).*drouter-snapshot\.timer', src) is not None)

    print('\n--- 红线：不得在部署里做这些 ---')
    for bad_pat, why in ((r'nft\s+-f\s+.*masquerade', '载入含 masquerade 的规则'),
                         (r'systemctl\s+(start|enable)\s+dnsmasq', '启动 DHCP/DNS 服务'),
                         (r'systemctl\s+(start|enable)\s+kea', '启动 DHCP 服务')):
        chk('部署脚本不会%s' % why, re.search(bad_pat, src) is None)

    print('\n' + '=' * 60)
    if FAIL:
        print('失败 %d 项' % FAIL)
        sys.exit(1)
    print('结果: 全部通过（通过 %d 项）' % PASS)
    sys.exit(0)


if __name__ == '__main__':
    main()
