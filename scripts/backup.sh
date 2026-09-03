#!/bin/bash
# MAPS 每日备份：MySQL + uploads + subtitles，默认保留30天
# 用法：./scripts/backup.sh   （建议加入crontab，每天凌晨3点执行）
# 备份后自动做 gzip 完整性校验，校验失败的备份立即删除并以非零码退出（便于 cron 告警）
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/data/backups/maps}"
KEEP_DAYS="${KEEP_DAYS:-30}"
DATE=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$BACKUP_DIR/backup.log"
mkdir -p "$BACKUP_DIR"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

# 兼容 docker compose v2 / docker-compose v1
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi

# 从 .env 读取凭据（避免在脚本中硬编码密码）
eval "$(grep -E '^(MYSQL_USER|MYSQL_PASSWORD|MYSQL_DATABASE|REDIS_PASSWORD)=' "$PROJECT_DIR/.env" | tr -d '\r')"

DB_FILE="$BACKUP_DIR/db_$DATE.sql.gz"
UL_FILE="$BACKUP_DIR/uploads_$DATE.tar.gz"
RD_FILE="$BACKUP_DIR/redis_$DATE.rdb.gz"
FAILED=0

# 备份MySQL（通过容器内mysqldump，无需在宿主机装mysql客户端）
# --no-tablespaces：受限账号/RDS无PROCESS权限时必需；--single-transaction：InnoDB一致性快照不锁表
log "开始备份 MySQL ..."
$DC -f "$PROJECT_DIR/docker-compose.yml" exec -T mysql \
  mysqldump -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" --single-transaction --no-tablespaces "$MYSQL_DATABASE" \
  | gzip > "$DB_FILE"

# 备份上传文件与字幕（uploads/subtitles 可能较大，tar 失败不阻断 DB 备份校验）
log "开始备份 uploads/subtitles ..."
if ! tar -czf "$UL_FILE" -C "$PROJECT_DIR" uploads subtitles; then
  log "ERROR: uploads 打包失败"
  rm -f "$UL_FILE"
  FAILED=1
fi

# 备份 Redis（队列任务/积分余额/DeepSeek 配额计数；RDB 快照经容器内 redis-cli 流式导出，不阻塞服务）
# 队列任务目前仅存 Redis（AOF 已开），RDB 快照可在误清库/卷损坏时恢复队列与余额
log "开始备份 Redis（RDB 快照）..."
if $DC -f "$PROJECT_DIR/docker-compose.yml" exec -T redis sh -c \
  'REDISCLI_AUTH="$0" redis-cli --rdb /tmp/redis_backup.rdb >/dev/null 2>&1 && cat /tmp/redis_backup.rdb && rm -f /tmp/redis_backup.rdb' \
  "$REDIS_PASSWORD" | gzip > "$RD_FILE.tmp"; then
  mv "$RD_FILE.tmp" "$RD_FILE"
else
  log "ERROR: Redis 备份失败"
  rm -f "$RD_FILE.tmp"
  FAILED=1
fi

# ---- 完整性校验 ----
# gzip -t：校验压缩流 CRC 与完整性；SQL 备份额外确认含 "Dump completed" 标记
log "校验备份完整性 ..."
if gzip -t "$DB_FILE" && zcat "$DB_FILE" | tail -3 | grep -q "Dump completed"; then
  log "DB 备份校验通过: $(du -h "$DB_FILE" | cut -f1)"
else
  log "ERROR: DB 备份校验失败（文件损坏或导出不完整）: $DB_FILE"
  rm -f "$DB_FILE"
  FAILED=1
fi

if [ -f "$UL_FILE" ] && gzip -t "$UL_FILE"; then
  log "uploads 备份校验通过: $(du -h "$UL_FILE" | cut -f1)"
elif [ -f "$UL_FILE" ]; then
  log "ERROR: uploads 备份校验失败: $UL_FILE"
  rm -f "$UL_FILE"
  FAILED=1
fi

# Redis RDB 校验：gzip 完整性 + RDB 魔数 "REDIS" 文件头
if [ -f "$RD_FILE" ] && gzip -t "$RD_FILE" && [ "$(zcat "$RD_FILE" | head -c 5)" = "REDIS" ]; then
  log "Redis 备份校验通过: $(du -h "$RD_FILE" | cut -f1)"
elif [ -f "$RD_FILE" ]; then
  log "ERROR: Redis 备份校验失败（文件损坏或非 RDB 格式）: $RD_FILE"
  rm -f "$RD_FILE"
  FAILED=1
fi

# 清理过期备份
find "$BACKUP_DIR" -name "*.gz" -mtime +"$KEEP_DAYS" -delete
# 日志仅保留最近 1000 行，避免无限增长
if [ -f "$LOG_FILE" ]; then
  tail -1000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

if [ "$FAILED" -ne 0 ]; then
  log "备份完成但存在失败项，请检查上方 ERROR"
  exit 1
fi
log "备份完成: $DB_FILE, $UL_FILE, $RD_FILE（保留 $KEEP_DAYS 天）"
