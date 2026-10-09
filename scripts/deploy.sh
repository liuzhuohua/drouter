#!/bin/bash
# Debian13 主路由系统 —— 部署脚本
# 将后端、前端、systemd 服务、sudoers 白名单安装到位。
# 不会启动任何网络服务，不会修改网络配置。
set -e

# 源码目录可用 DROUTER_SRC 覆盖（一键脚本 drouter-ctl.sh 会传入实际路径）
SRC="${DROUTER_SRC:-/tmp/drouter-src}"
OPT=/opt/drouter
UNIT_WEB=drouter-web

echo "=== 1. 创建目录结构 ==="
mkdir -p $OPT/backend $OPT/web $OPT/data $OPT/snapshots $OPT/certs $OPT/bin $OPT/docs
mkdir -p /etc/drouter/generated
mkdir -p /etc/nftables.d
mkdir -p /var/log/drouter
mkdir -p /etc/dnsmasq.d
mkdir -p /var/lib/drouter/trash      # 文件管理器"回收站"
mkdir -p $OPT/dpi                    # DPI 识别规则库与源码（#25）
mkdir -p /etc/ppp/ip-up.d /etc/ppp/ip-down.d   # PPPoE 多拨策略路由钩子（#23）
# helper 私有临时目录：/run 为 tmpfs（重启自清），且不归低权用户所有，
# 避开 /tmp、/var/tmp 的 sticky 位与 fs.protected_regular=2 写保护。
mkdir -p /run/drouter-helper
chmod 700 /run/drouter-helper

sha_of() { [ -f "$1" ] && sha1sum < "$1" | cut -d' ' -f1 || echo "-"; }
# shelld 是常驻守护，代码在内存里，不重启就是旧行为。
# 判断依据必须是「本次源码指纹」vs「上次真正重启过的指纹」（记在 mark 文件里），
# 不能拿 $SRC 和 $OPT 比 —— install 之后两边同源，cmp 恒等，永远不重启。
# 也不能拿 install 前的 $OPT 比 —— 若上一轮已经把新文件拷进去了但没重启成功，
# 这一轮两边又相等，漏网。
SHELLD_NEW=$(sha_of "$SRC/backend/drouter-shelld.py")
SHELLD_MARK=/var/lib/drouter/shelld.deployed.sha

echo "=== 2. 安装后端 ==="
install -m 0644 $SRC/backend/render.py          $OPT/backend/render.py
install -m 0644 $SRC/backend/drouter-helper.py  $OPT/backend/drouter-helper.py
install -m 0644 $SRC/backend/drouter-web.py     $OPT/backend/drouter-web.py
install -m 0644 $SRC/backend/theme.py           $OPT/backend/theme.py
# drouter-logd：统一日志采集守护。helper 写出的 drouter-logd.service 直接
# ExecStart 这个文件，漏装会导致日志定时器起不来（此前确实漏了，务必保留）。
install -m 0644 $SRC/backend/drouter-logd.py    $OPT/backend/drouter-logd.py
install -m 0644 $SRC/backend/drouter-snapshotd.py $OPT/backend/drouter-snapshotd.py
install -m 0644 $SRC/backend/drouter-rescue.py  $OPT/backend/drouter-rescue.py
# drouter-shelld：Web 真·PTY 终端的会话守护。它必须由常驻进程持有 PTY
# （helper 每次都是跑完就退出的新进程，PTY fd 跨不了请求），漏装则终端连不上。
install -m 0644 $SRC/backend/drouter-shelld.py  $OPT/backend/drouter-shelld.py
install -m 0644 $SRC/backend/drouter-helpd.py  $OPT/backend/drouter-helpd.py
# 三个定时任务守护（1.0.7）。它们的 systemd 单元由 helper 在用户保存设置时
# 现写，ExecStart 指向的就是下面这三个文件 —— 漏装 = 定时器起来就报 203/EXEC。
install -m 0644 $SRC/backend/drouter-backupd.py $OPT/backend/drouter-backupd.py
install -m 0644 $SRC/backend/drouter-alertd.py  $OPT/backend/drouter-alertd.py
install -m 0644 $SRC/backend/drouter-quotad.py  $OPT/backend/drouter-quotad.py
# DDNS 定时更新守护（1.0.8）。drouter-ddns.service/.timer 不在这里预置：
# 周期取自用户填的「检测间隔」，由 helper 的 _write_ddns_timer() 运行时生成。
install -m 0644 $SRC/backend/drouter-ddnsd.py  $OPT/backend/drouter-ddnsd.py

