#!/bin/bash
# =============================================================================
# 离线依赖打包：把 drouter 的全部依赖（含传递依赖）预下载成一个自洽目录，
# 供**没有外网**的目标机离线安装。
#
#   bash scripts/make-offline-deps.sh [输出目录]
#
# 产出目录结构：
#   debs/         所有依赖的 .deb（含 libc6 这类最底层传递依赖）
#   Packages.gz   由 dpkg-scanpackages 生成的本地索引（没有就退化为无索引模式）
#   install.sh    离线安装脚本（先装依赖，再装 drouter 本体）
#
# 为什么不能只 apt-get download 一串包名：
#   `apt-get download a b c` 只下载**指定**的那些包，不拉传递依赖。
#   目标机上 dpkg -i *.deb 会因为 libxxx 没装而大面积失败。必须先把
#   apt-cache depends --recurse 展开成闭包，再整体下载。
#
# 为什么用 apt-cache depends 而不是 apt-rdepends：
#   apt-rdepends 不是 Debian 基础系统自带的（要额外装），而 apt-cache 一定有。
#   `apt-cache depends --recurse --no-recommends --no-suggests ...` 的输出
#   已经能拿到完整闭包，不必引入外部依赖。
#
# 用法（目标机）：
#   把整个目录拷过去，执行
#     sudo bash install.sh drouter_1.0.4_all.deb
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HERE/dist/offline-deps}"

# 与 backend/drouter-helper.py 的 DEPS「必需项 + 常用可选项」保持一致。
# 手动同步点：改了 DEPS 就要同步这里，_dev/t-deps.py 会核对关键包是否都在。
# 注意：samba / nfs-kernel-server / docker.io / cups / snmpd / smartmontools / xfce4
# 会拉起常驻服务或体积巨大，不进离线包，用户在界面上按需安装即可。
DEPS="python3 nftables dnsmasq radvd dhcpcd-base ppp pppoe pppoeconf miniupnpd chrony
ethtool iperf3 mtr-tiny traceroute dnsutils conntrack bridge-utils iproute2
sqlite3 logrotate rsyslog curl wget procps sudo openssl ipset kmod systemd
adduser ca-certificates tcpdump jq htop lm-sensors dmidecode
e2fsprogs exfatprogs ntfs-3g dosfstools xfsprogs btrfs-progs f2fs-tools
util-linux usbutils tar coreutils hostname udev
etherwake zip unzip p7zip-full iputils-ping vim-tiny"

command -v apt-get >/dev/null 2>&1 || { echo "需要 apt-get（Debian/Ubuntu）"; exit 1; }

echo "== 1/4 准备目录 =="
rm -rf "$OUT"
mkdir -p "$OUT/debs"
cd "$OUT/debs"

echo "== 2/4 展开传递依赖闭包 =="
# --no-recommends / --no-suggests：把可选依赖排除掉，否则 4GB 机器光依赖就装爆。
# 过滤规则：只保留顶格的包名行（依赖关系行前面有空格和「依赖:」等前缀）。
#
# ⚠️ 这里有一个必须显式补的坑：`Essential: yes` 的包**不会**出现在
# apt-cache depends --recurse 的输出里 —— apt 认定「任何 Debian 系统都必有它们」，
# 所以从不把它们列为别人 的依赖。实测缺失的有：dash / diffutils / libc-bin /
# grep / findutils / base-files / base-passwd / dpkg / coreutils / sed / tar …
# 在正常的 Debian 上装没问题（本机已有了），但如果目标机是**刚 debootstrap
# 出来的极简根**或换了个发行版，缺了它们的表现是：
#     dpkg: error: 3 expected programs not found in PATH or not executable
#     （缺 sh / diff / ldconfig —— 分别来自 dash / diffutils / libc-bin）
# 而且这个错在装 drouter 本体时才暴露，排查起来很费劲。所以显式补一份。
ESSENTIAL="${DROUTER_ESSENTIAL:-base-files base-passwd bash coreutils dash diffutils
dpkg grep gzip findutils hostname libc-bin libc6 libgcc-s1 libselinux1 libpcre2-8-0
libacl1 libattr1 libcap2 libaudit1 libaudit-common sed tar zlib1g liblzma5 libbz2-1.0
xz-utils libtinfo6 libreadline8 libmount1 libblkid1 libsmartcols1 libuuid1
libsystemd0 libudev1 libzstd1 libcap-ng0 libcrypt1 libgmp10 libnettle8 libhogweed6
libidn2-0 libunistring5 libtasn1-6 libp11-kit0 libffi8 libgnutls30 libgpg-error0
libgcrypt20 libseccomp2 libstdc++6 libmd0 libedit2 libncursesw6 libfdisk1
libapparmor1 libxxhash0 libcom-err2 libext2fs2 libjansson4 libargon2-1
debianutils debconf init-system-helpers mount util-linux passwd login.defs
sysvinit-utils mawk perl-base readline-common sensible-utils}"

