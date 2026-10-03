"""drouter 静态回归驱动器（Windows 开发机用）。

为什么不用 shell 脚本跑：
  这个环境里 bash 会话有 SIGTERM 风险，把整轮结果一起带走，
  表现为「跑全量回归 → 命令莫名死掉」，看不出跑到第几个。
  改成 Python逐个跑 + 每项前后写日志文件，崩了也能定位到具体那一项。

用法：
    python _dev/run-static.py            跑全部
    python _dev/run-static.py t-107 t-ca 只跑指定的
"""
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 需要真机 / 网络 / 交互 / 长时间压测的，不在静态回归范围内。
# ⚠️ 必须带 `t-` 前缀 —— ls 出来的名字就是 t-xxx。写裸名的话一个都
# 排除不掉，交互脚本照样跑，其中一个会把整个会话干掉。
EXCL = {
    't-live', 't-smoke', 't-ulog-perf', 't-ulog-detail', 't-perf',
    't-print', 't-cachebust', 't-cleanup', 't-webshell', 't-pty',
    't-daemon-restart',
    # ⚠️ 反向验证脚本**会真的改源文件**（注入 bug → 跑测试 → 还原）。
    # 名字前缀也是 t-，会被 glob 自动收进来；一旦被自动执行，
    # 它就会在回归过程中反复改写 backend/ 与 web/ —— 后果是
    # 别的检查器读到被注入污染的代码，报出一堆假红，
    # 更糟的是中途被打断会留下污染（已经发生过一次）。
    # 只能手动跑：python _dev/t-107-inject-wg.py --batch 0
    't-107-inject-wg',
}


def main():
    args = sys.argv[1:]
    if args:
        names = [a if a.startswith('t-') else 't-' + a for a in args]
    else:
        names = sorted(os.path.basename(p)[:-3]
                       for p in glob.glob(os.path.join(HERE, 't-*.py')))
        names = [n for n in names if n not in EXCL]

    logpath = os.path.join(HERE, '.run-static.log')
    log = open(logpath, 'w', encoding='utf-8')

    def say(msg):
        sys.stdout.write(msg + '\n')
        sys.stdout.flush()
        log.write(msg + '\n')
        log.flush()

    passed, failed = [], []
    for n in names:
        f = os.path.join(HERE, n + '.py')
        if not os.path.isfile(f):
            say('  %-20s 跳过（不存在）' % n)
            continue
        say('  %-20s ... ' % n)
        try:
            p = subprocess.run([sys.executable, f], capture_output=True,
                               text=True, encoding='utf-8', errors='replace',
                               timeout=180)
            rc, out = p.returncode, (p.stdout or '')
        except subprocess.TimeoutExpired:
            rc, out = 124, 'TIMEOUT'
        if rc == 0:
            m = re.findall(r'通过 (\d+) 项', out)
            say('ok%s' % (('  (%s 项)' % m[-1]) if m else ''))
            passed.append(n)
        else:
            say('失败 rc=%s' % rc)
            for line in out.splitlines():
                if re.search(r'\[NG\]|Traceback|Error|✘', line):
                    say('      ' + line)
            failed.append(n)

    log.close()
    print()
    print('通过 %d 个检查器，失败 %d 个' % (len(passed), len(failed)))
    if failed:
        print('失败：%s' % ' '.join(failed))
        return 1
    print('（明细见 _dev/.run-static.log）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
