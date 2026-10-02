#!/bin/bash
# =============================================================================
# Drouter 自包含离线安装脚本
#
#   sudo bash install.sh [drouter_x.y.z_all.deb]
#
# 设计目标：**目标机零外网**也要能一次装完。
# 做法是先把本地 debs/ 目录注册成一个 file: 源，让 apt 自己去算依赖顺序 ——
# 比 `dpkg -i debs/*.deb` 一通乱装可靠得多（dpkg 不看顺序，装到一半就会
# 因为依赖没满足而大面积失败）。
#
# 会自动 sudo：不加 sudo 直接跑也能用。
# =============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"

# ---- 提权 ----
if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限，正在通过 sudo 重新执行…"
  exec sudo -E bash "$0" "$@"
fi

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✘ %s\033[0m\n' "$*"; exit 1; }

# ---------------------------------------------------------------------------
# 根前缀（仅 chroot 验收用，真实目标机上为空、行为完全不变）。
#
# 为什么需要：在不设根前缀的 chroot 里，apt/dpkg 读的是**宿主机**的
# /var/lib/dpkg/status，于是会出现「apt 说 hostname 已经是最新版、chroot 里
# 其实没有」这种鬼打墙；最后 drouter 本体因为
# `Package hostname is not installed` 装不上，看起来像离线包缺包。
#
# 用法（验收脚本）：DROUTER_ROOT=/tmp/xxx bash install.sh
# 真实目标机：不设该变量 → ROOT_PREFIX 为空 → 走原路径，零差异。
# ---------------------------------------------------------------------------
ROOT_PREFIX=""
APT_ROOT_OPTS=()
# 注意 `${X%/}` 对 "/" 会得到空串，会被误判成「没设置」。
# chroot 内的正确用法恰恰就是 DROUTER_ROOT=/（此时 chroot 的 / 已是目标根），
# 所以要单独判一次原值，而不是判剥掉斜杠后的结果。
if [ -n "${DROUTER_ROOT:-}" ]; then
  if [ "$DROUTER_ROOT" = "/" ] || [ "$DROUTER_ROOT" = "" ]; then
    ROOT_PREFIX=""
    echo "  根前缀：/（chroot 内，直接使用当前根）"
  else
    ROOT_PREFIX="${DROUTER_ROOT%/}"
    [ -d "$ROOT_PREFIX/var/lib/dpkg" ] || die "DROUTER_ROOT 指向的目录不是有效根：$ROOT_PREFIX"
    APT_ROOT_OPTS=(
      -o "Dir=$ROOT_PREFIX/"
      -o "Dir::State=$ROOT_PREFIX/var/lib/apt"
      -o "Dir::State::status=$ROOT_PREFIX/var/lib/dpkg/status"
      -o "Dir::Cache=$ROOT_PREFIX/var/cache/apt"
    )
    echo "  根前缀：$ROOT_PREFIX（chroot 模式，apt/dpkg 库都指向该根）"
  fi
fi

# ---------------------------------------------------------------------------
step "0/4 环境自检"
# ---------------------------------------------------------------------------
[ -d "$HERE/debs" ] || die "找不到 debs/ 目录 —— 解包不完整？"
NDEB=$(ls -1 "$HERE"/debs/*.deb 2>/dev/null | wc -l)
[ "$NDEB" -gt 0 ] || die "debs/ 里一个 .deb 都没有"
echo "  依赖包：$NDEB 个"

# 架构必须在下载依赖时那台机器一致，否则装了也起不来
if command -v dpkg >/dev/null 2>&1; then
  MYARCH=$(dpkg --print-architecture)
  case "$HERE" in
    *-"$MYARCH") : ;;
    *) warn "本机架构是 $MYARCH，但包名里写的是别的 —— 可能不匹配" ;;
  esac
fi

if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "  系统：${PRETTY_NAME:-unknown}"
  case "${VERSION_CODENAME:-}" in
    trixie) : ;;
    '')     warn "识别不出 VERSION_CODENAME，跳过版本核对" ;;
    *)      warn "依赖是按 Debian 13 (trixie) 下载的，本机是 ${VERSION_CODENAME}，"
            warn "版本对不上的包可能装不上。继续尝试…" ;;
  esac
fi

# 磁盘检查：200+ 个 deb 解包后要占几百 MB
# 用 df -Pk + cut 而不是 awk —— awk 不是 Essential 包，极简系统上可能没有，
# 装包脚本自己先因为「找不到 awk」而报错就太讽刺了。
AVAIL_KB=$(df -Pk /var 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f4)
if [ -n "${AVAIL_KB:-}" ] && [ "$AVAIL_KB" -lt 1500000 ] 2>/dev/null; then
  warn "根分区可用空间不足 1.5G（当前 $((AVAIL_KB/1024))M），安装可能失败"
fi

# ---------------------------------------------------------------------------
step "1/4 找安装包"
# ---------------------------------------------------------------------------
PKG="${1:-}"
if [ -z "$PKG" ]; then
  PKG=$(ls -1 "$HERE"/drouter_*_all.deb 2>/dev/null | head -1 || true)
fi
if [ -z "$PKG" ]; then
  die "目录里找不到 drouter_*_all.deb（用法：bash install.sh drouter_1.0.7_all.deb）"
fi
[ -f "$PKG" ] || PKG="$HERE/$PKG"
[ -f "$PKG" ] || die "找不到安装包：$PKG"
echo "  本体：$(basename "$PKG")"

# ---------------------------------------------------------------------------
step "2/4 安装依赖（走本地 file: 源，零外网）"
# ---------------------------------------------------------------------------
# 关键点：用一份**只指向本地 debs/** 的临时 sourcelist，
# 并同时把 Dir::Etc::sourceparts 指到 /dev/null —— 否则系统里原有的
# 外网源还会被读进来，apt 会去联网找"更新的版本"，无外网时就卡住/报错。
TMPLIST=$(mktemp /tmp/drouter-src.XXXXXX.list)
trap 'rm -f "$TMPLIST"' EXIT

cat > "$TMPLIST" <<EOF
deb [trusted=yes] file:$HERE/debs ./
EOF

APT_OPTS=(
  -o "Dir::Etc::sourcelist=$TMPLIST"
  -o "Dir::Etc::sourceparts=/dev/null"
  -o "APT::Get::List-Cleanup=0"
  -o "Acquire::Languages=none"
  -o "Acquire::Retries=0"
)
# chroot 模式下必须把整个 apt/dpkg 状态根也搬过去，
# 否则 apt 会去读宿主机的 status，判断「已装」和「未装」全是错的。
if [ -n "$ROOT_PREFIX" ]; then
  APT_OPTS+=("${APT_ROOT_OPTS[@]}")
else
  APT_OPTS+=( -o "Dir::State::lists=$HERE/.aptlists" )
fi

if [ -n "$ROOT_PREFIX" ]; then
  mkdir -p "$ROOT_PREFIX/var/lib/apt/lists/partial" "$ROOT_PREFIX/var/cache/apt/archives/partial" 2>/dev/null || true
else
  mkdir -p "$HERE/.aptlists/partial"
fi

# 索引自检：debs/ 里必须有 Packages 或 Packages.gz，否则 apt 会报
#   `E: Failed to fetch file:.../debs/./Packages  File not found`
# 然后「本地仓库」这条链路就静默退化成 dpkg -i 乱装（顺序不对就装不上）。
# 老版本的 make-offline-deps.sh 把索引生成到了上一级目录，正是这个症状；
# 这里顺手自愈一下，兼容用户手上可能存在的旧包。
if [ ! -f "$HERE/debs/Packages" ] && [ ! -f "$HERE/debs/Packages.gz" ]; then
  if [ -f "$HERE/Packages.gz" ]; then
    echo "  （索引在上级目录，已自动搬到 debs/ 下）"
    mv -f "$HERE/Packages.gz" "$HERE/debs/Packages.gz"
  elif [ -f "$HERE/Packages" ]; then
    echo "  （索引在上级目录，已自动搬到 debs/ 下）"
    mv -f "$HERE/Packages" "$HERE/debs/Packages"
  else
    warn "debs/ 里没有 Packages 索引，将退化为 dpkg -i 逐个安装"
  fi
fi

echo "  建立本地仓库索引…"
if ! apt-get "${APT_OPTS[@]}" update -qq 2>&1 | tail -3; then
  warn "本地索引建立有问题，继续尝试（下面的 install 会再校验一次）"
fi

# 把 debs/ 里所有包名拿去 install —— apt 会自动只选本地源里有的，
# 且按依赖顺序装。已经装过且版本相同的会跳过。
DEBLIST=$(cd "$HERE/debs" && ls -1 *.deb 2>/dev/null | sed 's/_.*//' | sort -u | tr '\n' ' ')
echo "  从本地源安装 $NDEB 个依赖包（已装过的会跳过）…"

# dpkg 也要跟着根前缀走。chroot 下不传 --root 会把包装到宿主机上（灾难）。
DPKG_ROOT_OPTS=()
[ -n "$ROOT_PREFIX" ] && DPKG_ROOT_OPTS=( --root="$ROOT_PREFIX" --force-not-root )

if ! DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" \
        --no-install-recommends -y install $DEBLIST 2>&1 | tail -8; then
  warn "apt 批量安装报错，改用 dpkg 逐个兜底"
  dpkg "${DPKG_ROOT_OPTS[@]}" -i "$HERE"/debs/*.deb 2>&1 | tail -5 || true
  # 再让 apt 用本地源把没配好的补一遍
  apt-get "${APT_OPTS[@]}" -y -f install 2>&1 | tail -5 || true
fi

# ---------------------------------------------------------------------------
step "3/4 安装 drouter 本体"
# ---------------------------------------------------------------------------
# postinst 里可能调用 systemctl，无外网不影响，但如果目标机没有 systemd
# 会报错，所以失败不致命 —— 最后单独报告。
if ! DEBIAN_FRONTEND=noninteractive dpkg "${DPKG_ROOT_OPTS[@]}" -i "$PKG" 2>&1 | tail -10; then
  warn "本体安装过程有报错，尝试用本地源补依赖"
  apt-get "${APT_OPTS[@]}" -y -f install 2>&1 | tail -8 || true
fi

# ---------------------------------------------------------------------------
step "4/4 结果"
# ---------------------------------------------------------------------------
OPT_ROOT="${ROOT_PREFIX}/opt/drouter"
if [ -f "$OPT_ROOT/backend/drouter-helper.py" ]; then
  MODS=$(ls -1 "$OPT_ROOT"/backend/*.py 2>/dev/null | wc -l)
  printf '\033[1;32m  ✅ 安装成功\033[0m（后端模块 %s 个）\n' "$MODS"

  # 服务状态（没 systemd 就跳过）
  if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
    ST=$(systemctl is-active drouter-web 2>/dev/null || echo unknown)
    echo "  drouter-web 服务：$ST"
  fi

  # 取本机 IP。优先 iproute2；没有就退回 hostname -I（更老但到处都有）。
  # 两者都不可用时留空，不影响安装结果。
  IP=""
  if command -v ip >/dev/null 2>&1; then
    IP=$(ip -4 -o addr show scope global 2>/dev/null | tr -s ' ' | cut -d' ' -f4 | cut -d/ -f1 | head -1)
  fi
  if [ -z "$IP" ] && command -v hostname >/dev/null 2>&1; then
    IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | head -1)
  fi
  PORT=$(cat /etc/drouter/web-port 2>/dev/null || echo 8443)
  cat <<EOF

  下一步：
    1) 浏览器访问 https://${IP:-<本机IP>}:${PORT}/
       （自签证书，浏览器会提示不安全，点"继续访问"）
    2) 用 admin / admin123 登录，**第一件事改密码**
    3) 左侧菜单第一项「新手向导」会带你配好外网/内网/DNS/IPv6

  命令行工具：
    sudo drouter-ctl status      查看状态
    sudo drouter-ctl webport 8443  改 Web 端口

EOF
else
  die "没装上 —— 看上面的报错。完整日志：bash install.sh 2>&1 | tee /tmp/install.log"
fi