# 再补一层「基线库」。踩过的坑：apt 自己的依赖链（libapt-pkg / liblz4-1 /
# libgnutls30 / libnettle8 / libstdc++6 …）**不会**被 drouter 的依赖闭包带进来 ——
# 因为 apt 在任何 Debian 上都已装好，它的依赖也被视为已满足。
# 后果是：在**没有 apt 的干净根 / 换发行版**里跑 install.sh 时，
# 「建立本地仓库索引」这一步会直接报
#     apt-get: error while loading shared libraries: liblz4.so.1
# 然后整条安装链路退化成 dpkg 乱装。补上这几个库成本极小（几百 KB），
# 换来的是「任何 Debian 用户态环境都能装」。
BASELIBS="${DROUTER_BASELIBS:-liblz4-1 libapt-pkg7.0 libgnutls30 libnettle8
libhogweed6 libstdc++6 libgmp10 libgnutls30 libunistring5 libidn2-0 libp11-kit0
libffi8 libtasn1-6 libssl3t64 zlib1g liblzma5 libzstd1 libbz2-1.0 libpcre2-8-0
libselinux1 libmount1 libblkid1 libuuid1 libcap2 libcap-ng0 libaudit1
libsystemd0 libudev1 libseccomp2 libgpg-error0 libgcrypt20}"

# ---------------------------------------------------------------------------
# 闭包求解：用 apt 自己算，不要用 `apt-cache depends --recurse` 做文本展开。
#
# 踩过的坑（很隐蔽，装到最后一步才炸）：
#   `apt-cache depends --recurse` 会把**虚拟包的每一个提供者**都展开成一行。
#   systemd / systemd-standalone-sysusers / opensysusers 三者都 Provides
#   `systemd-sysusers`，同时又互相 Conflicts。文本展开会把三家**全**收进来，
#   下载没问题；但装上时 apt 解冲突发现无解：
#     E: Unmet dependencies. Try 'apt --fix-broken install'
#     systemd : 预依赖 libsystemd-shared 但是它将不会被安装
#   看起来像「离线包缺包」，实际是一堆互相冲突的替代品被硬凑在一起。
#
#   `apt-get install --print-uris -y <目标>` 走的是**真正的依赖求解器**：
#   它会按 Conflicts/Provides 选一套自洽的方案，只给出确实要装的包。
#   这才是「离线包」该用的闭包来源。
# ---------------------------------------------------------------------------
CLOSURE_FILE="$OUT/.closure"
: > "$CLOSURE_FILE"

# ⚠️ 关键：求解必须在一个**空的 dpkg status** 上做，不能直接用宿主机的状态。
# 踩过的坑：在装了一堆东西的开发机上求解，apt 会认为「这些依赖已满足」而不列出来
# （libicu76 / liburcu8t64 / python3-dbus 就是这么漏的 —— 宿主机上有，
#  于是不进闭包；到了干净目标机上就变成
#  `Depends: libicu76 but it is not installable`，整包装不上）。
# 所以先造一个只有「空 status」的临时 apt 根，让 apt 以为什么都没装。
EMPTYROOT="$OUT/.solveroot"
rm -rf "$EMPTYROOT"
mkdir -p "$EMPTYROOT/var/lib/dpkg" "$EMPTYROOT/var/lib/apt/lists/partial" \
         "$EMPTYROOT/var/cache/apt/archives/partial" "$EMPTYROOT/etc/apt/apt.conf.d" \
         "$EMPTYROOT/etc/apt/preferences.d" "$EMPTYROOT/etc/apt/sources.list.d" \
         "$EMPTYROOT/etc/apt/trusted.gpg.d" "$EMPTYROOT/usr/share/keyrings" \
         "$EMPTYROOT/var/lib/dpkg/info" "$EMPTYROOT/var/lib/dpkg/updates"

