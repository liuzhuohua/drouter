#!/bin/bash
# 真机文字排版体检：直接驱动 192.168.7.3 上的**真实面板**（走 8080 明文口，
# 避开自签证书导致无头 Chrome 拒绝加载），逐个视图调 go(k) 渲染真数据，
# 再用同一个探针量几何。
#
# 为什么需要它：本地 t-en-render 预览是「假数据 + file://」，真实数据的长度
# （长网卡名 / 长 MAC / 真实提示语）才是把窄列压成竖排的元凶。
#
# 用法：bash _dev/live-text-audit.sh        # 默认 6 档视口
#       VIEWPORTS="800 800" bash _dev/live-text-audit.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="C:/Users/lyrz-pve-win10/.workbuddy/binaries/node/workspace"
cd "$WS" || exit 1
export PATH="/c/Users/lyrz-pve-win10/.workbuddy/binaries/node/versions/22.22.2-6:$PATH"
AB="node node_modules/agent-browser/bin/agent-browser.js"
PY="C:/Users/lyrz-pve-win10/.workbuddy/binaries/python/versions/3.13.12/python.exe"
URL="${URL:-http://192.168.7.3:8080/}"
VIEWPORTS="${VIEWPORTS:-1440 900|1280 900|1080 900|900 800|800 800|390 844}"

"$PY" "$ROOT/_dev/mk-live-audit.py" || exit 1
RUN="$(cat "$ROOT/_dev/.live-run.js")"

TOKEN=$(curl -s -X POST "${URL}api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])" 2>/dev/null)
if [ -z "${TOKEN:-}" ]; then echo "登录失败，拿不到 token"; exit 1; fi
echo "登录成功 token=${TOKEN:0:6}…"

LANGCODE="${LANGCODE:-}"
$AB close --all >/dev/null 2>&1
$AB open "$URL" >/dev/null 2>&1
$AB eval "localStorage.setItem('drouter_token','$TOKEN');localStorage.setItem('drouter_lang','${LANGCODE:-zh-CN}');location.reload();'ok'" >/dev/null 2>&1
sleep 4

# 自检：确认能读到 VIEWS / go
chk=$($AB eval "typeof VIEWS + '|' + typeof go" 2>/dev/null | head -1 | tr -d '"')
echo "环境自检 typeof VIEWS|go = $chk"
case "$chk" in
  object\|function|function\|function) ;;
  *) echo "⚠️ VIEWS/go 不可见，脚本无法继续"; $AB close >/dev/null 2>&1; exit 2;;
esac

echo "$VIEWPORTS" | tr '|' '\n' | while read -r wh; do
  [ -z "$wh" ] && continue
  set -- $wh
  $AB set viewport $1 $2 >/dev/null 2>&1
  sleep 1
  $AB eval "$RUN" >/dev/null 2>&1
  for i in $(seq 1 90); do
    s=$($AB eval "window.__aud?(window.__aud.done?'done':'run'):'none'" 2>/dev/null \
        | head -1 | tr -d '"')
    [ "$s" = "done" ] && break
    sleep 2
  done
  out="$ROOT/_dev/.live-aud-${PREFIX:-}$1.json"
  $AB eval "JSON.stringify({vw:innerWidth,done:window.__aud.done,n:window.__aud.rows.length,rows:window.__aud.rows})" \
    2>/dev/null | head -1 > "$out"
  echo "视口 $1x$2 -> $out  ($(wc -c < "$out") 字节)"
done
$AB close >/dev/null 2>&1
echo LIVE_AUDIT_DONE
