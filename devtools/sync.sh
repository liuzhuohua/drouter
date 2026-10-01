#!/bin/bash
# 把本地 router-build 源码同步到目标机 /tmp/drouter-src 并执行 deploy.sh
# 不会启动任何网络服务、不会改网络。部署后用 build_probe.sh 自检。
set -e
HERE="C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00"
SRC="$HERE/router-build"
RSH="$HERE/router-build/devtools/rsh.sh"

echo "=== 0. 本地预检（语法 + 编译）==="
cd "$SRC"
# Python 用 py_compile 才能抓出 "global 声明位置错误" 这类只有编译期才报的 bug
PYBIN="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODEBIN="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
"$PYBIN" - <<'PY' || { echo "❌ Python 预检失败"; exit 1; }
import py_compile, tempfile, sys, glob
bad = 0
for f in sorted(glob.glob('backend/*.py')):
    try:
        py_compile.compile(f, cfile=tempfile.mktemp(), doraise=True)
        print('  编译OK:', f)
    except Exception as e:
        print('  编译失败:', f, e); bad += 1
sys.exit(1 if bad else 0)
PY
"$NODEBIN" --check web/app.js && echo "  语法OK: web/app.js"
# 后端内部契约：GET /api/config 的模块清单必须与 save_config 白名单一致
"$PYBIN" _dev/t-web-contract.py || { echo "❌ 配置模块契约预检失败"; exit 1; }
# 页面 key ↔ VIEWS 映射完整性：缺一个就会让菜单点进去卡在「正在载入…」
"$NODEBIN" devtools/check-views.js web/app.js || { echo "❌ 视图映射预检失败"; exit 1; }
# 离线渲染回归：真正把每个视图函数跑一遍，能抓出「语法没问题但一调用就抛异常」
"$NODEBIN" devtools/check-render.js web/app.js || { echo "❌ 渲染回归预检失败"; exit 1; }
# 手机适配要素（#14）：viewport / 抽屉 / 表格横向滚动，防止改版时被悄悄改掉
"$NODEBIN" devtools/check-mobile.js || { echo "❌ 手机适配预检失败"; exit 1; }
# 入口页破缓存：部署后用户不手动强刷也能拿到新前端
"$PYBIN" _dev/t-cachebust.py || { echo "❌ 缓存版本号预检失败"; exit 1; }
# 历史累计流量（概览页），含「计数器回绕 / 重启清零」容错
"$PYBIN" _dev/t-nettotals.py || { echo "❌ 累计流量预检失败"; exit 1; }
# 环境契约：守护进程 PATH 归一化 + 虚拟网卡归类（都是「接口 ok 但功能悄悄不工作」）
"$PYBIN" _dev/t-env-path.py || { echo "❌ 环境契约预检失败"; exit 1; }
# 打包产物结构自检：deb 元数据 / 维护脚本 / Dockerfile（挡住「打出来的包不能用」）
"$PYBIN" _dev/t-packaging.py || { echo "❌ 打包自检失败"; exit 1; }
# 防火墙日志「为什么一条都没有」的状态判定：规则未载入 / 开关未开必须分开提示
"$PYBIN" _dev/t-fwlog-state.py || { echo "❌ 防火墙日志状态预检失败"; exit 1; }
# 外置存储（USB/Type-C/雷电）：格式化的三重保护是最容易造成不可逆损失的地方
"$PYBIN" _dev/t-storage.py || { echo "❌ 外置存储预检失败"; exit 1; }
# NAT 检测详情：后端是对象、前端曾当纯文本渲染成 "[object Object]"，别再回退
"$PYBIN" _dev/t-nat-detail.py || { echo "❌ NAT 详情渲染预检失败"; exit 1; }
# Web 终端（真 PTY）：helper/shelld/web 三端的协议契约 + 超时分级
"$PYBIN" _dev/t-webshell.py || { echo "❌ Web 终端契约预检失败"; exit 1; }
# PTY 可用性：/dev/ptmx 丢失时内核只报一句「out of pty devices」，
# 而 kernel.pty.nr 是 0（一个都没用），排查起来极其费时。
# 这里钉住「启动自愈 + 失败重试 + 可定位的诊断信息」三件事。
"$PYBIN" _dev/t-pty.py || { echo "❌ PTY 可用性预检失败"; exit 1; }
# 配置预检分诊：dnsmasq 报「seed the random number generator」时，要翻译成
# 「本机 /dev 节点缺失」这种能直接定位的提示，而不是把内核英文原样抛出
"$PYBIN" _dev/t-verify.py || { echo "❌ 配置预检分诊失败"; exit 1; }
# 终端模拟器行为：ANSI 光标 / 颜色 / 换行 / 分帧中文，在 node 里真跑一遍前端代码
"$NODEBIN" _dev/t-webterm.js || { echo "❌ 终端模拟器预检失败"; exit 1; }
# 依赖自检清单：一键安装绝不能把 xfce4 / samba / docker 这类可选大件一起装上
"$PYBIN" _dev/t-deps.py || { echo "❌ 依赖清单预检失败"; exit 1; }
# 全项目静态审计：动作名未注册 / 前端调了后端没有的路由 / 同名函数重复定义 /
# CSS 类缺失。这类 bug 语法检查全过，只在点进去那一刻才爆。
"$PYBIN" _dev/t-audit.py || { echo "❌ 全项目静态审计失败"; exit 1; }
# 快照覆盖率与容错：一个 0600 的 root 文件就让整个 /etc/drouter 进不了快照，
# 而界面还显示「已创建快照」——这种静默失败必须挡住。
"$PYBIN" _dev/t-snapshot.py || { echo "❌ 快照覆盖预检失败"; exit 1; }
# 常驻执行守护（#7）：socket 快路径 + subprocess 回退，两者都不能少。
# 少了回退，守护一挂面板就全废；少了快路径，每个请求白付 236ms 启动开销。
"$PYBIN" _dev/t-helpd.py || { echo "❌ 常驻执行守护预检失败"; exit 1; }
# 统一日志：conntrack -E 是阻塞事件流，曾经让每次查询固定白等 2 秒
"$PYBIN" _dev/t-ulog-perf.py || { echo "❌ 统一日志性能预检失败"; exit 1; }
# 响应压缩与 ETag 协商缓存：少了 Vary 会让代理把 gzip 喂给不支持的客户端
"$PYBIN" _dev/t-gzip.py || { echo "❌ 响应压缩预检失败"; exit 1; }
# 模块联动：前端「保存/应用」要处理的模块，后端必须接得住。
# 曾经 system / pppoe / portfwd 三个页面点了应用直接弹「未知的模块」。
"$PYBIN" _dev/t-linkage.py || { echo "❌ 模块联动预检失败"; exit 1; }
# 常驻守护重启契约：deploy.sh 里 shelld 的指纹取样必须在 install 之前，
# 否则「代码变了就重启」永远不触发，改了守护代码真机上还是旧行为。
"$PYBIN" _dev/t-daemon-restart.py || { echo "❌ 守护重启契约预检失败"; exit 1; }
# 磁盘与日志清理：这是唯一会真删文件的模块，安全边界表 + glob 不重叠必须每次验。
# 边界表漏一项就是误删配置库，glob 重叠则会让页面虚报可释放量。
"$PYBIN" _dev/t-cleanup.py || { echo "❌ 磁盘清理预检失败"; exit 1; }
# 内核转发与加速：默认值是我替用户做的判断，改错会直接改变这台机器的网络行为；
# 渲染器侧的 masquerade / MSS 也必须跟着校验，否则页面开了但规则集里没有。
"$PYBIN" _dev/t-kern.py || { echo "❌ 内核转发预检失败"; exit 1; }
# Docker 引擎配置：这一页会覆写 /etc/docker/daemon.json，合并逻辑是唯一的防线 ——
# 用户自己写的 data-root / insecure-registries 被静默丢掉是最糟的体验；
# 保存流程里多一个 systemctl 就会在用户不知情时中断所有容器。
"$PYBIN" _dev/t-dcfg.py || { echo "❌ Docker 引擎配置预检失败"; exit 1; }
# 打印服务：CUPS 与 USB RAW 抢同一个设备，切模式时没把对面停干净就是两边都打不出；
# cupsctl --remote-any 写的 Allow all 必须收窄，否则等于把打印机暴露给所有来源。
"$PYBIN" _dev/t-print.py || { echo "❌ 打印服务预检失败"; exit 1; }

