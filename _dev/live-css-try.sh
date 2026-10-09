#!/bin/bash
# 真机 CSS 试验台：把一段候选 CSS 注入真机页面，再跑一遍全视图探针，
# 用来在**改源码之前**验证修法是否真的消除竖排（避免又一轮「本地绿、真机红」）。
#
# 用法：CSS='th{white-space:nowrap}' VIEWPORTS="800 800" bash _dev/live-css-try.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
PY="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
URL="${URL:-http://192.168.7.3:8080/}"
VIEWPORTS="${VIEWPORTS:-800 800|390 844}"
CSS="${CSS:-}"

"$PY" "$ROOT/_dev/mk-live-audit.py" >/dev/null || exit 1
RUN="$(cat "$ROOT/_dev/.live-run.js")"

TOKEN=$(curl -s -X POST "${URL}api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])" 2>/dev/null)
[ -z "${TOKEN:-}" ] && { echo "登录失败"; exit 1; }

$AB close --all >/dev/null 2>&1
$AB open "$URL" >/dev/null 2>&1
$AB eval "localStorage.setItem('drouter_token','$TOKEN');localStorage.setItem('drouter_lang','${LANGCODE:-zh-CN}');location.reload();'ok'" >/dev/null 2>&1
sleep 4

echo "$VIEWPORTS" | tr '|' '\n' | while read -r wh; do
  [ -z "$wh" ] && continue
  set -- $wh
  $AB set viewport $1 $2 >/dev/null 2>&1
  # 注入候选 CSS（覆盖层，位于所有作者样式之后）
  if [ -n "$CSS" ]; then
    $AB eval "var s=document.getElementById('__try')||document.createElement('style');s.id='__try';s.textContent=$(printf '%s' "$CSS" | "$PY" -c "import sys,json;print(json.dumps(sys.stdin.read()))");document.head.appendChild(s);'ok'" >/dev/null 2>&1
  fi
  sleep 1
  $AB eval "$RUN" >/dev/null 2>&1
  for i in $(seq 1 90); do
    s=$($AB eval "window.__aud?(window.__aud.done?'done':'run'):'none'" 2>/dev/null | head -1 | tr -d '"')
    [ "$s" = "done" ] && break
    sleep 2
  done
  out="$ROOT/_dev/.try-$1.json"
  $AB eval "JSON.stringify({vw:innerWidth,done:window.__aud.done,rows:window.__aud.rows})" \
    2>/dev/null | head -1 > "$out"
  "$PY" - "$out" <<'PYEOF'
import io, json, sys, collections
d = json.loads(io.open(sys.argv[1], encoding='utf-8').read())
if isinstance(d, str):
    d = json.loads(d)
c = collections.Counter()
rows = []
over = []
for r in d['rows']:
    if r.get('vOver'):
        over.append('   %s vOver=%s' % (r['v'], r['vOver']))
    for it in r.get('items', []):
        if it['t'] in ('column', 'vertical'):
            c[(it['t'], it['sel'])] += 1
            rows.append('   %-16s w=%-4s lines=%-3s per=%-4s %s @%s' % (
                it['sel'], it['w'], it.get('lines'), it.get('per'), it['txt'][:26], r['v']))
print('视口 %s：竖排类问题 %d 个，内容区横向溢出 %d 个' % (d['vw'], len(rows), len(over)))
for k, v in c.most_common(12):
    print('   %s %s x%d' % (k[0], k[1], v))
for line in rows[:20]:
    print(line)
for line in over[:8]:
    print(line)
PYEOF
done
$AB close >/dev/null 2>&1
echo CSS_TRY_DONE
