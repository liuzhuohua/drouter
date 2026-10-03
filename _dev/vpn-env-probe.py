#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器冒烟探针：在镜像里 import drouter-helper 并跑 _vpn_env()，把四态打出来。

为什么需要单独一个文件（而不是在冒烟脚本里内联 heredoc）
--------------------------------------------------------
冒烟脚本经 `$PODMAN exec` 跑，heredoc 要穿过两层引号 + 一层命令替换，
bash 很容易把它吃掉或者吞掉后面的行（写的时候就踩了：留了个残缺的
`$echo "$($PODMAN exec ... <<'PYEOF'` 出来）。拆成文件后 `podman cp`
进去直接跑，失败也只是这一个文件的问题。

为什么不能只 grep 源码里的 'unsupported' 字面量
--------------------------------------------
注释和 docstring 里也会写这些词 —— 1.0.8 那轮判恒绿就是这么来的：
「查的是 /lib/modules 下的模块文件」这句话在 docstring 里，
把探测路径改成 /nonexistent 断言照样全绿。真跑一次函数就没这个问题。

输出是 KEY=VALUE，逐行print，冒烟脚本按行取。
"""
import importlib.util
import sys

HELPER = '/opt/drouter/backend/drouter-helper.py'


def main():
    try:
        spec = importlib.util.spec_from_file_location('drouter_helper', HELPER)
        if spec is None or spec.loader is None:
            print('IMPORTERR 无法为 %s 建立 spec' % HELPER)
            return 2
        m = importlib.util.module_from_spec(spec)
        # exec_module 会真的执行模块顶层代码。helper 的顶层只做常量定义
        # 与 import，不连网、不起服务（守护都是被显式调用才起来的）。
        spec.loader.exec_module(m)
    except Exception as ex:
        print('IMPORTERR %s: %s' % (type(ex).__name__, ex))
        return 2

    try:
        env = m._vpn_env()
    except Exception as ex:
        print('CALLERR %s: %s' % (type(ex).__name__, ex))
        return 3

    for k in ('state', 'virt', 'fixable', 'autoload', 'kernel',
              'mod_loaded', 'tool'):
        print('%s=%s' % (k.upper(), env.get(k)))
    # mod_file 单独打：容器里查不到时是空串，正好验「查不到也不抛异常」
    print('MODFILE=%s' % (env.get('mod_file') or '(空)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
