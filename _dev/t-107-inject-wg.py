"""反向验证：逐个注入 bug，确认 t-107 的 C3c 新断言真的会变红。

⚠️ 为什么必须做：静态断言最危险的失败模式是「正则没匹配上」
于是恒绿」。之前踩过两次（heredoc 锚点没匹配上却报「反向验证通过」）。
所以这里每一条都必须**看到 NG 才算验证通过**，而且注入用的是
Write 落盘的真实脚本，不用 heredoc（heredoc 里的 old 串
因为转义问题静默不匹配过一次）。
"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
APP = os.path.join(ROOT, 'web', 'app.js')
T = os.path.join(ROOT, '_dev', 't-107.py')

H0 = io.open(HELPER, encoding='utf-8').read()
A0 = io.open(APP, encoding='utf-8').read()

# (标签, 目标文件, 要替换的原文, 替换成什么, 期望变红的断言片段)
CASES = [
    ('E1 _vpn_env 去掉 /lib/modules 探测（还原本轮误报根因）',
     HELPER,
     "    _p = os.path.join('/lib/modules', kver,\n"
     "                          'kernel/drivers/net/wireguard', _n)",
     "    _p = os.path.join('/nonexistent', kver, _n)",
     ['查的是 /lib/modules 下的模块文件']),

    ('E2 四态判定顺序打乱（need_module 永远出不来）',
     HELPER,
     "    if mod_loaded and tool:\n        state = 'ready'\n"
     "    elif mod_loaded:\n        state = 'need_tool'\n"
     "    elif mod_file:\n        state = 'need_module'\n"
     "    else:\n        state = 'unsupported'",
     "    if mod_loaded:\n        state = 'ready'\n"
     "    elif tool:\n        state = 'need_tool'\n"
     "    else:\n        state = 'unsupported'",
     ['四态判定顺序正确']),

    ('E3 只认 .ko 不认 .ko.xz（Debian 默认是 xz，会全判 unsupported）',
     HELPER,
     "    for _n in ('wireguard.ko', 'wireguard.ko.xz', 'wireguard.ko.zst'):",
     "    for _n in ('wireguard.ko',):",
     ['覆盖 .ko/.ko.xz/.ko.zst']),

    ('E4 autoload 漏写模块名（文件写了但没写 wireguard 这一行）',
     HELPER,
     "           'wireguard\\n')",
     "           '# nothing\\n')",
     ['真的往 WG_AUTOLOAD 写 wireguard 这一行']),

    ('E5 modprobe 只看 rc，不复核 /sys/module',
     HELPER,
     "    if rc != 0 or not os.path.isdir('/sys/module/wireguard'):\n"
     "        return False, 'modprobe wireguard 失败：%s' % (\n"
     "            e or o or ('返回码 %d' % rc))",
     "    if rc != 0:\n        return False, 'modprobe failed'",
     ['以 /sys/module 复核']),

    ('E6 apply 不自愈，直接报错（还原原缺陷）',
     HELPER,
     "        if env['state'] == 'need_module':\n"
     "            okm, why = _vpn_ensure_module()",
     "        if False:\n            okm, why = _vpn_ensure_module()",
     ['遇 need_module 先 modprobe 自愈']),

    ('E7 apply 自愈后不重读 env（拿旧状态往下走）',
     HELPER,
     "            env = _vpn_env()\n        if env['state'] != 'ready':",
     "            pass\n        if env['state'] != 'ready':",
     ['自愈后重新读一次 env']),

    ('E8 status 不回传 env（前端拿不到四态）',
     HELPER,
     "        'env': _vpn_env(),",
     "        # 'env': _vpn_env(),",
     ['_vpn_status 返回 env']),

    ('E9 act_vpn 不派发 fix op',
     HELPER,
     "    if op == 'fix':\n        return _vpn_fix_op()",
     "    if op == 'fix_xxx':\n        return _vpn_fix_op()",
     ['act_vpn 派发 fix op']),

    ('E10 fix 不幂等（已 ready 也重装一遍）',
     HELPER,
     "    if before['state'] == 'ready':",
     "    if before['state'] == 'never':",
     ['_vpn_fix_op 是幂等的']),

    ('E11 前端文案退回一刀切「内核不支持 + 精简容器」',
     APP,
     "  if (env0.state === 'unsupported') {\n"
     "    env = '<div class=\"notice err\">本机内核 <span class=\"mono\">'\n"
     "      + esc(env0.kernel || '') + '</span> 里没有 wireguard 模块，无法启动服务。'\n"
     "      + '<span class=\"mono\">精简容器镜像和自编内核会裁掉它；'\n"
     "      + 'Debian 官方内核与 PVE / KVM 虚拟机都自带。</span></div>';",
     "  if (env0.state === 'unsupported') {\n"
     "    env = '<div class=\"notice err\">本机内核或工具不支持 WireGuard，'\n"
     "      + '无法启动服务。<span class=\"mono\">出现这个提示通常说明跑在'\n"
     "      + '精简容器里。</span></div>';",
     ['不再断言「精简容器」', '一刀切说法', '按四态分支']),

    ('E12 前端删掉「一键修复」按钮（只剩 need_tool 一档有）',
     APP,
     "      + '<div class=\"row\" style=\"margin-top:8px\">'\n"
     "      + '<button class=\"primary\" id=\"vp-fix\">一键修复（加载模块并配置开机自启）</button>'\n"
     "      + '</div>';",
     "      + '<div class=\"row\" style=\"margin-top:8px\"></div>';",
     ['每一档都给出可执行动作']),

    ('E13 vpnFix 不调 fix op',
     APP,
     "  const r = await api('/api/vpn', { method: 'POST', body: { op: 'fix' } });",
     "  const r = await api('/api/vpn', { method: 'POST', body: { op: 'noop' } });",
     ['vpnFix 调的是 fix op']),

    ('E14 vpnFix 去掉二次确认（会偷偷 apt-get install）',
     APP,
     "  if (!confirm('一键修复会做这几件事：\\n\\n'",
     "  if (false && confirm('一键修复会做这几件事：\\n\\n'",
     ['装包前有二次确认']),

    ('E15 前端把老后端 no 直接当 unsupported（误报复活）',
     APP,
     "  const env0 = d.env || { state: 'unknown', kernel: '', virt: '',\n"
     "    autoload: false };",
     "  const env0 = d.env || { state: d.installed === 'no'\n"
     "    ? 'unsupported' : 'ready', kernel: '', virt: '', autoload: false };",
     ['老后端兜底是 unknown']),

    ('E16 重新注入本轮踩到的死分支（残留 else if）',
     APP,
     "  } else if (!d.has_systemd) {\n"
     "    env = '<div class=\"notice warn\">当前环境没有 systemd（容器形态）。'",
     "  } else if (!d.has_systemd) {\n"
     "    env = '<div class=\"notice err\">残留的旧分支</div>';\n"
     "  } else if (!d.has_systemd) {\n"
     "    env = '<div class=\"notice warn\">当前环境没有 systemd（容器形态）。'",
     ['残留的重复 else if 死分支']),
]


def run_t107():
    p = subprocess.run([PY, T], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', cwd=ROOT)
    return p.stdout + p.stderr


def failed_items(out):
    """取「失败项：」小节里的条目。"""
    tail = out.split('失败项：')[-1] if '失败项：' in out else ''
    return [ln.strip().lstrip('·').strip()
            for ln in tail.splitlines() if ln.strip().startswith('·')]


base_out = run_t107()
base_fail = failed_items(base_out)
if base_fail:
    # ⚠️ 这里曾踩过：分批跑时一批被 SIGTERM 打断，finally 没执行，
    # 注入留在文件里，下一批的「基线全绿」检查就报看不懂的红。
    # 所以基线不干净时**必须停下人工确认**，绝不能继续注入 ——
    # 在污染状态上跑，验证结果毫无意义。
    print('基线不干净，先修基线：%s' % base_fail)
    print('（若这是上一批被中断残留的注入，手动还原后重跑）')
    sys.exit(1)
print('基线全绿，开始反向验证（每条都必须看到 NG）')
print('=' * 68)

# 长会话里一口气跑 16 次完整 t-107 会被 SIGTERM（无输出直接死），
# 所以支持按批跑：--batch 2 表示只跑第 2 批（每批 8 个）。
_only = None
if '--batch' in sys.argv:
    _only = int(sys.argv[sys.argv.index('--batch') + 1])
if '--only' in sys.argv:
    # 只跑指定编号（调试单条注入用）
    _pick = [int(x) for x in sys.argv[sys.argv.index('--only') + 1].split(',')]
    CASES = [CASES[i - 1] for i in _pick]
    _only = None
_PB = 8
batch = CASES[_only * _PB:(_only + 1) * _PB] if _only is not None else CASES
if _only is not None:
    print('（第 %d 批，共 %d 个注入）' % (_only, len(batch)))

bad = []
for label, path, old, new, expect in batch:
    orig = H0 if path == HELPER else A0
    if old not in orig:
        bad.append('%s：锚点没匹配上（等于没注入）' % label)
        print('[SKIP-BAD] %s —— 锚点未命中，注入无效' % label)
        continue
    io.open(path, 'w', encoding='utf-8').write(orig.replace(old, new, 1))
    try:
        out = run_t107()
        fails = failed_items(out)
        hit = [e for e in expect
               if any(e in f for f in fails)]
        if hit:
            print('[OK] %s' % label)
            print('       变红：%s' % '；'.join(hit))
        else:
            bad.append('%s：注入后仍全绿（断言恒绿）' % label)
            print('[NG-VERIFY] %s' % label)
            print('       期望红：%s' % '；'.join(expect))
            print('       实际失败项：%s' % (fails or '无'))
    finally:
        io.open(path, 'w', encoding='utf-8').write(orig)

print('=' * 68)
# 还原后必须回到全绿
after = run_t107()
after_fail = failed_items(after)
if after_fail:
    bad.append('还原后仍红：%s' % after_fail)
    print('[NG-VERIFY] 还原后仍失败：%s' % after_fail)
else:
    print('[OK] 全部还原，t-107 回到全绿')

if bad:
    print()
    print('反向验证失败 %d 项：' % len(bad))
    for b in bad:
        print('  · %s' % b)
    sys.exit(1)
print('反向验证全部通过：%d 个注入全部被断言抓住' % len(batch))
