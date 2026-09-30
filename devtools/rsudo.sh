#!/bin/bash
# 以 sudo 在目标机执行命令（密码通过 stdin 传给 sudo -S）
HERE="C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00"
"$HERE/router-build/devtools/rsh.sh" "echo 'yyfmo64102' | sudo -S -p '' bash -c $(printf %q "$1")" 2>&1
