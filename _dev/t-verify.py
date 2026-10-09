#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置预检（_verify）与「/dev 节点缺失」分诊的回归测试。

背景（真实故障）：
  用户在 DHCP 页面点「保存并应用」，弹出
    「配置语法检查未通过：dnsmasq: failed to seed the random number
      generator: 没有那个文件或目录」
  根因不是配置写错，而是那台机器的 /dev 标准设备节点被误删过 ——
  devtmpfs 由内核维护，但**已删除的节点不会被内核重建**，于是
  /dev/urandom、/dev/zero、/dev/tty 连带全部块设备一起消失了很久，
  直到 dnsmasq 预检要读随机数才暴露。内核那句话里没有 "dev"、
  也没有"设备"字样，用户只会以为是自己填错了。

这里钉住三件事：
  1. _missing_dev_nodes() 能准确报出缺了哪几个节点
  2. dnsmasq 报「seed the random number generator」时，要翻译成
     中文并指明这是**系统 /dev 层面**的问题、不是配置问题
  3. 其它语法错误不受影响，仍走原来的提示路径

纯逻辑测试：把函数抽到干净命名空间里跑，注入替身 os / sh，
不碰真实 /dev，也不需要目标机。
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "backend", "drouter-helper.py")

_ck = [0, 0]


def ck(name, cond, extra=''):
    _ck[1] += 1
    if cond:
        _ck[0] += 1
        print("  [OK] %s" % name)
    else:
        print("  [!!] %s  %s" % (name, extra))


def load_src():
    with open(SRC, encoding='utf-8') as f:
        return f.read()


def extract_funcs(src, names):
    """只抽 FunctionDef，不抽模块级赋值 —— 后者会覆盖我们注入的替身。"""
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, SRC, 'exec'), ns)
    return ns


class FakePath:
    """假的 os.path：exists() 只对 missing 集合里的路径返回 False。"""

    def __init__(self, missing):
        self.missing = set(missing)

    def exists(self, p):
        return p not in self.missing

    def isfile(self, p):
        return False

    def join(self, *a):
        return os.path.join(*a)

    def basename(self, p):
        return os.path.basename(p)


class FakeOS:
    def __init__(self, missing):
        self.path = FakePath(missing)


def make_sh(result):
    """构造一个 sh() 替身，记录被调用的命令并返回预设结果。"""
    calls = []

    def _sh(cmd, **kw):
        calls.append(cmd)
        return result

    _sh.calls = calls
    return _sh


def run_verify(ns, expr_os, sh_result):
    """在注入替身后跑一次 _verify('dnsmasq', ...)。返回 ((ok, msg), sh)。

    注意：必须**直接**往 ns 里注入，不能 dict(ns) 拷贝一份 —— 抽出来的
    函数其 __globals__ 指向的是最初那个 ns，往拷贝里塞替身函数是看不见的。
    """
    tmpdir = tempfile.mkdtemp(prefix='drouter-verify-')
    sh = make_sh(sh_result)
    ns['os'] = expr_os
    ns['VERIFY_DIR'] = tmpdir
    ns['VERIFY_PATH'] = {}
    ns['sh'] = sh
    ns['log'] = lambda *a, **k: None
    return ns['_verify']('dnsmasq', [('/etc/dnsmasq.d/drouter.conf', 'port=53\n')]), sh


def main():
    src = load_src()

    print("一、源码必须定义分诊函数")
    ck("定义了 _missing_dev_nodes()", 'def _missing_dev_nodes(' in src)
    ck("_verify 里引用了它", '_missing_dev_nodes()' in src)

    # ⚠️ 清单必须**跟着实现一起更新**（1.0.10 修）：1.0.10 把校验体从 _verify
    # 拆进了 _verify_one（为了让 finally 能覆盖所有 return 路径），并新增
    # _thread_id（临时文件名唯一化）。这里没同步 → 抽出来的 ns 里缺这两个，
    # 一跑就是 NameError，看起来像实现坏了，其实是预检没跟上。
    # 教训：拆函数/加辅助函数时，**所有按名字抽函数的预检都要一起改**。
    ns = extract_funcs(src, ['_missing_dev_nodes', '_verify',
                             '_verify_one', '_thread_id'])
    ck("抽取名单含 _verify_one（拆函数后必须同步）",
       '_verify_one' in ns, "预检抽不到它 → NameError")
    ck("抽取名单含 _thread_id（临时名唯一化后必须同步）",
       '_thread_id' in ns, "预检抽不到它 → NameError")

    print()
    print("二、_missing_dev_nodes：节点齐全时不该误报")
    ns['os'] = FakeOS([])
    ck("四个节点都在 → 返回空列表", ns['_missing_dev_nodes']() == [],
       repr(ns['_missing_dev_nodes']()))

    print()
    print("三、_missing_dev_nodes：缺谁报谁")
    ns['os'] = FakeOS(['/dev/urandom'])
    got = ns['_missing_dev_nodes']()
    ck("只缺 urandom → 只报 urandom", got == ['/dev/urandom'], repr(got))

    ns['os'] = FakeOS(['/dev/urandom', '/dev/random', '/dev/zero', '/dev/tty'])
    got = ns['_missing_dev_nodes']()
    ck("全缺 → 报 4 个", len(got) == 4, repr(got))

    print()
    print("四、dnsmasq 报随机数设备错误 → 翻译成中文并指向系统问题")
    seed_err = ('dnsmasq: failed to seed the random number generator: '
                'No such file or directory')
    real_os = os  # 真 os：让 _verify 能正常创建/删除临时文件
    (ok, msg), sh = run_verify(ns, real_os, (1, '', seed_err))
    ck("预检判定为失败", ok is False, repr(ok))
    ck("提示里点明了是 /dev 问题", '/dev' in msg, msg[:90])
    ck("提示里说明是系统层面（非配置写错）",
       ('系统层面' in msg) or ('不是本次配置' in msg), msg[:120])
    ck("没有把内核英文原样抛给用户",
       'seed the random number generator' not in msg, msg[:120])
    ck("确实调用了 dnsmasq --test", any('--test' in c for c in sh.calls), repr(sh.calls))

    print()
    print("五、其它语法错误不受影响，走原路径")
    other_err = 'dnsmasq: bad option at line 3 of /run/drouter-verify/drouter.conf'
    (ok2, msg2), _ = run_verify(ns, real_os, (1, '', other_err))
    ck("预检判定为失败", ok2 is False)
    ck("原始错误信息保留", 'bad option' in msg2, msg2[:120])
    ck("不误加 /dev 相关文案", '/dev' not in msg2, msg2[:120])

    print()
    print("六、配置合法时预检通过")
    (ok3, msg3), _ = run_verify(ns, real_os, (0, 'dnsmasq: syntax check OK.', ''))
    ck("判定为通过", ok3 is True, repr(ok3))
    ck("返回 '语法检查通过'", msg3 == '语法检查通过', repr(msg3))

    print()
    print("=" * 56)
    print("通过 %d / %d" % (_ck[0], _ck[1]))
    sys.exit(0 if _ck[0] == _ck[1] else 1)


if __name__ == '__main__':
    main()
