#!/bin/bash
# 真机验收：静态资源传输层（1.0.9）
#
# 验的是「浏览器实际收到什么」，不是源码里写了什么：
#   ① 带指纹的 app.js → Cache-Control: immutable 长缓存
#   ② 第二次请求 If-None-Match → 304，且**零字节响应体**
#   ③ 无指纹的 /app.js → 仍是 no-cache
#   ④ Accept-Encoding: br 但目标机没装 brotli → **必须退到 gzip**
#      （这是本轮最大的风险点，退化就是 583KB 裸传）
#   ⑤ 页脚版本号已注入，不再是 __APP_VERSION__ 字面量
#   ⑥ index.html 的 ETag 里含版本号
#   ⑦ logo 是 <a id="brand-home">，页脚有版本号元素
set -u
BASE="http://127.0.0.1:8080"
WEB="http://127.0.0.1:8080"
fail=0
ck() { if [ "$2" = "1" ]; then echo "  ✓ $1"; else echo "  ✗ $1"; fail=$((fail+1)); fi; }

echo "=== 1. 探测面板是否在跑 ==="
# ⚠️ **不要在这里试登录**。密码猜错有锁定计数（实测错误返回
# 「还可尝试 3 次」），验收脚本反复试会把用户的账号锁死 ——
# 而且脚本里硬编码密码这个做法本身就不该保留。
# 本脚本要验的东西全是公开静态资源 + 免鉴权的 /api/openapi，不需要登录。
CODE=$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "$BASE/index.html")
echo "  GET /index.html → $CODE"
[ "$CODE" = "200" ] || { echo "面板没起来（$CODE），后续无意义"; exit 1; }

echo
echo "=== 2. 取 app.js 的指纹（从 index.html 里解析）==="
IDX=$(curl -sk -m 5 "$BASE/index.html")
# ⚠️ 不能用 sed 抽 /app.js?v=... —— 问号是 sed 的选项分隔符，
# 会报「s 的未知选项」，然后指纹解析静默失败、退回测无指纹 URL，
# 于是「immutable 长缓存」那条必然变红 —— 假红比不测更坏。
APPV=$(printf '%s' "$IDX" | grep -o '/app\.js?v=[0-9]*' | head -1)
[ -z "$APPV" ] && APPV="/app.js"
echo "  指纹 URL = $APPV"

