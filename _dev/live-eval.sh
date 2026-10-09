#!/bin/bash
# 在真机页面上跑任意 JS（已登录）。用法：
#   bash _dev/live-eval.sh "<js>" [view] [w] [h]
# 第 2~4 参数给了就先切视口、go(view)、等 2 秒，再执行 js。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
PY="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
URL="${URL:-http://192.168.7.3:8080/}"
JS="${1:-}"
VIEW="${2:-}"; VW="${3:-}"; VH="${4:-}"
WAIT="${WAIT:-2}"

TOKEN=$(curl -s -X POST "${URL}api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])" 2>/dev/null)
[ -z "${TOKEN:-}" ] && { echo "登录失败"; exit 1; }

$AB close --all >/dev/null 2>&1
$AB open "$URL" >/dev/null 2>&1
$AB eval "localStorage.setItem('drouter_token','$TOKEN');localStorage.setItem('drouter_lang','${LANGCODE:-zh-CN}');location.reload();'ok'" >/dev/null 2>&1
sleep 4
if [ -n "$VIEW" ]; then
  [ -n "$VW" ] && $AB set viewport "$VW" "$VH" >/dev/null 2>&1
  $AB eval "go('$VIEW')" >/dev/null 2>&1
  sleep "$WAIT"
fi
# PRE：在等待之前先执行的 JS（例如点「开始检测」按钮，等结果渲染完再量）
if [ -n "${PRE:-}" ]; then
  $AB eval "$PRE" >/dev/null 2>&1
  sleep "${WAIT2:-8}"
fi
if [ -n "${CSS:-}" ]; then
  $AB eval "var s=document.getElementById('__try')||document.createElement('style');s.id='__try';s.textContent=$(printf '%s' "$CSS" | "$PY" -c "import sys,json;print(json.dumps(sys.stdin.read()))");document.head.appendChild(s);'ok'" >/dev/null 2>&1
  sleep 1
fi
$AB eval "$JS" 2>/dev/null | head -1
$AB close >/dev/null 2>&1
