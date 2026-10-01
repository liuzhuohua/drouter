#!/bin/bash
# fix-dev-nodes.sh —— 补齐 devtmpfs 上被误删的标准设备节点
#
# 背景：/dev 是 devtmpfs，由内核维护。但节点一旦被 `rm` 删掉，内核**不会**
#   自动重建 —— 只有重新挂载 devtmpfs（或重启）才会。于是"少几个节点"会
#   静默潜伏很久，直到某个程序用到它才炸。
#
#   2026-09-30 一次 `mount --bind /dev $CH/dev` 的变量写错，把宿主机 /dev
#   挂到了自己身上，随后的清理 `rm -f` 清空了整个 /dev；当时只补回了
#   /dev/null 一个节点。直到 10-01 用户保存 DHCP 配置时，dnsmasq 预检报
#   "failed to seed the random number generator: 没有那个文件或目录"，
#   才暴露出 /dev/urandom 缺失（连带 zero/random/tty/console 与全部块设备
#   都没有）。
#
# 本脚本幂等：只创建缺失的节点，已存在的一律不碰，可反复安全执行。
# 用法：sudo bash devtools/fix-dev-nodes.sh
set -u
export LC_ALL=C

if [ "$(id -u)" != "0" ]; then
  echo "错误：需要 root 权限（sudo bash $0）" >&2
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/root/dev-nodes-backup-$STAMP.txt"
ls -laR /dev > "$BACKUP" 2>&1 || true
echo "修改前现状已备份 → $BACKUP"

CREATED=0; SKIPPED=0; FAILED=0

mknod_if() {   # mknod_if <路径> <c|b> <主> <次> <权限> [属主]
  local p="$1" t="$2" M="$3" m="$4" mode="$5" owner="${6:-root:root}"
  if [ -e "$p" ]; then
    SKIPPED=$((SKIPPED + 1))
    return 0
  fi
  mkdir -p "$(dirname "$p")"
  if mknod -m "$mode" "$p" "$t" "$M" "$m" 2>/dev/null; then
    chown "$owner" "$p" 2>/dev/null || true
    printf '  + %-22s %s %s:%-4s %s %s\n' "$p" "$t" "$M" "$m" "$mode" "$owner"
    CREATED=$((CREATED + 1))
  else
    printf '  ! %-22s 创建失败\n' "$p"
    FAILED=$((FAILED + 1))
  fi
}

link_if() {    # link_if <软链路径> <目标>
  local l="$1" t="$2"
  if [ -e "$l" ]; then SKIPPED=$((SKIPPED + 1)); return 0; fi
  if ln -sfn "$t" "$l"; then
    printf '  + %-22s -> %s\n' "$l" "$t"
    CREATED=$((CREATED + 1))
  else
    printf '  ! %-22s 创建失败\n' "$l"
    FAILED=$((FAILED + 1))
  fi
}

echo
echo "== 基础字符设备（设备号取自内核 devices.txt，全平台固定）=="
mknod_if /dev/mem     c 1  1  640 root:kmem
mknod_if /dev/port    c 1  4  640 root:kmem
mknod_if /dev/zero    c 1  5  666
mknod_if /dev/full    c 1  7  666
mknod_if /dev/random  c 1  8  666
mknod_if /dev/urandom c 1  9  666     # ← dnsmasq 预检依赖它
mknod_if /dev/kmsg    c 1  11 600
mknod_if /dev/tty     c 5  0  666
mknod_if /dev/console c 5  1  600

echo
echo "== 网络与回环 =="
mknod_if /dev/net/tun c 10 200 666    # ← VPN / EasyTier 依赖它
if [ -e /sys/module/loop ] || [ -d /sys/block/loop0 ]; then
  mknod_if /dev/loop-control c 10 237 660 root:disk
fi

echo
echo "== 虚拟控制台（XFCE / LightDM 用）=="
mknod_if /dev/tty0 c 4 0 620 root:tty
for n in 1 2 3 4 5 6; do
  mknod_if "/dev/tty$n" c 4 "$n" 620 root:tty
done

echo
echo "== 块设备（主次号直接读 /proc/partitions，保证与内核一致）=="
while read -r maj min _blocks name; do
  case "$maj" in ''|major) continue ;; esac
  [ -n "${name:-}" ] || continue
  mknod_if "/dev/$name" b "$maj" "$min" 660 root:disk
done < /proc/partitions

echo
echo "== 输入设备（从 /sys 读实际主次号）=="
for d in /sys/class/input/event*; do
  [ -e "$d" ] || continue
  n=$(basename "$d")
  maj=$(cut -d: -f1 < "$d/dev")
  min=$(cut -d: -f2 < "$d/dev")
  mknod_if "/dev/input/$n" c "$maj" "$min" 660 root:input
done

echo
echo "== 标准软链 =="
link_if /dev/fd     /proc/self/fd
link_if /dev/stdin  /proc/self/fd/0
link_if /dev/stdout /proc/self/fd/1
link_if /dev/stderr /proc/self/fd/2
link_if /dev/core   /proc/kcore

echo
echo "------------------------------------------------------------"
printf '新建 %d 个 / 跳过（已存在）%d 个 / 失败 %d 个\n' "$CREATED" "$SKIPPED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
