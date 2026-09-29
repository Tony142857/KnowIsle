#!/usr/bin/env bash
# 知屿每日备份（§15.4）：pg_dump + 对象存储快照，保留 14 天。
# 在宿主机以 crontab 定时执行，例如：13 4 * * * /path/to/deploy/backup.sh
set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-compose.yml"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS=14
DATE="$(date +%Y%m%d)"

mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"   # docker -v 挂载源必须是绝对路径

# 复用已钉死且本地必有的 postgres 镜像执行打包（GNU tar），避免引入浮动的 alpine:latest
TAR_IMAGE="postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"

# PostgreSQL 逻辑备份
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U "${POSTGRES_USER:-knowisle}" "${POSTGRES_DB:-knowisle}" \
    | gzip > "$BACKUP_DIR/pg_${DATE}.sql.gz"

# SeaweedFS 快照：直接打包数据卷（含 S3 对象、filer 元数据与 volume 数据）
docker run --rm \
    -v knowisle_seaweeddata:/data:ro \
    -v "$BACKUP_DIR:/backup" \
    "$TAR_IMAGE" tar czf "/backup/seaweedfs_${DATE}.tar.gz" -C /data .

# 清理过期备份
find "$BACKUP_DIR" -maxdepth 1 -mtime "+$KEEP_DAYS" -exec rm -rf {} +

echo "backup done: $BACKUP_DIR (pg_${DATE}.sql.gz, seaweedfs_${DATE}.tar.gz)"
