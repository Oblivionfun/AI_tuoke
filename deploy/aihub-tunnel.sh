#!/usr/bin/env bash
# 临时方案：让服务器经这台电脑访问公司内网的 AIHub 网关（演示用，之后换成内网部署或海外服务器）。
#
# 原理：SSH 反向隧道。服务器上 127.0.0.1:18080 的连接，经 SSH 回到这台电脑，再转给 api.aihub.woa.com:80。
# 要求：这台电脑开着、连着公司内网（能打开 http://api.aihub.woa.com），并能 SSH 到服务器。
#
# 用法：
#   ./deploy/aihub-tunnel.sh                 # 前台运行，Ctrl+C 停止
#   nohup ./deploy/aihub-tunnel.sh > /tmp/aihub-tunnel.log 2>&1 &   # 后台运行
set -uo pipefail

SERVER="${SERVER:-root@113.249.117.200}"
REMOTE_PORT="${REMOTE_PORT:-18080}"
GATEWAY="${GATEWAY:-api.aihub.woa.com:80}"

# 隧道断了就 5 秒后重连；caffeinate 防止电脑休眠导致演示中断
while true; do
  echo "$(date '+%F %T') 建立隧道：${SERVER} 127.0.0.1:${REMOTE_PORT} -> ${GATEWAY}"
  caffeinate -i ssh -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=3 \
    -o BatchMode=yes \
    -R "127.0.0.1:${REMOTE_PORT}:${GATEWAY}" \
    "$SERVER"
  echo "$(date '+%F %T') 隧道断开（退出码 $?），5 秒后重连"
  sleep 5
done
