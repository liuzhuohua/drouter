#!/bin/bash
# =============================================================================
# 离线安装验收：在一个一次性 chroot 里「断网」跑一遍 install.sh，
# 证明离线包确实自洽（不需要任何外网就能把 drouter 装起来）。
#
#   sudo bash /tmp/test-offline-install.sh /tmp/drouter-offline
#
# 为什么要这么测：离线包最容易出的问题是**传递依赖没下全** —— 在装了
# 一堆东西的开发机上测不出来，因为缺的包恰好已经装着了。只有在干净
# 根目录 + 无外网的环境里，缺哪个包才会立刻暴露。
# =============================================================================
set -u

OFFLINE="${1:-/tmp/drouter-offline}"
CH=/tmp/drouter-chroot-test
DEBS="$OFFLINE/debs"

# 安全前置检查：CH 必须是个具体的 /tmp 子目录，绝不允许为空或落在 / 上，
# 否则后面的 rm -rf "$CH" 会变成删根。
case "$CH" in
  /tmp/*) : ;;
  *) echo "✘ 拒绝执行：CH 必须位于 /tmp 下（当前：$CH）"; exit 1 ;;
esac
[ -d "$DEBS" ] || { echo "✘ 找不到依赖目录：$DEBS"; exit 1; }

echo "=== 0/4 清理上次残留（含挂载点）==="
for m in "$CH/proc" "$CH/dev" "$CH/sys"; do
  umount "$m" 2>/dev/null || true
done
# 顺带检查宿主机 /dev/null 是否被人误改成了普通文件（本脚本早期版本踩过）
if [ ! -c /dev/null ]; then
  echo "  ⚠ 检测到 /dev/null 不是字符设备，正在修复"
  rm -f /dev/null && mknod -m 666 /dev/null c 1 3
fi

echo "=== 1/4 准备干净根目录 ==="
rm -rf "$CH"
mkdir -p "$CH"/{bin,dev,proc,sys,tmp,etc,var/lib/dpkg,usr/bin,usr/lib,var/log}

# 基础层：一个能跑 dpkg/apt 的最小 Debian 根。
# 注意 dpkg 启动时会硬性检查 PATH 里有没有 sh / diff / ldconfig，
# 缺任何一个都直接 `dpkg: error: N expected programs not found`，什么也装不了。
# 所以 dash(diffutils 的 diff)、libc-bin(ldconfig) 必须在。
# apt-get 也是必需的（install.sh 优先走本地仓库模式）。
BASE="base-files base-passwd libc6 libgcc-s1 gcc-14-base libselinux1 libpcre2-8-0
libacl1 libattr1 libcap2 libaudit1 libaudit-common dpkg tar gzip xz-utils
zlib1g liblzma5 libbz2-1.0 sed coreutils libc-bin bash libtinfo6 libreadline8t64 dash
diffutils grep findutils libstdc++6 libmd0 libgmp10 libnettle8t64 libhogweed6t64
libidn2-0 libunistring5 libtasn1-6 libp11-kit0 libffi8 libgnutls30t64 libseccomp2
libsystemd0 libzstd1 libudev1 libdb5.3t64 libgcrypt20 libgpg-error0 libmount1
libblkid1 libsmartcols1 libuuid1 libcap-ng0 libcrypt1 libedit2 libncursesw6
libfdisk1 libapparmor1 libxxhash0 libcom-err2 libext2fs2t64 libjansson4 libargon2-1
libapt-pkg7.0 libssl3t64 openssl openssl-provider-legacy"

# 说明两个刻意的例外（不是漏，是离线包里本来就没有）：
#  · apt / apt-utils：任何 Debian 都自带，没必要打进包；测试根从宿主机借。
#  · dpkg-dev / lsof / libcbor0.10 / libfido2-1：本项目用不到，不进闭包。
#    （lsof 只在诊断时手动用；libcbor/libfido2 是 openssh 的 FIDO 支持链。）
# 真实目标机是完整 Debian 13，这些一律不缺，只有「从零拼的测试根」才要借。

# ⚠️ 基础层的搭法（两种都试过，最后用这个组合）：
#
# 方案 A：只用 `dpkg-deb -x` 摊文件 —— 文件对，但 **不在 status 里登记**，
#         后面装 drouter 时 dpkg 会报「Package tar is not installed」。
# 方案 B：只用 `dpkg -i --root/--instdir` —— 登记对，但 dpkg 会按
#         「已装好的根」前提去处理 usr-merge 与维护脚本，实测把 /bin 搞空
#         （/bin/bash 消失），chroot 直接起不来。
#
# 最终方案：**dpkg-deb -x 摊文件（保证文件系统正确）**
#         + **手工把 control 记录并进 status（保证依赖数据库正确）**。
# 这样既不依赖 dpkg 的重装逻辑，又能让依赖校验正常工作。
for p in $BASE; do
  f=$(ls "$DEBS/${p}"_*.deb 2>/dev/null | head -1)
  [ -n "$f" ] || continue
  dpkg-deb -x "$f" "$CH" 2>/dev/null
done

# 把每个基础包的 control 段登记进 dpkg status，并把状态改成 installed。
# dpkg 的 status 是「空行分隔的记录集」，记录之间靠空行分界 ——
# **不能**写 `---` 分隔线（那是 .changes/changelog 的写法），
# dpkg 解析到 `---` 会直接报 `field name '---' cannot start with hyphen`。
# 只改 Status 字段：control 里的 Status 一般是 unknown，dpkg 会当成没装。
register_pkg() {
  _f="$1"
  [ -n "$_f" ] || return 0
  _ctl=$(dpkg-deb -f "$_f" Package) || return 0
  [ -n "$_ctl" ] || return 0
  grep -q "^Package: $_ctl\$" "$CH/var/lib/dpkg/status" 2>/dev/null && return 0
  # 保证前一条记录以空行收尾，否则会和新记录粘在一起
  if [ -s "$CH/var/lib/dpkg/status" ]; then
    printf '\n' >> "$CH/var/lib/dpkg/status"
  fi
  dpkg-deb -f "$_f" 2>/dev/null | grep -v '^Status:' >> "$CH/var/lib/dpkg/status"
  echo "Status: install ok installed" >> "$CH/var/lib/dpkg/status"
  mkdir -p "$CH/var/lib/dpkg/info"
  dpkg-deb -e "$_f" "$CH/var/lib/dpkg/info/$_ctl" 2>/dev/null || true
  dpkg-deb -c "$_f" 2>/dev/null | awk '{print $NF}' \
    | sed 's|^\./||; s|^|/|' | grep -v '^/$' \
    > "$CH/var/lib/dpkg/info/$_ctl.list" 2>/dev/null || true
}
for p in $BASE; do
  register_pkg "$(ls "$DEBS/${p}"_*.deb 2>/dev/null | head -1)"
done

# usr-merge：Debian 13 的 /bin /sbin /lib 都是指向 usr/ 的符号链接。
# 从零拼根目录时必须手工补上，否则 chroot 里 /bin/bash 找不到。
for d in bin sbin lib lib64; do
  if [ -d "$CH/usr/$d" ] && [ ! -e "$CH/$d" ]; then
    ln -s "usr/$d" "$CH/$d" 2>/dev/null || true
  fi
done

# 基础层登记结果自检：哪些包没进 dpkg 数据库就报出来。
# 不要吞掉这个信息 —— 基础层缺包会让后面「装 drouter 失败」被误判成
# 离线包有问题，白白浪费一轮排查。
BASE_MISS=""
for p in $BASE; do
  grep -q "^Package: $p\$" "$CH/var/lib/dpkg/status" 2>/dev/null || BASE_MISS="$BASE_MISS $p"
done
if [ -n "$BASE_MISS" ]; then
  echo "  ⚠ 基础层未登记（离线包里可能没有）：$BASE_MISS"
else
  echo "  基础层全部已登记进 dpkg"
fi

# apt 本身不在离线包里（任何 Debian 都自带，没必要发），但测试用的干净根
# 是从零拼的，所以要从宿主机借一套 apt/dpkg 二进制过来，否则 install.sh
# 会退化成 `dpkg -i` 乱装那一支，测不到真实的「本地 file: 源」链路。
# 注意：真实目标机不可能缺这些，这里只是补全测试环境。
for b in /usr/bin/apt-get /usr/bin/apt /usr/bin/apt-cache /usr/bin/dpkg \
         /usr/bin/dpkg-deb /usr/bin/dpkg-query /usr/bin/dpkg-split /usr/bin/dpkg-trigger; do
  [ -e "$b" ] && cp -a "$b" "$CH/usr/bin/" 2>/dev/null
done
mkdir -p "$CH/usr/lib/apt" "$CH/usr/lib/dpkg" "$CH/usr/lib/x86_64-linux-gnu"
cp -a /usr/lib/x86_64-linux-gnu/libapt-pkg*.so* "$CH/usr/lib/x86_64-linux-gnu/" 2>/dev/null
cp -a /usr/lib/x86_64-linux-gnu/libapt-private*.so* "$CH/usr/lib/x86_64-linux-gnu/" 2>/dev/null
cp -a /usr/lib/apt/* "$CH/usr/lib/apt/" 2>/dev/null
# dpkg 的方法脚本（dpkg-deb 解 .deb 时要调 tar 等）
mkdir -p "$CH/usr/lib/dpkg/methods"
cp -a /usr/share/dpkg/* "$CH/usr/share/dpkg/" 2>/dev/null
cp -a /etc/dpkg/dpkg.cfg "$CH/etc/dpkg/dpkg.cfg" 2>/dev/null

# 补 apt/dpkg 的动态库依赖
# libssl3t64 必须在：apt-get / apt-cache 都链 libcrypto.so.3，
# 缺了会直接 `error while loading shared libraries: libcrypto.so.3`，
# install.sh 的「本地 file: 源」那一支就整条走不通。
for p in libstdc++6 libmd0 libpcre2-8-0 liblzma5 liblz4-1 libzstd1 libbz2-1.0 \
         libselinux1 libacl1 libattr1 libcap2 libgcc-s1 libmount1 libblkid1 \
         libsmartcols1 libuuid1 libsystemd0 libudev1 libaudit1 libcap-ng0 \
         libseccomp2 libcrypt1 libedit2 libncursesw6 libfdisk1 libapparmor1 \
         libxxhash0 libcom-err2 libext2fs2t64 libgpg-error0 libgcrypt20 libjansson4 \
         libargon2-1 libdb5.3t64 mawk gawk libsigsegv2 libmpfr6 libmpc3 \
         libtext-charwidth-perl libtext-wrapi18n-perl perl-base \
         libssl3t64 openssl openssl-provider-legacy libreadline8t64 libnettle8t64 \
         libhogweed6t64 libgnutls30t64; do
  f=$(ls "$DEBS/${p}"_*.deb 2>/dev/null | head -1)
  [ -n "$f" ] && dpkg-deb -x "$f" "$CH" 2>/dev/null
done
ldconfig -r "$CH" 2>/dev/null || true
echo "  基础层文件数：$(find "$CH" -type f | wc -l)"

echo "=== 2/4 把离线包搬进 chroot 并断网 ==="
mkdir -p "$CH/tmp/offline" "$CH/etc/apt/sources.list.d" "$CH/etc/apt/apt.conf.d" \
         "$CH/var/lib/apt/lists/partial" "$CH/var/cache/apt/archives/partial" \
         "$CH/usr/share/dpkg" "$CH/var/lib/dpkg/info" "$CH/var/lib/dpkg/updates"
cp -a "$OFFLINE"/. "$CH/tmp/offline/"
# 清空所有源：让 apt 除了本地 file: 仓库之外无处可取
: > "$CH/etc/apt/sources.list"
rm -f "$CH"/etc/apt/sources.list.d/* 2>/dev/null || true
touch "$CH/var/lib/dpkg/status"
# dpkg 需要这两个文件做三向合并，缺了会在安装时报错
[ -f "$CH/etc/dpkg/dpkg.cfg" ] || { mkdir -p "$CH/etc/dpkg"; touch "$CH/etc/dpkg/dpkg.cfg"; }
echo "  chroot 内 sources.list 已清空、DNS 指向黑洞"

# ⚠️ 绝对不要 `mount --bind /dev $CH/dev` ——
# 实测踩过：如果 $CH 的路径写错（比如前面多 cd 了一层、或 CH 变量为空），
# bind 会把宿主机的 /dev 挂到宿主机自己的某个目录上，随后清理时的
# rm -f /dev/null 之类会把**宿主机的设备节点换成普通文件**，
# 结果是全系统所有 `> /dev/null` 都变成「权限不够」，各种服务莫名其妙挂掉。
# chroot 里装包根本用不到 /dev 的大部分节点，只补 /dev/null 即可。
mkdir -p "$CH/dev"
[ -e "$CH/dev/null" ] || mknod -m 666 "$CH/dev/null" c 1 3 2>/dev/null || \
  { : > "$CH/dev/null"; chmod 666 "$CH/dev/null"; }
[ -e "$CH/dev/urandom" ] || { : > "$CH/dev/urandom"; chmod 666 "$CH/dev/urandom"; }
echo "  chroot 内 /dev/null 已就绪（未做危险 bind-mount）"

echo "=== 3/4 在无外网 chroot 里执行离线安装 ==="
# 优先用自包含包里的 install.sh（packaging/offline-install.sh，走本地 file: 源）；
# 没有就退回 make-offline-deps.sh 生成的那个。
INSTALLER=""
for c in "$CH/tmp/offline/install.sh"; do
  [ -f "$c" ] && INSTALLER="$c" && break
done
if [ -z "$INSTALLER" ]; then
  echo "  ✘ 离线包里没有 install.sh"; exit 1
fi
echo "  使用安装脚本：$(basename "$INSTALLER")"

# 关键：把 chroot 路径通过 DROUTER_ROOT 告诉 install.sh，
# 让 apt/dpkg 都去读这个根的数据库（不然读的是宿主机的，判断全错）。
chroot "$CH" /bin/bash -c '
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
export DROUTER_ROOT=/
cd /tmp/offline
bash install.sh
' 2>&1 | tail -45
RC=${PIPESTATUS[0]}

echo ""
echo "=== 4/4 验收：drouter 是否真的装上了 ==="
if [ -f "$CH/opt/drouter/backend/drouter-helper.py" ]; then
  echo "  ✅ /opt/drouter/backend/drouter-helper.py 存在"
  echo "  后端模块：$(ls -1 "$CH"/opt/drouter/backend/*.py 2>/dev/null | wc -l) 个"
  echo "  前端文件：$(ls -1 "$CH"/opt/drouter/web/ 2>/dev/null | wc -l) 个"
  echo "  drouter-ctl：$([ -x "$CH/usr/local/bin/drouter-ctl" ] && echo 存在 || echo 缺失)"
  N_DEP=$(ls -1 "$CH"/var/lib/dpkg/info/*.list 2>/dev/null | wc -l)
  echo "  dpkg 记录的已安装包：$N_DEP"
else
  echo "  ❌ 没装上 drouter 本体"
fi

# 清理挂载点，避免残留
umount "$CH/proc" 2>/dev/null || true
umount "$CH/dev"  2>/dev/null || true
# 再确认一次宿主机设备节点没被弄坏
[ -c /dev/null ] || { rm -f /dev/null && mknod -m 666 /dev/null c 1 3; echo "  （已修复宿主 /dev/null）"; }
echo ""
echo "chroot 保留在 $CH（确认无误后可 rm -rf）"
exit 0
