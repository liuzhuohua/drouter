#!/bin/bash
# scp 到目标机也需要密码；复用 SSH_ASKPASS
#
# 用法（两种都可以）：
#   rscp.sh 本地文件 /tmp/远端路径              # 自动补 ajeef@192.168.7.3: 前缀
#   rscp.sh 本地文件 ajeef@192.168.7.3:/tmp/x  # 已是远端形式，原样透传
#
# 陷阱：Windows 的 scp 在目标不含 "user@host:" 时会退化成本地 cp，
#       静默返回 0 却什么都没传出去。所以这里强制补前缀。
HERE="C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00"
TARGET="ajeef@192.168.7.3"

args=()
for a in "$@"; do
  case "$a" in
    -*|*@*:*) args+=("$a");;          # scp 选项 / 已是远端形式
    *)        args+=("$a");;
  esac
done

# 最后一个参数视为远端目标；若不是远端形式则补前缀
n=${#args[@]}
if [ "$n" -ge 1 ]; then
  last="${args[$((n-1))]}"
  case "$last" in
    -*|*@*:*) ;;                      # 选项 / 已是远端，不动
    *) args[$((n-1))]="$TARGET:$last";;
  esac
fi

SSH_ASKPASS="$HERE/router-build/devtools/askpass.sh" \
SSH_ASKPASS_REQUIRE=force \
DISPLAY=:0 \
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "${args[@]}"
