#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向验证：把每条关键判据对应的实现改坏，确认判据真的会红。

这是防「恒绿假阳性」的关键一步 —— 判据自己也要被验。
每个用例：注入 → 跑判据 → 必须因为**这一条**而红 → 还原 → 恢复绿。

⚠️ 注入点必须打在**语义层**，不能打在被检查的那个名字上。
   早先踩过：判据写 `'_sha256' in u`，反向验证就去删 `_sha256` 函数
   定义 —— 而正确做法是判据查「真的拿 want 跟 actual 比」，
   注入点也要跟着改成「把 want 改成 None」。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
JUDGE = os.path.join(ROOT, '_dev', 't-update.py')

# (说明, 目标文件, 旧串, 新串, 期望变红的判据关键词, 是否替换全部出现)
# ⚠️ 第 6 个字段默认 False（只替换第一处）。**判据查的是「文件里任何一处
#    存在某串」时，必须设 True 全替换** —— 只改第一处，文件里还剩 N-1 处，
#    判据照样绿。反向验证第一版栽在这：`${esc(d.local)}` 有 4 处，
#    只改第 1 处 → 判据仍绿 → 误判成「判据不灵」，其实是注入不彻底。
CASES = [
    # 1.0.10 改成 DL_SOURCES 降级链（大陆网络实测 github.com 直连
    # 100% 丢包，「下载只走官方」等于一键更新永远不可用）。
    # 所以「混入代理」不再是红线，取而代之的是「必须走降级链且逐步记录来源」。
    ('下载源链里官方被摘掉（会直接用镜像，失去官方优先）',
     'backend/drouter-update.py',
     "    ('GitHub 官方', 'https://github.com/{repo}/releases/download/"
     "v{ver}/{name}'),",
     "    ('占位', 'https://example.invalid/{repo}/{ver}/{name}'),",
     '下载源官方排第一'),
    # 注入：把**镜像源那一项整条删掉** —— 只留官方。
    # ⚠️ 不能只在 `DL_SOURCES = [` 后面加注释：判据数的是 `https://` 出现次数，
    #    加注释不影响数量 → 注入不生效、判据不红（我第一版就这么写的，白跑一轮）。
    ('降级链被写死成只用官方（大陆网络不可用）',
     'backend/drouter-update.py',
     ("    ('ghproxy 镜像', 'https://gh-proxy.com/https://github.com/{repo}'\n"
      "                     '/releases/download/v{ver}/{name}'),\n", ""),
     None,
     '下载源 ≥ 2 个'),

    ('把校验结果改成 None（等于不校验）',
     'backend/drouter-update.py',
     "    want = sums.get(target['name'])",
     "    want = None",
     '有 SHA256 校验'),

    ('SSL 校验失败就降级（默许 MITM）',
     'backend/drouter-update.py',
     "    return ssl.create_default_context()",
     "    c = ssl.create_default_context()\n"
     "    c.check_hostname = False\n"
     "    c.verify_mode = ssl.CERT_NONE\n"
     "    return c",
     'SSL 校验失败不降级'),

    ('下载不可中断',
     'backend/drouter-update.py',
     "                if _stop_flag.is_set():\n"
     "                    raise InterruptedError('用户已取消')",
     "                pass",
     '下载可中断'),

    ('版本比较改回字符串语义（1.0.10 < 1.0.9 的坑）',
     'backend/drouter-update.py',
     "    return a > b",
     "    return '%s' % (a,) > '%s' % (b,)",
     '版本比较用元组而非字符串'),



    ('先下载后备份（顺序反了）',
     'backend/drouter-update.py',
     "            ok, bmsg = _do_backup()",
     "            ok, bmsg = (True, '')",
     '先备份后下载'),

    # ⚠️ 注入点必须是**真实的包管理调用**，不能改那句 docstring 文案 ——
    #    判据查的是「apply_update 函数体内有无 apt/dpkg 调用」，
    #    只改文案它照样绿。第一版就栽在这，误判成「判据不灵」。
    #    （锚点是 run 里加的那行 subprocess，不含中文，避免编码问题）
    ('自动安装（越过用户决定）',
     'backend/drouter-update.py',
     "        _stop_flag.clear()\n"
     "        _write_state(running=True,",
     "        subprocess.run(['apt-get', 'install', '-y', 'drouter'])\n"
     "        _stop_flag.clear()\n"
     "        _write_state(running=True,",
     '不自动安装（apply_update 内无任何包管理调用）'),

    ('用 import 加载带连字符的模块（必 ModuleNotFoundError）',
     'backend/drouter-web.py',
     "        U = self._load_mod('drouter-update')",
     "        import drouter_update as U",
     'web.py 用 importlib'),

    # backupd call() 去掉 sudo 前缀：drouter 身份（更新前备份）写不进
    # /opt/drouter/backups → [Errno 13] Permission denied（2026-10-10 真机修）。
    ('backupd call() 去掉 sudo（drouter 身份写不进备份目录）',
     'backend/drouter-backupd.py',
     "            ['sudo', '-n', '/usr/bin/python3', HELPER, action,",
     "            ['/usr/bin/python3', HELPER, action,",
     'backupd call() 用 sudo -n 提权'),

    ('/api/update 路由被摘掉',
     'backend/drouter-web.py',
     "        if p.startswith('/api/update'):",
     "        if False:",
     'web.py 接上 /api/update'),

    # —— 已废除的 passwall 残留（用户要求彻底清除）——
    ('passwall 路由复活',
     'backend/drouter-web.py',
     "        # ========== Web 终端（#18） ==========",
     "        if p.startswith('/api/pw'):\n"
     "            if not self.auth():\n"
     "                return\n"
     "            return self.json({'ok': False})\n"
     "        # ========== Web 终端（#18） ==========",
     'web.py 不含 passwall 路由'),

    ('update.js 不再展示本地版本（4 处全替换）',
     'web/update.js',
     "${esc(d.local)}",
     "无",
     'update.js 展示本地版本（模板里渲染'),

    # ⚠️ 锚点要写**当前源码里的原文**。i18n 之后文案被包成了 ${t('中断')}，
    #    旧锚点（裸 '中断'）再也匹配不上 → 注入静默跳过 → 反向验证
    #    「看起来跑过」，其实这条判据根本没被验（2026-10-08 实测）。
    #    判据本身走 read()，会剥掉 t() 包装，所以只影响**注入锚点**。
    ('update.js 去掉中断按钮',
     'web/update.js',
     'id="upd-cancel-task"',
     'id="upd-cancel-task-gone"',
     'update.js 有中断按钮（查元素本身'),

    ('update.js 去掉备份说明',
     'web/update.js',
     "<li><b>${t('先自动备份')}</b>${t('：调用面板自带的备份能力，把当前配置存一份。 备份不成功就')}"
     "<b>${t('直接中止更新')}</b>${t('，不会带着风险往下走。')}</li>",
     '<li>${t(\'随便更新一下。\')}</li>',
     'update.js 措辞提到备份（弹窗说明里'),

    ('update.css 引用不存在的变量且无 fallback',
     'web/update.css',
     '.upd-err { color:var(--txt3); font-size:12px;',
     '.upd-err { color:var(--no-such-var); font-size:12px;',
     '无未定义变量'),

    # ↓ 下面 6 个是 1.0.10 审查发现并修掉的 P0/P1，
    #   注入点打在**语义层**，验证新加的防线判据真的灵敏
    ('P0-1 模块缓存退化成每次重新加载（中断将失效）',
     'backend/drouter-web.py',
     "            hit = _MOD_CACHE.get(name)\n"
     "            if hit and hit[0] == mt:\n"
     "                return hit[1]",
     "            pass",
     '_load_mod 真的查了缓存'),

    ('P0-2 running 写入移出锁外（并发可起两个 worker）',
     'backend/drouter-update.py',
     "        if read_state().get('running'):\n"
     "            return {'ok': False, 'msg_cn': '已有更新任务在进行中'}\n"
     "        _stop_flag.clear()\n"
     "        _write_state(running=True,",
     "        if read_state().get('running'):\n"
     "            return {'ok': False, 'msg_cn': '已有更新任务在进行中'}\n"
     "    _stop_flag.clear()\n"
     "    _write_state(running=True,",
     '写 running 与检查在同一缩进层'),

    ('P0-3 备份只信 returncode（什么都没备份也报成功）',
     'backend/drouter-update.py',
     "    if r.returncode == 0 and 'BK_CREATED' in out:",
     "    if r.returncode == 0:",
     '_do_backup 校验 BK_CREATED'),

    ('P0-4 apply 白名单移到写库之后（数据已落库才报错）',
     'backend/drouter-web.py',
     "        if module not in APPLY_MODULES:\n"
     "            return self.json({'ok': False, 'code': 'BADMODULE',\n"
     "                              'msg_cn': '未知配置模块'})\n"
     "        # 预检绝不写配置库",
     "        # 预检绝不写配置库",
     'apply_module 的白名单检查在写库之前'),

    ('P1-8 holds 用 .get("data", {}) 导致 None 上再 .get() → 500',
     'backend/drouter-web.py',
     "out['holds'] = (helper('pkg', {'op': 'holds'}).get('data')\n"
     "                        or {}).get('holds', [])",
     "out['holds'] = helper('pkg', {'op': 'holds'}).get('data', {}).get('holds', [])",
     'holds 读取用 or {} 兜底'),

    ('P1-2 _verify 的清理移出 finally（chrony 临时文件永久残留 /etc）',
     'backend/drouter-helper.py',
     "    finally:\n"
     "        # 这里才是「任何路径都一定会走到」的位置\n"
     "        try:\n"
     "            os.remove(tmp)\n"
     "        except Exception:\n"
     "            pass",
     "    if False:\n        pass",
     '_verify 的临时文件清理在 finally 里'),

    ('P1-3 _verify 临时文件名退回固定（并发互相覆盖）',
     'backend/drouter-helper.py',
     "            tmp = '%s.%d.%d.tmp' % (_vt, os.getpid(), _tid)",
     "            tmp = _vt",
     '_verify 临时文件名唯一'),

    # ↓ 1.0.10 下载源降级链的防线
    # 注入：把降级链的 for 循环改成空 —— 等于永远只用第一个源，
    # 且一旦第一个源失败就直接 return False（没有任何降级）。
    ('下载不再走降级链（改回硬编码单个 URL）',
     'backend/drouter-update.py',
     "    for src_name, tmpl in DL_SOURCES:",
     "    for src_name, tmpl in []:",
     '下载走降级链函数'),

    ('清单不再走降级链（降级下载将无法校验）',
     'backend/drouter-update.py',
     "    for src_name, tmpl in DL_SOURCES:\n"
     "        url = tmpl.format(repo=REPO, ver=ver.lstrip('v'), name=name)",
     "    for src_name, tmpl in []:\n"
     "        url = tmpl.format(repo=REPO, ver=ver.lstrip('v'), name=name)",
     '清单也走降级链'),

    ('状态里不再记录用的源（前端无法显示）',
     'backend/drouter-update.py',
     "        _write_state(src=src, url=info,\n"
     "                     msg_cn='正在从 %s 下载…' % src)",
     "        _write_state(url=info,",
     '状态里记录实际用的源'),
    # 注入：把**整个镜像分支删掉**（含 tag warn 那个 span）。
    # ⚠️ 只把 `mirror` 改成 `false` 不够 —— tag warn 的字符串还在，
    #    判据照样匹配得到（第一版就这么写的，白跑一轮）。
    ('前端不再显示下载源（用户不知道走的是镜像）',
     'web/update.js',
     # 2026-10-06：三段都包了 t()，锚点要跟着改成新形态
     ("    if (s.src) {\n"
      "      const mirror = /镜像|proxy/i.test(s.src);\n"
      "      meta += (mirror\n"
      "        ? `<span class=\"tag warn\">${t('下载源：')}${esc(updSrc(s.src))}</span>`\n"
      "          + `<span style=\"color:var(--txt3)\">${t('　官方直连不通，已自动改用镜像；')}`\n"
      "          + t('文件完整性仍会按官方 SHA256 校验</span>')\n"
      "        : `<span class=\"tag ok\">${t('下载源：')}${esc(updSrc(s.src))}</span>`);\n"
      "    }\n", ""),
     None,
     'update.js 在进度区显示下载源'),

    ('P2-6 版本号校验退回前缀匹配（路径穿越）',
     'backend/drouter-update.py',
     "    if not re.fullmatch(r'\\d+\\.\\d+(\\.\\d+)?([-+~][\\w.]+)?', ver):",
     "    if not re.match(r'^\\d+\\.\\d+', ver):",
     None),   # 这条由真跑套件覆盖，静态判据暂不查

    # ↓ 1.0.10 用户反馈后新增的两组
    # ⚠️ 判据查「bindForce 与 bindRetry **两处都有** toast」，
    #    注入也必须**两处都删**。
    #    ⚠️⚠️ 元组的格式是「**若干个 (锚点, 替换) 对**」，
    #    我第一版误写成「(old, new)」两个元素 —— 结果注入把 A2 当成了替换文本，
    #    实际什么也没删，判据当然绿。手工删两处验证过：判据会如实变红（rc=1）。
    ('去掉「重新检查」的 toast 反馈（用户提的原问题回归）',
     'web/update.js',
     (("      await renderCard();\n"
       "      // ③ toast 让用户确知结果 —— 这一条是这次改进的关键\n"
       "      toast(ok ? msg : msg, ok ? 'ok' : 'err', 4200);",
       "      await renderCard();\n      void msgA;"),
      ("      toast(msg, ok ? 'ok' : 'err', 4200);",
       "      void msgB;")),
     None,   # old 是元组时 new 不使用
     '重新检查与失败重试都有 toast 反馈'),

    ('去掉「检查中」提示（按钮文字会被 innerHTML 重建冲掉）',
     'web/update.js',
     # 2026-10-06：已包 t()
     "      if (w) w.textContent = t('正在连接 GitHub 查询最新版本…');",
     "      if (w) w.textContent = '';",
     '重新检查有「进行中」提示'),

    ('去掉「上次检查」时间戳',
     'web/update.js',
     # 2026-10-06：已包 t()（形态是 ${t('上次检查：')}${esc(whenTxt)}）
     "          <span class=\"upd-when\" id=\"upd-when\">${t('上次检查：')}${esc(whenTxt)}",
     "          <span class=\"upd-when\" id=\"upd-when\"></span><!-- ${esc(whenTxt)}",
     '卡片常驻显示上次检查时间'),

    ('已连接时长去掉 boot_id 判断（重启后 uptime 归零会算出负数）',
     'backend/drouter-helper.py',
     "    if rec.get('boot') != boot:",
     "    if False:",
     '用 boot_id 区分重启'),

    ('已连接时长去掉状态持久化（关浏览器就归零）',
     'backend/drouter-helper.py',
     "    db = os.path.join('/var/lib/drouter', 'link-since.json')",
     "    db = '/tmp/link-since.json'  # 注入：改成易失路径",
     '状态持久化到文件'),

    ('去掉可信度标记（变成显示裸数字）',
     'backend/drouter-helper.py',
     "            'uptime_s': up_sec, 'uptime_trusted': up_trusted,",
     "            'uptime_s': up_sec,",
     '已连接时长必须带可信度标记'),
]


