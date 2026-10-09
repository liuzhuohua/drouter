#!/bin/bash
# =============================================================================
# 构建 drouter 的 .deb 安装包（需在 Debian/Ubuntu 上执行，用到 dpkg-deb）
#
#   bash packaging/build-deb.sh [版本号]
#
# 产物：dist/drouter_<版本>_all.deb
#
# 说明：.deb 本身不内嵌依赖的二进制（那是 apt 的职责），control 里用 Depends
#       声明完整依赖清单，安装时由 apt 自动补齐。若目标机无外网，可先用
#       scripts/make-offline-deps.sh 把依赖预下载成离线包，再离线安装。
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# ⚠️ 版本号的**唯一真源**是 packaging/VERSION，不要在脚本里另写一份。
# 早先 4 个构建脚本各写死一个默认值，改版本时漏改一处就产出一个
# 「文件名是 1.0.9、control 里是 1.0.8」的包 —— apt 会按 control 认版本，
# 表现为 `dpkg -l` 永远显示旧版，升级也升不上去。
VERSION_FILE="$HERE/packaging/VERSION"
[ -f "$VERSION_FILE" ] || { echo "缺少 $VERSION_FILE（版本号唯一真源）"; exit 1; }
VERSION="${1:-$(tr -d ' \t\r\n' < "$VERSION_FILE")}"
[ -n "$VERSION" ] || { echo "$VERSION_FILE 为空"; exit 1; }
DIST="$HERE/dist"
PKG="drouter"
STAGE="$DIST/${PKG}_${VERSION}_all"

command -v dpkg-deb >/dev/null 2>&1 || { echo "需要 dpkg-deb（请在 Debian/Ubuntu 上执行）"; exit 1; }

echo "== 1/6 清理并准备目录 =="
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/drouter/backend" \
         "$STAGE/opt/drouter/web" \
         "$STAGE/opt/drouter/docs" \
         "$STAGE/opt/drouter/scripts" \
         "$STAGE/usr/local/bin" \
         "$STAGE/lib/systemd/system"
mkdir -p "$DIST"

