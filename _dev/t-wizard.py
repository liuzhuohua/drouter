#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新手向导弹测：纯逻辑 + 参数校验 + 步骤提交 + 前后端接线 + 安全红线。

为什么这个页面的测试要写这么细：
  新手向导是「第一次用这台机器的人」唯一会看的页面。别的页面填错了，
  用户至少知道自己在改什么，能自己判断；这里填错，用户没有任何判断依据，
  只会得出「这东西坏了」的结论。所以每一个校验分支、每一句错误提示
  都要单独钉一遍 —— 尤其是「地址池把本机自己圈进去」这类，
  不拦的话表现是「网络时通时不通」，没有任何日志会提示原因。
"""
import ast
import io
import os
import re
import sys
import ipaddress

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')
WEB = os.path.join(ROOT, 'backend', 'drouter-web.py')
APP = os.path.join(ROOT, 'web', 'app.js')
CSS = os.path.join(ROOT, 'web', 'app.css')

PASS = FAIL = 0


def _strip_t_wrapper(src):
    """把 ${t('x')} / t('x') / ${T('x')} 还原成 x。"""
    out = re.sub(r"\$\{[tT]\('((?:[^'\\\\]|\\\\.)*)'\)\}",
                  lambda m: m.group(1), src)
    out = re.sub(r"(?<![A-Za-z0-9_$.])[tT]\('((?:[^'\\\\]|\\\\.)*)'\)",
                  lambda m: m.group(1), out)
    return out


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
# 2026-10-07：中文已被包进 t('…')，裸中文匹配全部失效。
# 构造一份「剥掉 t()/T() 包装」的源码副本，判据查它 ——
# 这样「代码里有没有这段中文」与它是否被包 t() 无关。
APP_SRC_PLAIN = _strip_t_wrapper(APP_SRC)

CSS_SRC = io.open(CSS, encoding='utf-8').read()
TREE = ast.parse(HELPER_SRC)

# 向导用到的全部纯函数（不碰系统、不碰数据库）
WIZ_FUNCS = [
    '_wiz_ip_in_pool', '_wiz_same_subnet',
]
WIZ_CONSTS = ['WIZ_STEPS', 'WIZ_DNS_PRESETS', 'WIZ_DNS_DEFAULT', 'WIZ_POOL_HINT',
              'WIZ_WAN_MODES', 'WIZ_LEASE_DEFAULT', 'WIZ_DNS_CACHE_DEFAULT']


def node(name):
    for n in TREE.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return n
        elif isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def build(names, ns=None):
    ns = ns if ns is not None else {}
    body = []
    for nm in names:
        n = node(nm)
        if n is None:
            raise AssertionError('源码里找不到 %s' % nm)
        body.append(n)
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, '<wiz>', 'exec'), ns)
    return ns


# ---------------------------------------------------------------- 注入替身
# helper 里的 sh()/systemctl/落盘 全都要挡住：单测在 Windows 上跑，
# 真去调 systemctl 会得到「命令不存在」，把「逻辑对不对」和「环境有没有」
# 混在一起，最后没人再信这套测试。
def make_ns(extra=None):
    """构造一个只含向导纯函数的命名空间，并注入可控替身。

    注意：**必须**用平铺的 from-import 假模块，不要用真实的 ipaddress ——
    ipaddress 是本模块也 import 的，如果用同一个对象，测试里对它的 patch
    会串到被测代码里去（跨用例污染，排起来极痛苦）。
    """
    ns = {'ipaddress': ipaddress, 're': re, 'json': __import__('json')}
    build(WIZ_CONSTS, ns)
    build(WIZ_FUNCS, ns)
    return ns


def real_sh_stub(calls, results):
    """sh() 的替身：按命令行前缀匹配预设结果，没匹配到就返回 (0,'','')"""
    def _sh(cmd, timeout=None):
        if isinstance(cmd, str):
            key = cmd
        else:
            key = ' '.join(str(x) for x in cmd)
        calls.append(key)
        for pat, r in results:
            if key.startswith(pat):
                return r
        return (0, '', '')
    return _sh


def main():
    print('=== 一、后端动作与路由接线 ===')
    chk('定义了 act_wizard', 'def act_wizard(' in HELPER_SRC)
    chk('注册到 ACTIONS', re.search(r"^\s*'wizard':\s*act_wizard,", HELPER_SRC, re.M) is not None)
    chk('Web 路由 /api/wizard', "'/api/wizard'" in WEB_SRC)
    chk('probe 超时 60 秒', re.search(r"to = 120 if op == 'dial' else \(110 if op == 'apply_step' else 60\)", WEB_SRC) is not None)
    chk('未知 op 返回 fail', '未知的新手向导操作' in HELPER_SRC)
    chk('路由走 self.helper', re.search(r"self\.helper\('wizard', b, timeout=to\)", WEB_SRC) is not None)
    chk('路由要求登录', re.search(r"if p in \('/api/wizard'.*?\n\s*if not self\.auth\(\):",
                                WEB_SRC, re.S) is not None)

    print('\n=== 二、四个步骤与顺序 ===')
    ns = make_ns()
    steps = ns['WIZ_STEPS']
    chk('正好四步', len(steps) == 4, str(len(steps)))
    chk('顺序是 wan→lan→dns→v6',
        [s['k'] for s in steps] == ['wan', 'lan', 'dns', 'v6'],
        str([s['k'] for s in steps]))
    chk('每步都有中文名', all(s.get('n') for s in steps))
    chk('每步都有说明', all(s.get('d') for s in steps))
    chk('第 4 步说明里点明可选', '要' in steps[3]['n'] or '要不要' in steps[3]['n'])

    print('\n=== 三、上网方式（第 1 步）===')
    modes = ns['WIZ_WAN_MODES']
    chk('三种方式齐全', [m['k'] for m in modes] == ['pppoe', 'dhcp', 'static'])
    chk('每种都有说明', all(m.get('d') for m in modes))
    chk('每种都有「什么时候选它」', all(m.get('when') for m in modes),
        '新手最需要的就是这个判断依据')
    chk('PPPoE 说明里点了运营商', '电信' in (modes[0]['d'] + modes[0]['when']))
    chk('DHCP 说明里点了光猫', '光猫' in (modes[1]['d'] + modes[1]['when']))
    chk('静态说明里点了专线', '专线' in (modes[2]['d'] + modes[2]['when']))
    print('\n=== 四、DNS 服务商范例（第 3 步）===')
    presets = ns['WIZ_DNS_PRESETS']
    keys = [p['k'] for p in presets]
    chk('阿里 DNS 在列表里', 'ali' in keys)
    chk('腾讯 DNS 在列表里', 'dnspod' in keys)
    chk('114 在列表里', '114' in keys)
    chk('Cloudflare 在列表里', 'cf' in keys)
    chk('Google 在列表里', 'gg' in keys)
    chk('跟随运营商在列表里', 'isp' in keys)
    ali = [p for p in presets if p['k'] == 'ali'][0]
    chk('阿里 DNS 地址正确', ali['v4'] == '223.5.5.5,223.6.6.6', ali['v4'])
    dp = [p for p in presets if p['k'] == 'dnspod'][0]
    chk('DNSPod 地址正确', dp['v4'] == '119.29.29.29,182.254.116.116', dp['v4'])
    chk('每个都有推荐理由', all(p.get('d') for p in presets))
    chk('国外 DNS 标明了国内体验差',
        '国内' in [p for p in presets if p['k'] == 'cf'][0]['d'],
        '不标的话新手会以为国外的更「高级」')
    chk('ISP 项的地址留空（表示自动获取）',
        [p for p in presets if p['k'] == 'isp'][0]['v4'] == '')
    chk('默认 DNS 是国内地址', ns['WIZ_DNS_DEFAULT'].startswith('223.5.5.5'),
        ns['WIZ_DNS_DEFAULT'])

    print('\n=== 五、地址池范例提示（新手最容易填错的地方）===')
    hint = ns['WIZ_POOL_HINT']
    chk('有 why 说明', bool(hint.get('why')))
    rules = hint.get('rules') or []
    chk('至少三条规则', len(rules) >= 3, str(len(rules)))
    joined = ' '.join(rules)
    chk('讲了「前三段要一致」', '前三段' in joined)
    chk('讲了「别包含本机地址」', '本机' in joined)
    chk('给了 .100 起的具体做法', '.100' in joined)

    print('\n=== 六、地址池校验的纯逻辑 ===')
    ps = ns['_wiz_ip_in_pool']
    chk('池内地址 → True', ps('192.168.7.150', '192.168.7.100', '192.168.7.200') is True)
    chk('池外（下方）→ False', ps('192.168.7.9', '192.168.7.100', '192.168.7.200') is False)
    chk('池外（上方）→ False', ps('192.168.7.201', '192.168.7.100', '192.168.7.200') is False)
    chk('边界起点 → True', ps('192.168.7.100', '192.168.7.100', '192.168.7.200') is True)
    chk('边界终点 → True', ps('192.168.7.200', '192.168.7.100', '192.168.7.200') is True)
    # 关键：必须按数值比，不能按字符串比 —— '192.168.7.9' > '192.168.7.100'
    # 在字符串比较里成立，会得出「9 比 100 大」的荒谬结论。
    chk('按数值比而不是字符串比（7.9 vs 7.100）',
        ps('192.168.7.9', '192.168.7.100', '192.168.7.200') is False,
        '字符串比较会把 .9 判成池内')
    chk('起点大于终点时自动纠正',
        ps('192.168.7.150', '192.168.7.200', '192.168.7.100') is True)
    chk('非法 IP 不抛异常', ps('abc', '192.168.7.1', '192.168.7.2') is False)
    chk('空串不抛异常', ps('', '', '') is False)

    ss = ns['_wiz_same_subnet']
    chk('同网段 → True', ss('192.168.7.100', '192.168.7.3') is True)
    chk('不同网段 → False', ss('192.168.8.100', '192.168.7.3') is False)
    chk('掩码参与计算（/16 时跨段也算同网）',
        ss('192.168.8.100', '192.168.7.3', '255.255.0.0') is True)
    chk('非法掩码不抛异常', ss('192.168.7.1', '192.168.7.3', 'not-a-mask') is False)

    print('\n=== 七、第 1 步的参数校验（不该让坏参数走进 openssl/systemd）===')
    body = HELPER_SRC[HELPER_SRC.index('def _wiz_apply_step('):]
    body = body[:body.index('def _wiz_dial(')]
    chk('拒绝未知 step', '未知的向导步骤' in body)
    chk('PPPoE 必须填账号', "return fail('PPPoE 拨号必须填宽带账号'" in body)
    chk('PPPoE 必须填密码', "return fail('PPPoE 拨号必须填宽带密码'" in body)
    chk('账号过滤危险字符', '宽带账号不能包含空格' in body)
    chk('静态模式必须填地址', "return fail('静态地址模式要填 IP 地址 / 掩码'" in body)
    chk('静态模式必须填网关', "return fail('静态地址模式要填网关'" in body)
    chk('静态地址做格式校验', 'ipaddress.ip_interface(v)' in body)
    chk('网卡名做白名单校验', re.search(r"re\.match\(r'\^\[A-Za-z0-9_\.:@-\]\{1,15\}\$', iface\)", body) is not None)
    chk('WAN 网卡角色会落到 system 配置', "sysc['wan_iface'] = iface" in body)

    print('\n=== 八、第 2 步的地址池校验 ===')
    chk('地址池必须同网段', 'POOL_SUBNET' in body)
    chk('地址池不能圈进本机自己', 'POOL_SELF' in body)
    chk('错误提示里给了修正建议', '请把池子的起点往后挪' in body)
    chk('起止地址做格式校验', "return fail('%s 不是合法的 IPv4 地址' % f, 'BAD_POOL')" in body)
    chk('租期有上下限钳制', 'max(120, min(int(' in body)
    chk('自动把网关填成本机', "cfg['option_gateway'] = lan_ip" in body)
    chk('DNS 下发项做格式校验', "'BAD_DNS_OPT'" in body)
    chk('强制打开 DHCP 开关', "cfg['dhcp_enabled'] = True" in body)

    print('\n=== 九、第 3 步的 DNS 校验 ===')
    chk('校验 mode 取值', "if mode not in ('isp', 'custom', 'both')" in body)
    chk('选了自定义就必须填', "'NO_DNS'" in body)
    chk('逐个校验地址格式', "'BAD_DNS'" in body)
    chk('提示里说明用逗号分隔', '英文逗号分隔' in body)
    chk('缓存条数有钳制', 'max(0, min(int(' in body)

    print('\n=== 十、第 4 步的 IPv6 校验 ===')
    chk('接受 enable 开关', "want = b.get('enable')" in body)
    chk('关闭时不删已填前缀', '已填写的 IPv6 前缀保留未删除' in body)
    chk('关闭 IPv6 不影响 IPv4 的说法', '不影响 IPv4 上网' in body)
    chk('radvd 没装时给人话提示', 'radvd 没装' in body)
    chk('前缀做规范解析', 'ipaddress.ip_network(prefix, strict=False)' in body)
    chk('非 v6 前缀被拒', "if net.version != 6" in body)
    chk('强制 /64 并说明原因', '内网 RA 通告只能用 /64' in body)
    chk('说明里点了 RFC 精神（客户端会忽略）', '手机电脑会直接忽略' in body)
    chk('RDNSS 做格式校验', "'BAD_RDNSS'" in body)
    chk('WAN 无 PD 时给提示', '还没拿到运营商下发的 IPv6 前缀' in body)

    print('\n=== 十一、apply 走的是既有通道，不另造一套 ===')
    chk('_wiz_apply 调用的是 act_apply', "r = act_apply({'module': mod, 'cfg': cfg, 'live': True})" in HELPER_SRC)
    chk('live=True 显式传入', "'live': True" in HELPER_SRC)
    chk('dnsmasq 走 dnsmasq 模块', "_wiz_apply('dnsmasq', cfg)" in body)
    chk('radvd 走 radvd 模块', "_wiz_apply('radvd', radvd_cfg)" in body)
    chk('pppoe 走 pppoe 别名', "_wiz_apply('pppoe', ppp)" in body)
    chk('不直接写 /etc/ 下的文件', not re.search(r"open\('/etc/(?!drouter/kern)", body),
        '向导只改配置库，落盘交给 act_apply')

    print('\n=== 十二、失败的粒度：半成功不回滚 ===')
    chk('只把失败的模块报出来', "if errs and not done:" in body)
    chk('注释里写明不回滚的理由', '不做整体回滚' in body or '回滚会把用户原本就已正确的配置也一起抹掉' in HELPER_SRC)
    chk('成功与失败分开收集', 'done, errs, notes =' in body)
    chk('返回里带上三个列表', "'done': done, 'errors': errs, 'notes': notes" in body)

    print('\n=== 十三、拨号是独立动作（不会偷偷断网）===')
    dial = HELPER_SRC[HELPER_SRC.index('def _wiz_dial('):]
    dial = dial[:dial.index('def act_wizard(')]
    chk('拨号必须显式确认', "if not p.get('confirm'):" in dial)
    chk('确认缺失时给出原因', '拨号会短暂中断现有网络' in dial)
    chk('只认 connect/disconnect', "if op not in ('connect', 'disconnect')" in dial)
    chk('复用 act_ppp_control', 'act_ppp_control(' in dial)
    chk('注释说明为何要分开', '写凭据不会断网' in HELPER_SRC or '独立操作' in HELPER_SRC)
    # 保存配置那一步绝不能自己拨号
    chk('apply_step 里不调用 connect', "act_ppp_control({'op': 'connect'" not in body,
        '保存账号 ≠ 拨号，绑一起会让用户在不知情下断网')

    print('\n=== 十四、体检（probe）的判据设计 ===')
    probe = HELPER_SRC[HELPER_SRC.index('def _wiz_probe('):]
    probe = probe[:probe.index('def _wiz_iface_options(')]
    chk('用 223.5.5.5 判外网可达', "'223.5.5.5'" in probe)
    chk('注释说明为何不用 8.8.8.8', '8.8.8.8' in probe and '常态' in probe)
    chk('体检要求有默认路由', 'default_rt' in probe)
    chk('IPv6 不通不算失败（只提示不拦）', 'IPv6 不通不算' in probe or '可选' in probe)
    chk('不给总分（说明为什么）', '总分对新手没有意义' in HELPER_SRC or '一个总分' in HELPER_SRC)
    chk('返回 internet_ok 汇总', "'internet_ok':" in probe)
    chk('返回 todo 列表', "'todo': todo" in probe)
    chk('返回 build_mode', "'build_mode': in_build_mode()" in probe)

    print('\n=== 十五、DNS 探测要给人话原因 ===')
    dnsf = HELPER_SRC[HELPER_SRC.index('def _wiz_dns_probe('):]
    dnsf = dnsf[:dnsf.index('def _wiz_has_ipv6_global(')]
    chk('超时给人话', '查询超时' in dnsf)
    chk('拒绝给人话', '连接被拒绝' in dnsf)
    chk('NXDOMAIN 给人话', '域名不存在' in dnsf)
    chk('没有 dig 时回退 nslookup', 'nslookup' in dnsf)
    chk('都没有时如实说明', '无法测试' in dnsf)

    print('\n=== 十六、前端接线 ===')
    chk('菜单里有 wizard', re.search(r"k: 'wizard'", APP_SRC) is not None)
    chk('向导排在概览组第一位',
        APP_SRC.index("k: 'wizard'") < APP_SRC.index("k: 'dash'"),
        '新手第一个看到的应该就是它')
    chk('菜单标题解释了它是干什么的',
        re.search(r"k: 'wizard'.{0,200}第一次用这台机器", APP_SRC, re.S) is not None)
    chk('VIEWS 注册了 wizard', re.search(r"^\s*wizard:\s*viewWizard,", APP_SRC, re.M) is not None)
    chk('定义了 viewWizard', 'async function viewWizard(' in APP_SRC_PLAIN)
    chk('视图函数放在 VIEWS 之前使用（函数声明提升）',
        APP_SRC.index('const VIEWS') < APP_SRC.index('async function viewWizard('),
        'VIEWS 在模块顶层求值，viewWizard 必须是函数声明而不是 const 箭头函数')
    for fn in ('wizLoad', 'wizRender', 'wizRenderStep', 'wizStepWan', 'wizStepLan',
               'wizStepDns', 'wizStepV6', 'wizBindStep', 'wizSubmit',
               'wizApplyWan', 'wizApplyLan', 'wizApplyDns', 'wizApplyV6'):
        chk('定义了 %s' % fn, ('function %s(' % fn) in APP_SRC)

    print('\n=== 十七、前端范例提示（用户点名要求的）===')
    chk('PPPoE 账号给了范例', '范例：0512' in APP_SRC_PLAIN)
    chk('说明了「不是 Wi-Fi 密码」', '不是 Wi-Fi 密码' in APP_SRC_PLAIN)
    chk('DHCP 说明给了接线范例', '光猫的 LAN 口' in APP_SRC_PLAIN)
    chk('静态地址给了 /24 的解释', '表示掩码是 255.255.255.0' in APP_SRC_PLAIN)
    chk('地址池给了「照着抄」的段落', '照着抄就行' in APP_SRC_PLAIN)
    chk('地址池范例由后端下发（不写死）', 'base + \'.100\'' in APP_SRC_PLAIN)
    chk('DNS 说明推荐了具体一家', '不知道选哪个就用它' in APP_SRC_PLAIN)
    chk('解释了填两个 DNS 的作用', '一个不通时自动换另一个' in APP_SRC_PLAIN)
    chk('IPv6 前缀给了范例', '2408:8207:1234:5678::/64' in APP_SRC_PLAIN)
    chk('IPv6 说明点了 /64 的硬要求', '内网通告只能用 /64' in APP_SRC_PLAIN)
    chk('IPv6 明确写了不开也不影响', '不开也不影响上网' in APP_SRC_PLAIN)

    print('\n=== 十八、前端不重复后端的一份数据 ===')
    # 只看向导这一段，别把 WAN 页 / DNS 页里本来就有的 223.5.5.5 算进来。
    # 断言的是「向导没有自己另抄一份默认值」，不是「整个 app.js 只准出现几次」。
    wiz_body = APP_SRC[APP_SRC.index('let WIZ = null;'):APP_SRC.index('/* ============================ 动态域名 DDNS')]
    hard_default = re.search(r"=\s*'223\.5\.5\.5", wiz_body) is not None
    chk('向导默认 DNS 不由前端写死', not hard_default,
        '默认值应来自 n.default_custom（后端 WIZ_DNS_DEFAULT）')
    # 2026-10-05：步骤名改走 bt4('WIZ_STEPS', …) 查英文（后端零改动），
    # 原来查 `esc(s.n)` 字面量形态，判据没跟上。
    # ⛔ 2026-10-07：这两条原来断言 bt4(..., 'name', ...) / (..., 'desc', ...)，
    #    但 i18n.js 的 WIZ_* 表字段是 'n' / 'd' —— 断言本身在认可错字段名。
    #    bt4 查不到字段就回落后端中文，英文界面整段显示中文且**不报错**。
    #    正解：断言「bt4 用的字段名必须真实存在于 i18n 的该表里」。
    I18N = io.open(os.path.join(ROOT, 'web', 'i18n.js'), encoding='utf-8').read()

    def _bt_field_ok(table, field):
        m = re.search(r"'%s':\s*\{" % re.escape(table), I18N)
        if not m:
            return False
        i = m.end() - 1
        d = 0
        for j in range(i, len(I18N)):
            if I18N[j] == '{':
                d += 1
            elif I18N[j] == '}':
                d -= 1
                if d == 0:
                    break
        blk = I18N[i:j + 1]
        return bool(re.search(r"'%s'\s*:" % re.escape(field), blk))

    for _tb, _fld, _lbl in [('WIZ_STEPS', 'n', '步骤名'),
                            ('WIZ_WAN_MODES', 'n', '上网方式名'),
                            ('WIZ_WAN_MODES', 'd', '上网方式说明'),
                            ('WIZ_DNS_PRESETS', 'n', 'DNS 服务商名'),
                            ('WIZ_DNS_PRESETS', 'd', 'DNS 服务商说明')]:
        chk('%s走 bt4 且字段名与 i18n 表一致（%s.%s）' % (_lbl, _tb, _fld),
            ("bt4('%s'" % _tb) in APP_SRC and _bt_field_ok(_tb, _fld))

    # 反向：bt4 的第三个参数不得再出现 'name'/'desc'（那会静默回落中文）
    _bad_fld = []
    for _m in re.finditer(r"bt4\('(\w+)',[^,]+,'(\w+)'", APP_SRC):
        if _m.group(2) in ('name', 'desc') and _bt_field_ok(_m.group(1), 'n'):
            _bad_fld.append(_m.group(0))
    chk('bt4 不再用错字段名 name/desc（会静默回落中文）', not _bad_fld,
        '；'.join(_bad_fld[:3]))
    chk('DNS 服务商卡片渲染后端 presets', 'presets.map(p =>' in wiz_body)

    print('\n=== 十九、断网风险：拨号按钮要二次确认 ===')
    dial_ui = APP_SRC[APP_SRC.index('function wizBindDial('):]
    dial_ui = dial_ui[:dial_ui.index('function wizOut(')]
    chk('拨号前弹确认框', 'modal(' in dial_ui)
    # 2026-10-05：拨号确认框的「拨号」二字被包成 t('拨号')（i18n 补全），
    # 原来查 `!== '拨号'` 的字面量形态失效。判据要跟代码形态一起走。
    chk('确认要输入指定文字',
        # ⚠️ 2026-10-07：翻译调用统一成 T('…')，判据两种形态都要认。
        ("!== '拨号'" in dial_ui)
        or re.search(r"!==\s*[tT]\('拨号'\)", dial_ui) is not None,
        "两种形态都没找到：拨号确认框的必输文字")
    chk('明确告知会中断网络', '网络短暂中断' in dial_ui)
    chk('明确说保存不必点它', '不用点这个按钮' in dial_ui)
    chk('拨号带 confirm 字段', re.search(r"confirm:\s*true", dial_ui) is not None)

    print('\n=== 二十、安全红线（每轮必查）===')
    chk('不启动 dnsmasq（由 act_apply 按配置决定）',
        'systemctl", "start", "dnsmasq' not in HELPER_SRC.replace("'", '"'))
    chk('不碰 5900', '5900' not in body)
    chk('不写 nft 规则文件', 'nftables.d' not in body)
    chk('保护模式下不重启服务（act_apply 负责）', 'in_build_mode()' in body)
    chk('不执行 shell 字符串拼接', not re.search(r"sh\(['\"]", body))
    chk('所有 sh 调用是列表形式', body.count('sh([') > 0)
    chk('DHCP 只在用户点保存时才启用（不是页面加载就开）',
        "cfg['dhcp_enabled'] = True" in body and 'def _wiz_probe' in HELPER_SRC)

    print('\n=== 二十一、样式 ===')
    chk('步骤条样式存在', '.wiz-steps{' in CSS_SRC)
    chk('步骤条窄屏改网格', '.wiz-steps{display:grid' in CSS_SRC)
    chk('DNS 卡片样式存在', '.wiz-dns{' in CSS_SRC)
    # 轨道用 minmax(0,1fr) 而非裸 1fr：1fr 的隐含 min-width:auto 会被
    # 长英文标签（Local machine LAN Address）撑破，窄屏整页横向滚动。
    chk('DNS 网格窄屏单列', '.wiz-dns-grid{grid-template-columns:minmax(0,1fr)}' in CSS_SRC)
    chk('步骤条可点（做了按钮不是指示器）', 'cursor:pointer' in CSS_SRC)

    print('\n========================================')
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