def run():
    r = subprocess.run([PY, JUDGE], capture_output=True, cwd=ROOT)
    return r.returncode, (r.stdout or b'').decode('utf-8', 'replace')


def main():
    print('基线（应全绿）：')
    rc, out = run()
    tail = out.strip().splitlines()[-1] if out.strip() else ''
    print('  rc=%d  %s' % (rc, tail))
    if rc != 0:
        print('✘ 基线就是红的，先修好再谈反向验证')
        print(out[-2000:])
        return 1

    bad = []
    for case in CASES:
        desc, rel, old, new, kw = case[:5]
        if kw is None:
            # 显式标记「本条不由静态判据覆盖，跳过」——
            # 比悄悄留一个错关键词好，后者会被当成「判据失效」。
            print('  跳过 %-46s （由真跑套件覆盖）' % desc)
            continue
        p = os.path.join(ROOT, rel)
        with open(p, 'rb') as f:
            orig = f.read().decode('utf-8')
        # ⚠️ old 的格式演进（踩了两次才搞对）：
        #    v1  str            → 单个锚点
        #    v2  (old, new)     → 我误以为的「多组替换」，实际被当成
        #                         一对，于是 A2 被当替换文本，什么也没删
        #    v3  ((o1,n1),(o2,n2)) → 正确：若干个 (锚点, 替换) 对
        #    所以这里**递归**拆：遇到嵌套就往下剥一层。
        pairs = []

        def _collect(x):
            if isinstance(x, (tuple, list)) and len(x) == 2 \
                    and isinstance(x[0], (tuple, list)):
                for it in x:
                    _collect(it)
            elif isinstance(x, (tuple, list)) and len(x) == 2:
                pairs.append((x[0], x[1]))
            else:
                pairs.append((x, new))

        _collect(old)
        missing = [o2 for o2, _n2 in pairs if not isinstance(o2, str)
                   or o2 not in orig]
        if missing:
            print('  跳过 %-46s （锚点未命中）' % desc)
            bad.append(desc + ' 锚点未命中')
            continue
        try:
            patched = orig
            for o2, n2 in pairs:
                patched = patched.replace(o2, n2)
            with open(p, 'wb') as f:
                f.write(patched.encode('utf-8'))
            rc, out = run()
            # 必须因为「这一条」而红，不是碰巧别的红了
            caused = (rc != 0) and (kw in out)
            if caused:
                print('  ok   %-46s → 如约变红' % desc)
            else:
                print('  FAIL %-46s → rc=%d 关键词命中=%s'
                      % (desc, rc, kw in out))
                bad.append(desc)
        finally:
            # ⚠️ 必须无条件还原：抛异常会留下污染，基线就废了
            with open(p, 'wb') as f:
                f.write(orig.encode('utf-8'))

    rc, out = run()
    tail = out.strip().splitlines()[-1] if out.strip() else ''
    print()
    print('还原后基线：rc=%d  %s' % (rc, tail))
    if rc != 0:
        bad.append('还原后未恢复全绿')
        print(out[-1500:])

    print()
    print('反向验证 %d 例，异常 %d 例' % (len(CASES), len(bad)))
    for b in bad:
        print('  ! %s' % b)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