# 版本检测/自更新（1.0.10）。它不是守护进程，是被 drouter-web.py 按路径
# importlib 加载的模块 —— 文件名带连字符，不是合法 Python 标识符，
# 所以 web.py 里用 spec_from_file_location 加载，**不要**改成普通 import。
# 这里必须装：漏了的话 web.py 每次调 /api/update/* 都会抛异常。
install -m 0644 $SRC/backend/drouter-update.py $OPT/backend/drouter-update.py

# 主题之家（#12）：自定义主题目录 + 生效主题标记
mkdir -p /etc/drouter/themes
# 只在「还没有生效主题」时播种默认值。
# 这里原来是无条件 `echo default > active-theme` —— 每次部署都把用户自己
# 挑的主题打回默认，用户只会看到「部署完样式莫名其妙变回去了」。
# 容器版 docker-init.sh 一直是「首次为空时才写」，两处行为保持一致。
if [ ! -f /etc/drouter/active-theme ]; then
  echo "default" > /etc/drouter/active-theme
fi
chmod 644 /etc/drouter/active-theme
# 记录的生效主题如果指向一个已不存在的主题（自定义主题目录被删/id 写错），
# 界面会一直挂着一个「不存在」的生效主题，且只能手改文件才出得来。这里兜底回退。
python3 - <<'PY' || true
import importlib.util
spec = importlib.util.spec_from_file_location(
    'drouter_helper', '/opt/drouter/backend/drouter-helper.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
tid = h._theme_read_active()
if h._theme_find(tid) is None:
    h._theme_write_active('default')
    print('生效主题 %r 已不存在，已回退 default' % tid)
PY

echo "=== 3. 安装前端 ==="
install -m 0644 $SRC/web/index.html $OPT/web/index.html
install -m 0644 $SRC/web/app.css    $OPT/web/app.css
install -m 0644 $SRC/web/app.js     $OPT/web/app.js
# ⚠️ 新增的前端模块（1.0.10）必须在这里逐个登记 —— 这一段是**逐个 install**，
#    不是通配符，漏登记 = 真机上文件不存在 = 页面 JS 404。
#    2026-10-04 首次部署就踩了：drouter-update.py 装上了，
#    但 update.js / update.css / upstream.js 三个都没装，概览页直接坏掉。
#    加新的 web/*.js|css 时**必须同步这里**，t-audit 的部署判据会检查。
for wf in update.js update.css upstream.js netdetail.js realtime.js i18n.js; do
  [ -f "$SRC/web/$wf" ] || { echo "✘ 源码缺少 web/$wf"; exit 1; }
  install -m 0644 "$SRC/web/$wf" "$OPT/web/$wf"
done
if [ -f "$SRC/web/logo.svg" ]; then
  install -m 0644 $SRC/web/logo.svg $OPT/web/logo.svg
fi
# 版本号（1.0.9 新增）。页面底部的版本号与 /api/openapi 的 version 字段
# 都从 /opt/drouter/VERSION 读。缺了不报错，只是永远显示后端的兜底值 ——
# 部署了新版却显示旧版本号，比什么都不做更让人困惑。
# devtools/sync.sh 负责把 packaging/VERSION 打进源码包；源码包里没有就跳过。
if [ -f "$SRC/packaging/VERSION" ]; then
  install -m 0644 $SRC/packaging/VERSION $OPT/VERSION
  echo "  版本号 $(cat $OPT/VERSION)"
fi

echo "=== 3.5 安装运维脚本（自检/回归用）==="
if [ -d "$SRC/scripts" ]; then
  install -d -m 0755 $OPT/scripts
  for s in "$SRC"/scripts/*.sh; do
    [ -f "$s" ] || continue
    install -m 0755 "$s" "$OPT/scripts/$(basename "$s")"
  done
  echo "已安装脚本：$(ls $OPT/scripts | tr '\n' ' ')"
fi

echo "=== 3.6 安装文档（systemd unit 的 Documentation= 指向这里）==="
if [ -d "$SRC/docs" ]; then
  for d in "$SRC"/docs/*; do
    [ -f "$d" ] || continue
    install -m 0644 "$d" "$OPT/docs/$(basename "$d")"
  done
  echo "已安装文档：$(ls $OPT/docs | tr '\n' ' ')"
fi

echo "=== 4. 创建专用低权用户 drouter ==="
if ! id drouter >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -d $OPT -M drouter
  echo "已创建系统用户 drouter"
else
  echo "用户 drouter 已存在"
fi

echo "=== 5. 权限设置 ==="
chown -R drouter:drouter $OPT/data $OPT/snapshots $OPT/certs
chmod 750 $OPT/data $OPT/snapshots $OPT/certs
chmod +x $OPT/backend/drouter-helper.py
chmod +x $OPT/backend/drouter-snapshotd.py
chmod +x $OPT/backend/drouter-rescue.py
chown root:root $OPT/backend/drouter-helper.py
chown -R root:root $OPT/backend
chown -R root:root $OPT/web
chmod 755 /var/log/drouter
chmod 755 /etc/drouter/generated
chown -R drouter:drouter /var/lib/drouter 2>/dev/null || true
chmod -R 750 /var/lib/drouter 2>/dev/null || true

echo "=== 6. sudoers 白名单（仅放行 helper，且限定参数形态）==="
cat > /etc/sudoers.d/drouter <<'SUDO'
# drouter：后端仅可通过该脚本以 root 执行白名单操作
# 参数被严格限制为 action + JSON 字符串
drouter ALL=(root) NOPASSWD: /usr/bin/python3 /opt/drouter/backend/drouter-helper.py *
SUDO
chmod 0440 /etc/sudoers.d/drouter
visudo -c -f /etc/sudoers.d/drouter >/dev/null && echo "sudoers 语法检查通过"

echo "=== 7. 日志轮转配置 ==="
cat > /etc/logrotate.d/drouter <<'LR'
/var/log/drouter/*.log /var/log/drouter/*.jsonl {
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    create 0644 root root
}
LR

echo "=== 8. systemd 服务单元 ==="
cat > /etc/systemd/system/drouter-web.service <<'UNIT'
[Unit]
Description=drouter 主路由管理后台
Documentation=file:/opt/drouter/docs/
After=network-online.target
Wants=network-online.target
Before=shutdown.target

[Service]
Type=simple
User=drouter
Group=drouter
WorkingDirectory=/opt/drouter
# systemd 默认 PATH 不含 /usr/sbin，而 nft/dnsmasq/radvd 都在那儿。
# 后端虽已自行归一化 PATH，这里再显式声明一次，避免任何遗漏路径静默失败。
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-web.py
Restart=on-failure
RestartSec=5
# 资源限制：适配 2 核 / 3.9G 小内存环境
MemoryMax=160M
MemoryHigh=120M
CPUQuota=40%
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=5
# 安全加固
# 说明：后端以低权用户 drouter 运行，写 /etc 由 root helper 完成（白名单 action）。
# 注意：这里必须 ProtectSystem=false —— 「依赖自检 / 一键安装」需要通过 apt/dpkg
# 往 /usr/bin、/var/cache/apt、/var/lib/dpkg 写入；若把 /usr 挂为只读，
# apt 会报 "只读文件系统" 而安装失败。安全边界由 helper 的 action 白名单保证：
# 非白名单动作一律拒绝，且所有参数逐个强校验。
NoNewPrivileges=false
PrivateTmp=false
ProtectSystem=false
ProtectHome=read-only
ReadWritePaths=/opt/drouter/data /opt/drouter/snapshots /opt/drouter/certs /var/log/drouter
# 额外保护：这些路径不给写（即使 helper 被滥用也改不到）
InaccessiblePaths=/boot
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drouter-web

[Install]
WantedBy=multi-user.target
UNIT

echo "=== 8.5 自动快照定时器 + 紧急救援通道服务单元 ==="
# 自动快照：默认「开启」，每 6 小时一次。真实参数由界面写入
# /etc/drouter/snapshot.conf，helper 保存策略时会重建本单元。
cat > /etc/systemd/system/drouter-snapshot.service <<'SVC'
[Unit]
Description=drouter 自动配置快照
After=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-snapshotd.py
# oneshot 打完包就退出，但 tar 大目录时也会有内存尖峰，给个上限兜底
MemoryMax=300M

[Install]
WantedBy=multi-user.target
SVC

cat > /etc/systemd/system/drouter-snapshot.timer <<'TMR'
[Unit]
Description=drouter 自动快照定时器

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true
Unit=drouter-snapshot.service

[Install]
WantedBy=timers.target
TMR

# 紧急救援通道：默认「关闭」。只有用户在界面上勾选开启后，
# helper 才会创建虚拟网卡别名并 enable/start 本服务。
cat > /etc/systemd/system/drouter-rescue.service <<'RSC'
[Unit]
Description=drouter 紧急救援通道（独立于 WAN/LAN 的应急恢复入口）
After=network.target
Wants=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-rescue.py
Restart=always
RestartSec=5
# 救援通道是「最后的入口」：本身只是个小 HTTP 服务，几十 MB 足够。
# 给它也装上内存闸 —— 真到内存泄漏那天，不能让它把本就紧张的机器拖死
MemoryMax=150M
StandardOutput=journal
StandardError=journal
NoNewPrivileges=false
ProtectSystem=false

[Install]
WantedBy=multi-user.target
RSC

# 救援通道默认关闭：不 enable、不 start（避免影响现有局域网）
echo "救援通道单元已就位（默认关闭，需在界面显式开启）"

# 常驻执行守护（#7 性能优化）：把「每个请求 spawn 一次 python3+sudo」的
# 236ms 固定开销去掉。以 root 运行，但只监听 Unix socket（0660 root:drouter），
# 不占任何 TCP 端口；drouter-web 连不上它时会自动回退到 subprocess，所以它
# 挂掉只会变慢、不会让面板不可用。
cat > /etc/systemd/system/drouter-helpd.service <<'HPD'
[Unit]
Description=drouter 特权动作常驻执行守护
Documentation=file:/opt/drouter/docs/
After=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=simple
User=root
Group=root
# 刻意不用 RuntimeDirectory：见 drouter-shelld.service 里的说明。
# /run/drouter 由守护自己 mkdir 并 chmod 0755（drouter-web 要能进目录连
# helper.sock，而 socket 自身是 0660 root:drouter，权限并没有放宽）。
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-helpd.py
Restart=on-failure
RestartSec=2
# 常驻 import 了整套 helper，比其它守护占得多；上限防内存泄漏拖垮路由器
MemoryMax=300M
# 故意不设 CPUQuota：它要跑格式化大硬盘这类子进程，限 CPU 会把这些操作拖慢
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drouter-helpd
ProtectHome=read-only
PrivateTmp=false
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
HPD
echo "常驻执行守护单元已就位"

# Web 终端守护（#4 重做）：真 PTY 会话必须由常驻进程持有。
# 说明为什么不用 socket 激活：helper 在连不上时会自动 systemctl start 拉起，
# 但那要等几秒；预启动能让用户点「连接」时立刻进入 shell。
# 它以 root 运行（要开 root shell），但只监听 0600 的 Unix socket，不占任何端口。
cat > /etc/systemd/system/drouter-shelld.service <<'SHD'
[Unit]
Description=drouter Web 终端 PTY 会话守护
Documentation=file:/opt/drouter/docs/
After=network.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Type=simple
User=root
Group=root
# 刻意不用 RuntimeDirectory=drouter：systemd 会在「最后一个声明它的单元停止」
# 时把整个目录删掉重建。两个守护共用 /run/drouter，于是 restart helpd 的瞬间
# systemd 重建目录，把 shelld 的 shell.sock 一起删了 —— shelld 依旧 active，
# socket 却没了，用户点终端只看到「守护未运行」。改成两个守护各自 mkdir。
ExecStart=/usr/bin/python3 /opt/drouter/backend/drouter-shelld.py
Restart=on-failure
RestartSec=3
# 只是转字节流，用不了多少内存；上限是防异常输出把内存吃光
MemoryMax=80M
CPUQuota=15%
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drouter-shelld
# 安全加固：不碰系统目录、不给新特权（它本身就以 root 跑，靠 socket 0600 收口）
ProtectHome=read-only
PrivateTmp=false
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
SHD
echo "Web 终端守护单元已就位"

echo "=== 9. 生成 isp-dns 占位文件（dnsmasq resolv-file 引用）==="
# 只在缺失时补占位。这个文件是 pppd 拨号成功后经
# /opt/drouter/scripts/sync-isp-dns.sh 写入的真实运营商 DNS；无条件覆盖会把
# 已经拿到的 DNS 擦掉，在那之后 dnsmasq 的上游解析一直空着，直到下一次拨号
# 才会恢复（表现为「重新部署后网页解析变慢」）。
# 容器版 docker-init.sh 同样是「缺失才写」。
# 脚本本体不再用 heredoc 写进 $OPT/bin/：第 84 行的 install 已经把
# scripts/*.sh 装到 $OPT/scripts/，而 deb / 镜像走的也是同一条规则 ——
# 早先只有 deploy.sh 部署的机器才有这个文件。
if [ ! -f /etc/drouter/generated/isp-dns.conf ]; then
cat > /etc/drouter/generated/isp-dns.conf <<'ISP'
# 由 drouter 自动维护：运营商 PPPoE 下发的 DNS
# 拨号成功后由 pppd 的 usepeerdns 写入 /etc/ppp/resolv.conf，
# 可执行 /opt/drouter/scripts/sync-isp-dns.sh 将其同步到此处。
ISP
fi
# 存量占位文件里可能还留着旧路径（bin/），只换注释行、不碰 nameserver：
# 用户已经拿到的运营商 DNS 不能被部署动作擦掉。
if [ -f /etc/drouter/generated/isp-dns.conf ] \
   && grep -qs 'bin/sync-isp-dns.sh' /etc/drouter/generated/isp-dns.conf; then
  sed -i 's#bin/sync-isp-dns\.sh#scripts/sync-isp-dns.sh#' \
    /etc/drouter/generated/isp-dns.conf
fi

# 存量机器迁移：/etc/drouter/rescue.conf 旧版本写成了 0600 属主 root，
# 以 drouter 身份运行的快照读不到它，一个文件就让整个 /etc/drouter 目录
# 进不了快照（界面却显示「已创建快照」）。这里把它放宽成 0640 属组 drouter。
if [ -f /etc/drouter/rescue.conf ]; then
    chmod 0640 /etc/drouter/rescue.conf 2>/dev/null || true
    chgrp drouter /etc/drouter/rescue.conf 2>/dev/null || true
fi

echo "=== 10. 校验 ==="
# 逐个编译校验：缺文件直接中止部署（避免上线后才发现少装了一个守护进程）
for f in render.py drouter-helper.py drouter-web.py drouter-logd.py \
         drouter-snapshotd.py drouter-rescue.py drouter-shelld.py \
         drouter-helpd.py drouter-backupd.py drouter-alertd.py \
         drouter-quotad.py drouter-ddnsd.py drouter-update.py \
         theme.py; do
    p="$OPT/backend/$f"
    if [ ! -f "$p" ]; then echo "✘ 缺少文件: $p"; exit 1; fi
    # ⚠️ 用 compile() 不是 ast.parse()：后者只做语法分析，抓不到
    # 「重复关键字参数」这类语义错误（1.0.10 实测：静态全绿、import 才炸）
    python3 -c 'import sys; compile(open(sys.argv[1],encoding="utf-8").read(),sys.argv[1],"exec")' "$p" \
        && echo "语法OK: $p" || { echo "✘ 语法错误: $p"; exit 1; }
done
echo "--- 目录 ---"
ls -la $OPT/
echo "--- 服务文件 ---"
systemctl daemon-reload
systemctl is-enabled drouter-web 2>&1 || true
echo "--- 自动快照定时器（默认开启）---"
systemctl enable --now drouter-snapshot.timer 2>&1 | tail -2 || true
systemctl is-active drouter-snapshot.timer 2>&1 || true
echo "--- 紧急救援通道（默认关闭）---"
systemctl is-active drouter-rescue 2>&1 || true
echo "--- 磁盘与日志清理定时器（默认开启）---"
# 单元与入口脚本由 helper 的 cleanup save 动作生成（默认参数即推荐值）。
# 关键点：**只在新机器上才传 enabled=true**。
# _clean_save_op 是合并式保存，无条件传 enabled=true 会把用户在界面上明确
# 关掉的自动清理重新打开 —— 而清理是本项目唯一会真删文件的模块，
# 静默恢复一个「自动删文件」的策略是所有副作用里最不该发生的。
if [ ! -f /etc/drouter/cleanup.conf ]; then
    /usr/bin/python3 $OPT/backend/drouter-helper.py cleanup '{"op":"save","enabled":true}' \
        >/dev/null 2>&1 && echo "清理策略已按推荐值初始化" || echo "⚠ 清理策略初始化失败（界面保存一次即可恢复）"
else
    # 已有策略：只重建单元文件，enabled / trigger / items 全部沿用用户设置
    /usr/bin/python3 $OPT/backend/drouter-helper.py cleanup '{"op":"save"}' \
        >/dev/null 2>&1 && echo "清理策略沿用已有设置（未改动用户开关）" || echo "⚠ 清理单元重建失败（界面保存一次即可恢复）"
fi
systemctl is-active drouter-cleanup.timer 2>&1 || true
echo "--- 常驻执行守护 ---"
# 必须 restart 而不是 enable --now：守护把 helper 代码常驻在内存里，
# 只 enable --now 的话它还在跑上一版代码，改的东西根本不生效。
systemctl enable drouter-helpd 2>&1 | tail -1 || true
systemctl restart drouter-helpd 2>&1 | tail -2 || true
sleep 1
systemctl is-active drouter-helpd 2>&1 || echo "⚠ 常驻执行守护未运行（面板会自动回退到慢路径）"
echo "--- Web 终端守护 ---"
# 重启 shelld 会断掉正在用的终端会话，所以默认只 ensure 起来。
# 但两种情况必须重启：① socket 文件没了（多半是被 helpd 重启连带删掉的）；
# ② 代码确实变了 —— 不重启的话跑的还是旧代码。
mkdir -p /var/lib/drouter
SHELLD_SEEN=$(cat "$SHELLD_MARK" 2>/dev/null || echo "-")
if [ ! -S /run/drouter/shell.sock ] || [ "$SHELLD_NEW" != "$SHELLD_SEEN" ]; then
    echo "终端守护代码有更新（$SHELLD_SEEN → $SHELLD_NEW），重启以生效"
    systemctl restart drouter-shelld 2>&1 | tail -2 || true
    # 只有真的起来了才更新 mark，否则下一轮还会重试重启
    if systemctl is-active --quiet drouter-shelld; then
        echo "$SHELLD_NEW" > "$SHELLD_MARK"
    else
        echo "⚠ 终端守护未能启动，mark 不更新（下一轮部署会再试）"
    fi
else
    systemctl enable --now drouter-shelld 2>&1 | tail -1 || true
    echo "$SHELLD_NEW" > "$SHELLD_MARK"
fi
systemctl is-active drouter-shelld 2>&1 || true
# socket 必须真的存在：systemd 说 active 不代表 socket 已经在盘上
# （restart 是异步的，守护起来后还要自己 mkdir + bind，得给它几秒）。
for _i in 1 2 3 4 5 6 7 8 9 10; do
    [ -S /run/drouter/shell.sock ] && break
    sleep 1
done
[ -S /run/drouter/shell.sock ] && echo "终端 socket 就位" \
    || echo "⚠ 终端 socket 缺失，Web 终端将不可用"
echo "--- 网络与 VNC 复核 ---"
ip -4 -o addr show ens18 2>/dev/null || echo "(ens18 不存在，跳过)"
ss -lnt | grep 5900 || echo "5900 异常"

# 重启管理后台，让新代码真正生效。
# 必须放在语法校验之后：一旦新代码有语法错误，宁可继续跑旧进程也不能把自己关在门外。
echo "--- 重启管理后台 ---"
systemctl restart $UNIT_WEB 2>&1 | tail -3 || echo "(重启失败，请手动检查)"
sleep 2
systemctl is-active $UNIT_WEB || echo "⚠ drouter-web 未在运行"

# 统一日志采集定时器：默认配置是 enabled=true，但 service/timer 单元只有
# 在界面里点过一次「保存日志设置」才会被 helper 写出来。全新部署后防火墙日志
# 页面会一直是空的，表现为「日志功能没做」。这里补一次同步，让默认开关真正生效。
echo "--- 同步日志采集定时器 ---"
python3 - <<'PY' || echo "(日志定时器同步失败，可在界面「日志设置」里保存一次重建)"
# 文件名带连字符，不能用普通 import，走 importlib 按路径加载
import importlib.util
spec = importlib.util.spec_from_file_location(
    'drouter_helper', '/opt/drouter/backend/drouter-helper.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
r = h.act_logd_sync({'interval_sec': 60})
print(r.get('msg_cn') or r)
PY
systemctl is-active drouter-logd.timer 2>&1 || true

echo "DEPLOY_DONE"