# AC/AP 管理中心：升级或改端口绝不能重新生成加密密钥（历史配置会解不开）；
# 安装包是外部下载的，解包必须挡住 zip slip。
"$PYBIN" _dev/t-opensoho.py || { echo "❌ AC/AP 管理中心预检失败"; exit 1; }
# CA 证书管理：部署是唯一能把用户关在门外的动作 —— 换证书前必须过四道闸，
# 换完必须握手比对指纹，不对就自动还原；有效期/SAN 的 falsy 陷阱也要每次验。
"$PYBIN" _dev/t-ca.py || { echo "❌ CA 证书管理预检失败"; exit 1; }
# 新手向导：这是新手唯一会看的页面，填错了他没有任何判断依据。
# 地址池「圈进本机自己」这类错要在服务端就拦住 —— 不拦的表现是网络时通时不通，
# 且没有任何日志提示原因。另外「保存账号」绝不能顺带拨号（会在用户不知情下断网）。
"$PYBIN" _dev/t-wizard.py || { echo "❌ 新手向导预检失败"; exit 1; }

# 性能回归：单文件 SPA 最容易悄悄退化的三类问题 —— 切页漏停定时器（后端被空打）、
# 高频读接口里滥 fork subprocess（2 核机上每次切页面白烧几十毫秒）、
# 写了参数却两个分支一样（quick 形同虚设）。这类问题不影响功能，只咬性能，
# 功能测试永远抓不到，必须单独钉住。
"$PYBIN" _dev/t-perf.py || { echo "❌ 性能回归预检失败"; exit 1; }

