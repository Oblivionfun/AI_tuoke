#!/usr/bin/env bash
# 每日备份：SQLite 在线备份到 ./data/backups（保留 14 份），可选同步到异地。
# 宿主机 crontab 示例（每天凌晨 3 点）：
#   0 3 * * * cd /opt/guanwei-workbench && ./deploy/backup.sh >> data/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T web workbench backup --dest /data/backups --keep 14

# 异地备份（可选）：配置好 rclone 远端后取消注释，例如对象存储或另一台机器。
# rclone copy data/backups remote:guanwei-workbench-backups --max-age 48h

echo "$(date '+%F %T') backup ok"