echo "== 2/6 复制程序文件 =="
install -m 0644 "$HERE"/backend/*.py          "$STAGE/opt/drouter/backend/"
install -m 0644 "$HERE"/web/*                 "$STAGE/opt/drouter/web/"
# 文档只装顶层文件，**不能写 `docs/*`**：`docs/screenshots/` 是个目录，
# GNU install 遇到目录会报 "略过目录" 并**返回非 0**，在 set -e 下直接中断整个打包。
# （截图是给 GitHub README 用的，也没必要塞进 deb 里白白撑大体积。）
# 逐项 `[ -f ]` 过滤的写法与 scripts/deploy.sh 保持一致。
for d in "$HERE"/docs/*; do
  [ -f "$d" ] || continue
  install -m 0644 "$d" "$STAGE/opt/drouter/docs/"
done
install -m 0755 "$HERE"/scripts/*.sh          "$STAGE/opt/drouter/scripts/"
# 版本号随包落到 /opt/drouter/VERSION —— 页面底部的版本号和 openapi 的
# version 字段都从它读。不装的话后端只能退回硬编码的兜底值，
# 表现是「deb 装的是 1.0.9，页面却显示 1.0.8」。
install -m 0644 "$VERSION_FILE"                 "$STAGE/opt/drouter/VERSION"
# 一键运维命令：直接随包提供，postinst 里再做一次兜底安装
install -m 0755 "$HERE/scripts/drouter-ctl.sh" "$STAGE/usr/local/bin/drouter-ctl"

# systemd 单元必须随包发。踩过的坑：原先 .deb 里**一个单元都没有** ——
# 单元只在 scripts/deploy.sh 里用 heredoc 写，所以「用 deploy.sh 部署」能用，
# 但「用 .deb 装」的用户装完是一堆文件、没有任何服务，必须再手工跑一遍
# deploy.sh 才行。对 Release 用户来说这是致命的。
# 注意：drouter-logd.service / drouter-qos.service **不在这里** ——
# 它们的内容随用户的日志/限速配置变化，由 helper 在保存配置时动态生成，
# 静态打进包里反而会写错默认值。
install -m 0644 "$HERE"/packaging/deb/units/*.service "$STAGE/lib/systemd/system/"
install -m 0644 "$HERE"/packaging/deb/units/*.timer   "$STAGE/lib/systemd/system/"

echo "== 3/6 写入 DEBIAN 元数据 =="
# 注：DEBIAN/control 是 deb822 格式，不保证支持 "#" 注释行 —— 需要说明的地方
# 一律写在本脚本里，不要写进 control，否则某些 dpkg 工具会解析失败。
#
# 依赖策略：apt 默认会装 Recommends，所以 Recommends 里只放「不自带守护进程」
# 的工具。以下三类一律降级到 Suggests，由用户在界面上按需逐项安装：
#   * smartmontools —— 装完自动拉起 smartd 常驻；
#   * samba / nfs-kernel-server —— 装完会启用 smbd / nfs-server 文件共享服务；
#   * docker.io / docker-compose —— 装完会启用 docker 守护，4GB 机器上很重。
#     注意包名：Debian 13 是 docker-compose（提供 compose v2 插件），
#     docker-compose-v2 是 Ubuntu 的叫法，在 Debian 上装不到。
# 格式化工具（exfatprogs / ntfs-3g / dosfstools / xfsprogs / btrfs-progs /
# f2fs-tools）和 etherwake、zip 等纯工具没有守护进程，放在 Recommends 即可。
sed "s/__VERSION__/$VERSION/" "$HERE/packaging/deb/control" > "$STAGE/DEBIAN/control"
install -m 0755 "$HERE/packaging/deb/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$HERE/packaging/deb/prerm"    "$STAGE/DEBIAN/prerm"
install -m 0755 "$HERE/packaging/deb/postrm"   "$STAGE/DEBIAN/postrm"
# md5sums 让 dpkg 能检测文件被改动
( cd "$STAGE" && find . -path ./DEBIAN -prune -o -type f -print \
    | sed 's|^\./||' | xargs -r md5sum > DEBIAN/md5sums )

echo "== 4/6 校验 =="
# 后端 13 个模块必须齐全，缺一个上线后就是某个功能打不开。
# ⚠️ drouter-ddnsd.py 是 DDNS 定时更新守护（1.0.8 新增）：
#    deb 里**只有这个脚本**，drouter-ddns.service/.timer 是运行时由
#    helper 的 _write_ddns_timer() 生成的（周期取自用户填的检测间隔），
#    所以下面的 systemd 清单里不能出现它们，否则构建会误判「缺少单元」。
for f in render.py drouter-helper.py drouter-web.py drouter-logd.py \
         drouter-snapshotd.py drouter-rescue.py drouter-shelld.py \
         drouter-helpd.py drouter-backupd.py drouter-alertd.py \
         drouter-quotad.py drouter-ddnsd.py drouter-update.py \
         theme.py; do
  [ -f "$STAGE/opt/drouter/backend/$f" ] || { echo "✘ 缺少 $f"; exit 1; }
  # ⚠️ 必须用 compile() 不是 ast.parse()：后者只做语法分析，抓不到
  # 「重复关键字参数」这类语义错误（1.0.10 实测：ast.parse 通过，
  # import 时 SyntaxError）。构建期就崩在这里，别等到真机才发现。
  python3 -c 'import sys; compile(open(sys.argv[1],encoding="utf-8").read(),sys.argv[1],"exec")' \
    "$STAGE/opt/drouter/backend/$f"
done
for wf in app.js app.css index.html update.js update.css upstream.js netdetail.js realtime.js i18n.js; do
  [ -f "$STAGE/opt/drouter/web/$wf" ] || { echo "✘ 缺少 $wf"; exit 1; }
done
# 这里的数字必须等于上面for 里的条目数。写错了不会让构建失败，
# 但会让人以为清单是全的（历史上「7 个」和实际 12 个长期不符）。
echo "  后端模块 14 个、语法全部通过"

# systemd 单元齐全性：少一个就是「某项功能装完不工作」
for u in drouter-web.service drouter-helpd.service drouter-shelld.service \
         drouter-snapshot.service drouter-snapshot.timer drouter-rescue.service; do
  [ -f "$STAGE/lib/systemd/system/$u" ] || { echo "✘ 缺少 systemd 单元 $u"; exit 1; }
done
echo "  systemd 单元 6 个齐全"

echo "== 5/6 构建 =="
dpkg-deb --build --root-owner-group "$STAGE" "$DIST/${PKG}_${VERSION}_all.deb"

echo "== 6/6 产物 =="
ls -lh "$DIST/${PKG}_${VERSION}_all.deb"
dpkg-deb -I "$DIST/${PKG}_${VERSION}_all.deb" | sed -n '1,14p'
echo ""
echo "安装：sudo apt install ./dist/${PKG}_${VERSION}_all.deb"
echo "卸载：sudo apt remove drouter      （保留配置）"
echo "      sudo apt purge  drouter      （连配置一起删）"
