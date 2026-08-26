#!/usr/bin/env bash
# P1-4: fusion-artifacts-engine 在线备份脚本。
# 备份 meta.db（sqlite3 .backup 在线快照，WAL 友好，不锁库）+ content/ 目录（rsync）。
# 用法：
#   ./scripts/backup.sh                       # 默认备份 ~/.fusion/artifacts → ~/.fusion/artifacts-backup-<ts>
#   ./scripts/backup.sh /path/to/dest         # 指定目标目录
#   STORAGE_ROOT=/data/artifacts ./scripts/backup.sh /backup  # 覆盖源
# 退出码：0 成功；非 0 失败（写日志到 stderr）。
set -euo pipefail

STORAGE_ROOT="${STORAGE_ROOT:-$HOME/.fusion/artifacts}"
DB_NAME="${DB_NAME:-meta.db}"
TS="$(date +%Y%m%d-%H%M%S)"
DEST="${1:-$HOME/.fusion/artifacts-backup-$TS}"

log() { printf '[backup] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[ -d "$STORAGE_ROOT" ] || die "storage_root 不存在: $STORAGE_ROOT"
DB_PATH="$STORAGE_ROOT/$DB_NAME"
[ -f "$DB_PATH" ] || die "meta.db 不存在: $DB_PATH"

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 未安装"
command -v rsync >/dev/null 2>&1 || die "rsync 未安装"

mkdir -p "$DEST"
DEST_DB="$DEST/$DB_NAME"

# 1) 在线 DB 快照。sqlite3 .backup 在 WAL 模式下生成一致性快照，不阻塞读写。
log "备份 DB: $DB_PATH -> $DEST_DB"
sqlite3 "$DB_PATH" ".backup '$DEST_DB'" || die "sqlite3 .backup 失败"
log "DB 快照完成: $(du -h "$DEST_DB" | cut -f1)"

# 2) content/ 目录（大版本内容文件）。rsync 增量复制，运行中新增版本文件不影响已拷部分。
CONTENT_SRC="$STORAGE_ROOT/content"
if [ -d "$CONTENT_SRC" ]; then
    log "备份 content/: $CONTENT_SRC -> $DEST/content"
    rsync -a --delete "$CONTENT_SRC/" "$DEST/content/" || die "rsync content 失败"
    log "content 备份完成"
else
    log "无 content/ 目录，跳过"
fi

log "备份成功: $DEST"