# 文档链接：README 是项目的门面，一张图挂了就是一个白框，而且这种问题
# 只在 GitHub 网页上才看得出来（本地 markdown 预览器很多会静默忽略）。
# 截图从桌面搬进 docs/screenshots/ 时最容易漏改扩展名或文件名，
# 所以把「文档里写的路径 → 文件是否真的存在」钉成预检项。
"$PYBIN" devtools/check-md-links.py || { echo "❌ 文档链接预检失败"; exit 1; }

# 截图覆盖度：功能页 ↔ 截图 ↔ README 引用 三者必须对得上。
# 加功能页忘截图、补了图忘登记、重命名漏一张，这三类漂移都是静默的，
# 必须靠这条预检抓。缺图只提示不拦截（截图本来就可以慢慢补）。
"$PYBIN" devtools/check-shots.py || { echo "❌ 截图覆盖度预检失败"; exit 1; }

# 换行符检查：Windows 上写出的 CRLF 到了 Linux 会让 bash 报 "$'\r': 未找到命令"，
# 整个 deploy.sh 会静默跑成一堆错误。必须在打包前拦住。
# 注意：不能用 grep —— Git Bash 的 grep 在文本模式下会把 LF 读成 CRLF，全是误报。
#
# 只扫 git 已追踪的文件：未追踪的本地临时产物（如 _preview-shots.html）
# 根本不会进仓库、也不会被 deploy.sh 上传，为它们报错是纯噪音。
"$PYBIN" - <<'PY' || { echo "❌ 换行符预检失败"; exit 1; }
import subprocess, sys
out = subprocess.run(['git', 'ls-files'], capture_output=True, text=True).stdout
bad = [f for f in out.splitlines()
       if f.endswith(('.sh', '.py', '.js', '.css', '.html'))
       and b'\r' in open(f, 'rb').read()]
if bad:
    print('以下文件含 CRLF 换行，Linux 上会执行失败：')
    for f in bad:
        print('   ' + f)
    print('修正：python3 -c "open(f,\'wb\').write(open(f,\'rb\').read().replace(b\'\\r\\n\',b\'\\n\'))"')
    sys.exit(1)
print('  换行符OK: 已追踪的脚本全部为 LF')
PY

echo "=== 打包源码 ==="
# docs 也要带上：deploy.sh 会装到 /opt/drouter/docs（systemd 的 Documentation 指向它）
tar czf /tmp/drouter-src.tgz backend web scripts docs 2>/dev/null
ls -la /tmp/drouter-src.tgz

echo "=== 上传 ==="
"$HERE/router-build/devtools/rscp.sh" /tmp/drouter-src.tgz ajeef@192.168.7.3:/tmp/drouter-src.tgz 2>&1 | tail -3

echo "=== 解包 + 部署 ==="
"$RSH" "rm -rf /tmp/drouter-src && mkdir -p /tmp/drouter-src && tar xzf /tmp/drouter-src.tgz -C /tmp/drouter-src" 2>&1 | tail -3
bash "$HERE/router-build/devtools/rsudo.sh" "bash /tmp/drouter-src/scripts/deploy.sh" 2>&1 | tail -45
