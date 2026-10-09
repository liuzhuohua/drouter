#!/bin/bash
# 真机级排版审计：把 t-en-render 的 DUMP_ALL 预览页在真 Chrome 里按
# PC(1280x900) / 手机(390x844) 两种视口打开，量出「内容区被迫横向滚动」的视图
# 与具体越界元素。需要 agent-browser + Chrome（见 skill agent-browser）。
# 用法：node _dev/t-en-render.js 前先 DUMP_ALL=1 生成 _dev/.layout/，再 bash 本脚本。
set -u
AB="node node_modules/agent-browser/bin/agent-browser.js"
cd "C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
BASE="file:///C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/_dev/.layout"
JS="$(cat /c/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/_dev/layout-probe.js)"
HIDE="var s=document.querySelector('.sidebar'); if(s) s.style.display='none';"
for v in $(ls /c/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/_dev/.layout | sed 's/.html$//'); do
  for wh in "390 844 m" "1280 900 d"; do
    set -- $wh
    $AB set viewport $1 $2 >/dev/null 2>&1
    $AB open "$BASE/$v.html" >/dev/null 2>&1
    if [ "$3" = "m" ]; then $AB eval "$HIDE" >/dev/null 2>&1; fi
    out=$($AB eval "$JS" 2>/dev/null | head -1)
    echo "$v|$3|$out"
  done
done
$AB close >/dev/null 2>&1