# 签名校验要用到 keyring，否则 apt 会拒绝所有源：
#   E: 仓库 “…” 没有数字签名。  （trusted.gpg.d 不是目录）
# 只借「公钥」目录即可，不复制 status —— status 必须是空的，那是本步的目的。
cp -f /etc/apt/trusted.gpg.d/* "$EMPTYROOT/etc/apt/trusted.gpg.d/" 2>/dev/null || true
cp -f /usr/share/keyrings/* "$EMPTYROOT/usr/share/keyrings/" 2>/dev/null || true
# apt 自身仍从宿主机运行，签名校验走宿主 keyring 也通常可行，这里只是兜底。
: > "$EMPTYROOT/var/lib/dpkg/status"
touch "$EMPTYROOT/etc/apt/apt.conf.d/.keep"

# 复用宿主机的源列表（只是用来读 Packages 索引，不会真的下载）
[ -f /etc/apt/sources.list ] && cp -f /etc/apt/sources.list "$EMPTYROOT/etc/apt/" 2>/dev/null || true
mkdir -p "$EMPTYROOT/etc/apt/sources.list.d"
cp -f /etc/apt/sources.list.d/*.list "$EMPTYROOT/etc/apt/sources.list.d/" 2>/dev/null || true
cp -f /etc/apt/sources.list.d/*.sources "$EMPTYROOT/etc/apt/sources.list.d/" 2>/dev/null || true

SOLVER_OPTS=(
  -o "Dir=$EMPTYROOT/"
  -o "Dir::State=$EMPTYROOT/var/lib/apt"
  -o "Dir::State::status=$EMPTYROOT/var/lib/dpkg/status"
  -o "Dir::Cache=$EMPTYROOT/var/cache/apt"
  -o "Dir::Etc::sourcelist=$EMPTYROOT/etc/apt/sources.list"
)
echo "  在空 status 上求解依赖（模拟全新目标机）…"
# update 失败不致命：说明宿主机源列表不可用，但本地 Packages 索引可能还在，
# 后面的 -s 求解会自己报出真正的问题，这里不吞错也不中止。
apt-get "${SOLVER_OPTS[@]}" update -qq >/dev/null 2>&1 || \
  echo "  （索引刷新失败，继续用已有索引求解）"

# 求解：只列 Inst 行的包名。--no-install-recommends 与发布口径一致。
if apt-get "${SOLVER_OPTS[@]}" -s --no-install-recommends -y install $DEPS \
      >"$OUT/.sim.txt" 2>"$OUT/.sim.err"; then
  sed -n 's/^Inst \([^ ]*\) .*/\1/p' "$OUT/.sim.txt" >> "$CLOSURE_FILE"
  echo "  依赖求解成功（apt 求解器 / 空 status）"
else
  echo "  ⚠ 空 status 求解失败，回退到宿主状态求解"
  if apt-get -s --no-install-recommends -y install $DEPS >"$OUT/.sim.txt" 2>/dev/null; then
    sed -n 's/^Inst \([^ ]*\) .*/\1/p' "$OUT/.sim.txt" >> "$CLOSURE_FILE"
    echo "  已用宿主状态求解"
  else
    echo "  ⚠ 仍失败，回退到文本展开（可能含冲突替代品）"
    apt-cache depends --recurse --no-recommends --no-suggests \
        --no-conflicts --no-breaks --no-replaces --no-enhances \
        $DEPS 2>/dev/null \
      | grep -E '^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$' >> "$CLOSURE_FILE"
  fi
fi
rm -f "$OUT/.sim.txt" "$OUT/.sim.err"