echo
echo "=== 3. 带指纹请求 → 应为 immutable 长缓存 ==="
H1=$(curl -sk -m 10 -D - -o /dev/null "$BASE$APPV")
CC=$(printf '%s' "$H1" | sed -n 's/^[Cc]ache-[Cc]ontrol:[[:space:]]*//p' | tr -d '\r')
echo "  Cache-Control: $CC"
[ -z "$CC" ] && CC=$(printf '%s' "$H1" | grep -i '^cache-control:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
printf '%s' "$CC" | grep -q 'immutable' && ck "带指纹 → immutable 长缓存" 1 || ck "带指纹 → immutable 长缓存（实际: $CC）" 0
printf '%s' "$CC" | grep -q 'max-age=31536000' && ck "max-age 是一年" 1 || ck "max-age 是一年" 0

ETAG=$(printf '%s' "$H1" | grep -i '^etag:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
[ -n "$ETAG" ] && ck "有 ETag（$ETAG）" 1 || ck "有 ETag" 0

echo
echo "=== 4. 传输量（br / gzip / 无压缩 各一次）==="
SZ_BR=$(curl -sk -m 20 -H 'Accept-Encoding: br, gzip' -o /dev/null -w '%{size_download}' "$BASE$APPV")
ENC_BR=$(curl -sk -m 20 -D - -o /dev/null -H 'Accept-Encoding: br, gzip' "$BASE$APPV" | grep -i '^content-encoding:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
SZ_GZ=$(curl -sk -m 20 -H 'Accept-Encoding: gzip' -o /dev/null -w '%{size_download}' "$BASE$APPV")
ENC_GZ=$(curl -sk -m 20 -D - -o /dev/null -H 'Accept-Encoding: gzip' "$BASE$APPV" | grep -i '^content-encoding:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
SZ_RAW=$(curl -sk -m 20 -H 'Accept-Encoding:' -o /dev/null -w '%{size_download}' "$BASE$APPV")
SRC=$(stat -c %s /opt/drouter/web/app.js)
echo "  源文件 $SRC 字节"
echo "  Accept-Encoding: br,gzip → $SZ_BR 字节（Content-Encoding: ${ENC_BR:-无}）"
echo "  Accept-Encoding: gzip   → $SZ_GZ 字节（Content-Encoding: ${ENC_GZ:-无}）"
echo "  不带编码        → $SZ_RAW 字节"

# 关键：目标机没装 python3-brotli，br 必须降级到 gzip，
# 绝不能「要 br 就发原文」—— 那样等于 583KB 裸传，比优化前更慢。
if [ -n "$ENC_BR" ]; then
  ck "带 br 时有编码（实际 ${ENC_BR}）" 1
  if [ "$ENC_BR" = "gzip" ]; then
    ck "br 不可用时已降级到 gzip（本机无 python3-brotli，属预期）" 1
  elif [ "$ENC_BR" = "br" ]; then
    echo "    （本机装了 brotli，用的是 br）"
  fi
else
  SZ_NOGZ=$(curl -sk -m 20 -H 'Accept-Encoding: br' -o /dev/null -w '%{size_download}' "$BASE$APPV")
  echo "    ⚠ br 无编码且不支持 gzip → $SZ_NOGZ 字节裸传"
  ck "br 不可用且无 gzip 兜底时会裸传（应为 0，本机应装 gzip 支持）" 0
fi
[ "$SZ_GZ" -lt "$SRC" ] && ck "gzip 确实压小了（$SZ_GZ < $SRC）" 1 || ck "gzip 确实压小了" 0
[ "$SZ_RAW" = "$SRC" ] && ck "不带编码时原样输出（$SZ_RAW = 源文件大小）" 1 || ck "不带编码时原样输出" 0

echo
echo "=== 5. 304 协商（第二次打开应当零传输）==="
# 304 响应没有 body，curl **不会创建** -o 指定的文件 → stat 会报
# 「没有那个文件或目录」，进而让 [: -lt ] 拿到空值。
# 用 %{size_download} 让 curl 自己报字节数，不依赖文件是否存在。
rm -f /tmp/_304body
read -r CODE BODY <<<"$(curl -sk -m 10 -o /tmp/_304body -w '%{http_code} %{size_download}' \
  -H "If-None-Match: $ETAG" "$BASE$APPV")"
BODY=${BODY:-0}
echo "  状态码 $CODE，响应体 $BODY 字节"
[ "$CODE" = "304" ] && ck "If-None-Match 命中 → 304" 1 || ck "If-None-Match 命中 → 304（实际 $CODE）" 0
[ "$BODY" -lt 100 ] && ck "304 不回传响应体" 1 || ck "304 不回传响应体（实际 $BODY 字节）" 0
rm -f /tmp/_304body

echo
echo "=== 6. 无指纹请求 → 必须仍是 no-cache ==="
CC2=$(curl -sk -m 10 -D - -o /dev/null "$BASE/app.js" | grep -i '^cache-control:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
echo "  Cache-Control: $CC2"
printf '%s' "$CC2" | grep -q 'no-cache' && ck "无指纹 → no-cache（否则部署后拿到旧文件）" 1 || ck "无指纹 → no-cache（实际: $CC2）" 0
printf '%s' "$CC2" | grep -q 'immutable' && ck "⚠ 无指纹却给了 immutable（会导致用户卡在旧版本）" 0 || ck "无指纹没有误给 immutable" 1

echo
echo "=== 7. Vary 头（代理正确性）==="
V=$(curl -sk -m 10 -D - -o /dev/null -H 'Accept-Encoding: gzip' "$BASE$APPV" | grep -ci '^vary:.*Accept-Encoding')
[ "$V" -ge 1 ] && ck "压缩响应带 Vary: Accept-Encoding" 1 || ck "压缩响应带 Vary: Accept-Encoding" 0

echo
echo "=== 8. 版本号注入 ==="
if printf '%s' "$IDX" | grep -q '__APP_VERSION__'; then
  ck "页脚版本号已注入（不应残留占位符）" 0
else
  ck "页脚版本号已注入（无占位符残留）" 1
fi
VER=$(printf '%s' "$IDX" | sed -n 's/.*id="app-ver"[^>]*>\(v[0-9][^<]*\)<.*/\1/p' | head -1)
[ -z "$VER" ] && VER=$(printf '%s' "$IDX" | grep -o 'v[0-9]\+\.[0-9]\+\(\.[0-9]\+\)\?' | tail -1)
echo "  页面显示版本号 = ${VER:-（未找到）}"
[ -n "$VER" ] && ck "页脚有版本号" 1 || ck "页脚有版本号" 0
VFILE=$(cat /opt/drouter/VERSION 2>/dev/null | tr -d ' \t\r\n')
echo "  /opt/drouter/VERSION = ${VFILE:-（不存在）}"
[ -n "$VFILE" ] && ck "VERSION 文件已部署" 1 || ck "VERSION 文件已部署" 0
if [ -n "$VER" ] && [ -n "$VFILE" ]; then
  [ "v$VFILE" = "$VER" ] && ck "页面版本号与 VERSION 文件一致" 1 || ck "页面版本号与 VERSION 文件一致（页面 $VER vs 文件 v$VFILE）" 0
fi

echo
echo "=== 9. index.html 的 ETag 含版本号 ==="
IE=$(curl -sk -m 10 -D - -o /dev/null "$BASE/index.html" | grep -i '^etag:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
echo "  index.html ETag = $IE"
if [ -n "$VFILE" ]; then
  printf '%s' "$IE" | grep -q "$VFILE" && ck "ETag 里含版本号（改版本即换 ETag）" 1 || ck "ETag 里含版本号" 0
fi

echo
echo "=== 10. 两个 UI 元素 ==="
printf '%s' "$IDX" | grep -q 'id="brand-home"' && ck "左上角 logo 是可点击元素" 1 || ck "左上角 logo 是可点击元素" 0
printf '%s' "$IDX" | grep -q '<a class="brand' && ck "logo 用 <a>（键盘/读屏可用）" 1 || ck "logo 用 <a>" 0
printf '%s' "$IDX" | grep -q 'title="返回系统概览"' && ck "logo 有可访问名称" 1 || ck "logo 有可访问名称" 0
printf '%s' "$IDX" | grep -q 'id="app-ver"' && ck "页脚有版本号容器" 1 || ck "页脚有版本号容器" 0

echo
echo "=== 11. openapi 的 version 与页面一致（/api/openapi 免鉴权）==="
API_VER=$(curl -sk -m 5 "$BASE/api/openapi" | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
echo "  openapi version = ${API_VER:-（未取到）}"
[ -n "$API_VER" ] && ck "openapi 可免鉴权访问且能取到 version" 1 || ck "openapi version 取不到" 0
if [ -n "$VFILE" ] && [ -n "$API_VER" ]; then
  [ "$API_VER" = "$VFILE" ] && ck "openapi version 与 VERSION 文件一致（都是 $VFILE）" 1 \
    || ck "openapi version 与 VERSION 一致（$API_VER vs $VFILE）" 0
fi

echo
if [ "$fail" -eq 0 ]; then echo "=== 全部通过 ==="; else echo "=== $fail 项失败 ==="; fi
exit $fail
