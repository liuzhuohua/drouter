#!/bin/bash
# 真机截图（给用户做修复前后对比用）。
# 用法：bash _dev/shot-live.sh <view> <w> <h> <out.png> [要点的按钮选择器]
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
PY="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
URL="${URL:-http://192.168.7.3:8080/}"
VIEW="${1:?view}"; VW="${2:-1908}"; VH="${3:-1080}"; OUT="${4:?out.png}"
CLICK="${5:-}"

TOKEN=$(curl -s -X POST "${URL}api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])" 2>/dev/null)
[ -z "${TOKEN:-}" ] && { echo "登录失败"; exit 1; }

$AB close --all >/dev/null 2>&1
$AB open "$URL" >/dev/null 2>&1
$AB eval "localStorage.setItem('drouter_token','$TOKEN');localStorage.setItem('drouter_lang','${LANGCODE:-zh-CN}');location.reload();'ok'" >/dev/null 2>&1
sleep 4
$AB set viewport "$VW" "$VH" >/dev/null 2>&1
$AB eval "go('$VIEW')" >/dev/null 2>&1
sleep 2
if [ -n "$CLICK" ]; then
  $AB eval "var b=document.querySelector('$CLICK'); if(b) b.click(); 'ok'" >/dev/null 2>&1
  sleep 14
fi
$AB screenshot body "$OUT" 2>/dev/null | tail -1
$AB close >/dev/null 2>&1
if [ -f "$OUT" ]; then echo "OK $(stat -c %s "$OUT") bytes -> $OUT"; else echo "截图失败"; fi
