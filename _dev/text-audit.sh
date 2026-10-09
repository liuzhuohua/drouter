#!/bin/bash
# 文字排版体检：把 t-en-render 的 DUMP_ALL 预览页在真 Chrome 里按
# PC(1280x900) / 手机(390x844) 打开，跑 .text-probe.js，找出
# 「被挤成柱状（竖排感）/ 被裁 / 卡片内左边缘不齐」的元素。
# 用法：DUMP_ALL=1 node _dev/t-en-render.js 生成 _dev/.layout/ 后再跑本脚本。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
BASE="file:///$ROOT/_dev/.layout"
JS="$(cat "$ROOT/_dev/.text-probe.js")"
HIDE="var s=document.querySelector('.sidebar'); if(s) s.style.display='none';"
for v in $(ls "$ROOT/_dev/.layout" | sed 's/\.html$//'); do
  for wh in "390 844 m" "1280 900 d"; do
    set -- $wh
    $AB set viewport $1 $2 >/dev/null 2>&1
    $AB open "$BASE/$v.html" >/dev/null 2>&1
    if [ "$3" = "m" ]; then $AB eval "$HIDE" >/dev/null 2>&1; fi
    o=$($AB eval "$JS" 2>/dev/null | head -1)
    echo "$v|$3|$o"
  done
done
$AB close >/dev/null 2>&1
