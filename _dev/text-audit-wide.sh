#!/bin/bash
# 文字排版体检（宽视口版）。
#
# 与 _dev/text-audit.sh 的区别：那一版**把侧栏 display:none 藏掉**再量，
# 等于白送 236px 宽度 —— 真机上侧栏是占位的（>760px 视口），内容区实际只有
# vw-236px。这就是「本地审计 0 问题、真机还是竖排」的假绿来源（2026-10-09）。
# 本脚本保留真侧栏，并按 1440/1280/1080/900/800 五档宽度扫（<=760 时侧栏
# 变抽屉，才隐藏）。
#
# 用法：DUMP_ALL=1 node _dev/t-en-render.js 先生成 _dev/.layout/，再跑本脚本。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
BASE="file:///$ROOT/_dev/.layout"
JS="$(cat "$ROOT/_dev/.text-probe.js")"
HIDE="var s=document.querySelector('.sidebar'); if(s) s.style.display='none';"
ONLY="${ONLY:-}"
VIEWPORTS="${VIEWPORTS:-1440 900 d|1280 900 d|1080 900 d|900 800 d|800 800 d|390 844 m}"
for v in $(ls "$ROOT/_dev/.layout" | sed 's/\.html$//'); do
  [ -n "$ONLY" ] && [ "$v" != "$ONLY" ] && continue
  echo "$VIEWPORTS" | tr '|' '\n' | while read -r wh; do
    set -- $wh
    $AB set viewport $1 $2 >/dev/null 2>&1
    $AB open "$BASE/$v.html" >/dev/null 2>&1
    if [ "$3" = "m" ]; then $AB eval "$HIDE" >/dev/null 2>&1; fi
    o=$($AB eval "$JS" 2>/dev/null | head -1)
    echo "$v|$1|$o"
  done
done
$AB close >/dev/null 2>&1
