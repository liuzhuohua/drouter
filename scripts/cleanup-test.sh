#!/bin/bash
# 清理回归测试残留：PPPoE 测试账号（peers / chap-secrets / pap-secrets）
set -u
echo "=== 清理前 ==="
grep -l "test12345" /etc/ppp/peers/* /etc/ppp/*-secrets 2>/dev/null

echo "=== 删除测试生成的 PPPoE 文件 ==="
rm -f /etc/ppp/peers/drouter-wan
# secrets 文件如果是本次测试生成的（仅含测试账号），恢复为空的模板
for f in /etc/ppp/chap-secrets /etc/ppp/pap-secrets; do
  if [ -f "$f" ] && [ "$(wc -l < "$f")" -le 1 ] && grep -q "test12345" "$f" 2>/dev/null; then
    printf '# 由 drouter 自动生成：PPPoE 认证凭据\n# 在 WEB 界面填写 PPPoE 账号密码并保存后，此处会被写入\n' > "$f"
    chmod 600 "$f"; chown root:root "$f"
    echo "已重置 $f"
  fi
done

echo "=== 重置数据库 pppoe 配置块 ==="
python3 - <<'PY'
import sqlite3, json
db = '/opt/drouter/data/drouter.db'
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
row = c.execute("SELECT value FROM settings WHERE key='pppoe'").fetchone()
cur = json.loads(row['value']) if row else {}
cur.setdefault('isp', 'auto'); cur.setdefault('mtu', 1492); cur.setdefault('mru', 1492)
cur.setdefault('persist', True); cur.setdefault('maxfail', 0); cur.setdefault('holdoff', 5)
cur.setdefault('service_name', '')
cur['iface'] = ''; cur['username'] = ''; cur['password'] = ''
cur['saved_only'] = True; cur['note'] = '仅保存凭据，未执行拨号'
c.execute("UPDATE settings SET value=? WHERE key='pppoe'", (json.dumps(cur, ensure_ascii=False),))
c.commit()
print('pppoe ->', json.dumps(cur, ensure_ascii=False))
PY

echo "=== 清理后核对 ==="
if grep -rq "test12345" /etc/ppp/ 2>/dev/null; then
  echo "仍有残留！"; grep -rn "test12345" /etc/ppp/ 2>/dev/null
else
  echo "无测试账号残留 ✔"
fi
ls -la /etc/ppp/peers/ /etc/ppp/chap-secrets /etc/ppp/pap-secrets 2>&1
echo "CLEANUP_DONE"