ALL=$(cat "$CLOSURE_FILE" | sort -u)
ALL="$ALL $ESSENTIAL $BASELIBS"
ALL=$(echo $ALL | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ')

# 显式排掉已知的「冲突替代品」：它们是 systemd-sysusers 的多余提供者，
# 只要 systemd 在，就不能同时出现。留着必然导致上面那个 Unmet dependencies。
for drop in systemd-standalone-sysusers opensysusers systemd-standalone-tmpfiles; do
  if [ -n "$(ls "$OUT"/debs/${drop}_*.deb 2>/dev/null)" ]; then
    echo "  （移除冲突替代品：$drop）"
    rm -f "$OUT"/debs/${drop}_*.deb
  fi
  ALL=$(echo "$ALL" | tr ' ' '\n' | grep -vx "$drop" | tr '\n' ' ')
done

# 过滤掉纯粹是虚拟/元包而下载不到实体的名字
REAL=""
for p in $ALL; do
  if apt-get download --print-uris "$p" >/dev/null 2>&1; then
    REAL="$REAL $p"
  fi
done
ALL="$REAL"
echo "  闭包内可下载的包：$(echo $ALL | wc -w) 个"

echo "== 3/4 下载（只下不装）=="
# 分批下载：命令行过长会在老 shell 上踩 ARG_MAX，且失败时更容易定位是哪个包。
# 陷阱：apt-get download 的落地目录取自**进程的当前目录**，不是 -o 参数；
# 早期写法用 `xargs -a partfile apt-get download`——xargs 在部分实现下会以
# 自己的 CWD 起子进程，结果 apt 把 .deb 往 / 根目录写，全部「权限不够」。
# 改成显式 for 循环 + 显式 cd，且每批都重新确认 CWD，绝不让 xargs 起进程。
DEBDIR="$OUT/debs"
echo "$ALL" | tr ' ' '\n' | grep -v '^$' > "$OUT/.allpkgs"
BATCH=40
total=$(wc -l < "$OUT/.allpkgs")
i=0
while [ "$i" -lt "$total" ]; do
  batch=$(sed -n "$((i+1)),$((i+BATCH))p" "$OUT/.allpkgs")
  # shellcheck disable=SC2086
  ( cd "$DEBDIR" && apt-get download $batch 2>&1 | grep -E '^E:' | head -3 ) || true
  i=$((i + BATCH))
done
rm -f "$OUT/.allpkgs"
N=$(ls -1 "$DEBDIR"/*.deb 2>/dev/null | wc -l)
echo "  已下载 $N 个 .deb（约 $(du -sh "$DEBDIR" | cut -f1)）"
[ "$N" -gt 0 ] || { echo "✘ 一个都没下到，检查网络/源"; exit 1; }

echo "== 4/4 生成本地索引与安装脚本 =="
# 有 dpkg-scanpackages（dpkg-dev 提供）就生成真正的本地仓库索引，
# 这样目标机可以用 `apt-get install -o Dir::Etc::sourcelist=... drouter` 一次装齐。
cd "$OUT"
if command -v dpkg-scanpackages >/dev/null 2>&1; then
  # ⚠️ 索引必须落在 **debs/** 里，和 install.sh 里那条
  #     `deb [trusted=yes] file:$HERE/debs ./` 对上。
  # 踩过的坑：原来生成到 $OUT/Packages.gz，而 apt 去 $OUT/debs/ 找 Packages，
  # 结果 `E: Failed to fetch file:.../debs/./Packages  File not found`，
  # 整条「本地仓库」链路静默退化成 dpkg -i 乱装。
  ( cd debs && dpkg-scanpackages -m . /dev/null 2>/dev/null > Packages )
  # 同时给一份 gzip 版：apt 优先用 Packages.gz，没有才用 Packages。
  # 两个都放，避免不同 apt 版本偏好不一致又踩一次。
  [ -s debs/Packages ] && gzip -9c debs/Packages > debs/Packages.gz
  echo "  已生成 debs/Packages(+.gz)（本地仓库索引）"
  HAS_IDX=1
else
  echo "  未装 dpkg-dev，跳过 Packages，改用 dpkg -i 逐个安装"
  HAS_IDX=0
fi

cat > "$OUT/install.sh" <<'EOS'
#!/bin/bash
# =============================================================================
# drouter 离线安装脚本
#
#   sudo bash install.sh [drouter_x.y.z_all.deb]
#
# 不传参数则在当前目录里自动找 drouter_*.deb。
# 做三件事：① 装齐全部依赖；② 装 drouter 本体；③ 打印后续步骤。
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="${1:-}"

yesno() { printf '  → %s' "$1"; }

if [ "$(id -u)" -ne 0 ]; then
  # 先切到 sudo 再回来执行自己，免得用户忘了加 sudo 跑到一半才失败
  exec sudo -E bash "$0" "$@"
fi

# 1) 找本体
if [ -z "$PKG" ]; then
  PKG=$(ls -1 "$HERE"/drouter_*_all.deb "$HERE"/*/drouter_*_all.deb 2>/dev/null | head -1 || true)
fi
[ -n "$PKG" ] && [ -f "$PKG" ] || PKG="$HERE/$PKG"
[ -f "$PKG" ] || { echo "✘ 找不到 drouter 安装包（用法：bash install.sh drouter_1.0.4_all.deb）"; exit 1; }
echo "安装包：$PKG"

echo ""
echo "== 1/3 安装离线依赖 =="
# 优先走本地仓库模式：让 apt 自己算依赖顺序，比 dpkg -i 一通乱装可靠得多。
if [ -f "$HERE/Packages.gz" ] && command -v apt-get >/dev/null 2>&1; then
  echo "  使用本地仓库索引安装（推荐）"
  tmp_list=$(mktemp)
  trap 'rm -f "$tmp_list"' EXIT
  cat > "$tmp_list" <<EOF
deb [trusted=yes] file:$HERE/debs ./
EOF
  apt-get -o Dir::Etc::sourcelist="$tmp_list" \
          -o Dir::Etc::sourceparts=/dev/null \
          -o APT::Get::List-Cleanup=0 \
          -o Acquire::Languages=none \
          update -qq 2>&1 | tail -2 || true
  # 先把闭包里的包整体装掉（--no-install-recommends 避免又去网上拉推荐包）
  DEBLIST=$(ls -1 "$HERE"/debs/*.deb 2>/dev/null | sed 's|.*/||; s|_.*||' | sort -u)
  # shellcheck disable=SC2086
  apt-get -o Dir::Etc::sourcelist="$tmp_list" \
          -o Dir::Etc::sourceparts=/dev/null \
          -o APT::Get::List-Cleanup=0 \
          -o Acquire::Languages=none \
          --no-install-recommends -y install $DEBLIST 2>&1 | tail -6 || true
else
  echo "  无本地索引，逐个 dpkg -i（可能报依赖未满足，最后会用 -f 补齐）"
  dpkg -i "$HERE"/debs/*.deb 2>&1 | tail -4 || true
  apt-get --no-download -y -f install 2>&1 | tail -4 || true
fi

echo ""
echo "== 2/3 安装 drouter 本体 =="
dpkg -i "$PKG" 2>&1 | tail -6 || apt-get --no-download -y -f install 2>&1 | tail -6

echo ""
echo "== 3/3 完成 =="
echo "  · 查看状态：sudo drouter-ctl status"
echo "  · 打开面板：https://<本机IP>:8443/   （默认账号 admin / admin123，请尽快改掉）"
echo "  · 首次部署建议在界面里先走「新手向导」"
EOS
chmod +x "$OUT/install.sh"

# 把本体也放进离线目录，方便整包分发
if ls "$HERE"/dist/drouter_*.deb >/dev/null 2>&1; then
  cp -f "$HERE"/dist/drouter_*.deb "$OUT/" 2>/dev/null || true
fi

# 清掉构建期的中间产物。踩过的坑：`.closure`（包名列表）和 `.solveroot/`
# （空 status 求解用的临时 apt 根）会被原样打进 tar 包，用户解包后会看到两个
# 莫名其妙的隐藏目录 —— 既占地方又让人困惑「这是不是漏了什么」。
rm -rf "$OUT/.solveroot" "$OUT/.closure" "$OUT/.allpkgs" \
       "$OUT/.sim.txt" "$OUT/.sim.err" 2>/dev/null || true

echo ""
echo "======================================================================"
echo "离线包已生成：$OUT"
echo "  体积：$(du -sh "$OUT" | cut -f1)   （依赖 $(ls -1 "$OUT"/debs/*.deb 2>/dev/null | wc -l) 个 deb）"
echo "  分发：把整个目录拷到目标机，执行"
echo "        sudo bash install.sh drouter_1.0.4_all.deb"
echo "======================================================================"
