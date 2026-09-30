#!/bin/sh
# askpass.sh 在 devtools/ 下（此前误写成根目录的 _dev/，导致连不上目标机）
SSH_ASKPASS="C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/devtools/askpass.sh" \
SSH_ASKPASS_REQUIRE=force \
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 ajeef@192.168.7.3 "$@"
