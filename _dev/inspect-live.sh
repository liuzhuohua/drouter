#!/bin/bash
# 针对单个视图，把「被挤到 <70px 的 tag / th / td / button / b / label」
# 连同它向上 5 层的计算样式打出来，用于定位竖排的 CSS 根因。
# 用法：bash _dev/inspect-live.sh nfs 900 800
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
PY="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
URL="${URL:-http://192.168.7.3:8080/}"
VIEW="${1:-dash}"; VW="${2:-900}"; VH="${3:-800}"
JS="$(cat "$ROOT/_dev/.inspect.js")"
if [ "${OVER:-0}" = "1" ]; then JS="$(cat "$ROOT/_dev/.overflow.js")"; fi

TOKEN=$(curl -s -X POST "${URL}api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])" 2>/dev/null)
[ -z "${TOKEN:-}" ] && { echo "登录失败"; exit 1; }

$AB close --all >/dev/null 2>&1
$AB open "$URL" >/dev/null 2>&1
$AB eval "localStorage.setItem('drouter_token','$TOKEN');location.reload();'ok'" >/dev/null 2>&1
sleep 4
$AB set viewport "$VW" "$VH" >/dev/null 2>&1
$AB eval "go('$VIEW')" >/dev/null 2>&1
sleep 2
if [ -n "${SEL:-}" ]; then
  $AB eval "window.__insSel=$SEL;window.__insMaxW=${MAXW:-70};window.__insLim=${LIM:-6};'ok'" >/dev/null 2>&1
fi
echo "=== view=$VIEW viewport=${VW}x${VH} sel=${SEL:-default} over=${OVER:-0} ==="
$AB eval "$JS" 2>/dev/null | head -1 | "$PY" -c "
import sys,json
raw=sys.stdin.read().strip()
try: d=json.loads(raw)
except Exception: print(raw[:400]); raise SystemExit
if isinstance(d,str): d=json.loads(d)
for it in d:
    if 'over' in it:
        print('溢出 %-4s w=%-5s pOver=%-5s %-16s ovx=%-8s ws=%s | %s'%(
          it['over'],it['w'],it['pOver'],it['sel'][:16],it['ovx'],it['ws'],it['txt'][:30]))
        print('     %s'%it['html'][:150])
        continue
    print('%s  w=%s h=%s ws=%s wb=%s disp=%s | par=%s(w=%s kids=%s) | %s'%(
      it['sel'],it['w'],it['h'],it['ws'],it['wb'],it['disp'],
      it['parDisp'],it['parW'],it['parKids'],it['txt']))
    print('    parHTML: %s'%it['parHtml'].replace('\n',' ')[:170])
    for c in it['chain'][:4]:
        print('    %-30s w=%-5s disp=%-10s wrap=%-7s basis=%-7s shrink=%-4s minw=%-8s ws=%-9s wb=%s'%(
          c['s'][:30],c['w'],c['disp'],c['wrap'],c['basis'],c['shrink'],c['minw'],c['ws'],c['wb']))
"
$AB close >/dev/null 2>&1
