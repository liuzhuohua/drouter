#!/bin/bash
# drouter 静态回归总入口（不需要真机，不需要 root）
#
# 分两类：
#   A. 静态检查器 —— 跑 _dev/t-*.py，纯读源码/造临时目录，
#      任何开发机上都能跑，改完代码先跑这个。
#   B. 真机回归 —— scripts/regression.sh，需要已部署的实例。
#
# 用法：
#   scripts/test-static.sh                跑全部静态检查
#   scripts/test-static.sh t-107 t-api106   只跑指定的
#
# ⚠️ 这份文件只是薄封装，真正跑的是 _dev/run-static.py。
# 早先这里用 bash for循环逐个 `python _dev/t-xxx.py`，跑到第 20 来个
# 检查器时整个 bash 会话被 SIGTERM 掉，输出全丢、还不知道跑到哪 ——
# 逐个跑明明都过（分批跑验证过），单跑也过，就是一口气跑太久会死。
# 排查这点花了好几轮，结论是「长会话累计触发」，不是某个脚本的问题。
# 与其跟它搏斗，不如把驱动挪到 Python：那边能落日志文件，
# 崩了也知道自己死在哪一步。shell 只负责转发参数。
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
cd "$HERE" || exit 1

exec "$PY" _dev/run-static.py "$@"
