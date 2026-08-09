#!/usr/bin/env bash
# 每日备份 data/(持仓/策略/对话/账户/流水)。用法: bash deploy/backup.sh
# 配 systemd timer(deploy/tradeassistant-backup.*)或 crontab 每日跑。保留最近 14 份。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="${TA_BACKUP_DIR:-backups}"
mkdir -p "$DST"
F="$DST/data-$(date +%F_%H%M).tgz"
tar czf "$F" data 2>/dev/null
# 只留最近 14 份
ls -1t "$DST"/data-*.tgz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "✓ 备份完成: $F  (现有 $(ls -1 "$DST"/data-*.tgz 2>/dev/null | wc -l | tr -d ' ') 份)"
