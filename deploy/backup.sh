#!/bin/bash
# QuantDesk 自动备份脚本
# 每日自动备份 PostgreSQL、Redis 和配置文件
# 保留最近 7 天的备份

set -euo pipefail

# ===== 配置 =====
BACKUP_DIR="${QUANTDESK_ROOT:-/opt/quantdesk}/backups"
DATE=$(date +%Y-%m-%d)
BACKUP_PATH="${BACKUP_DIR}/${DATE}"
LOG_FILE="${BACKUP_PATH}/backup.log"
RETENTION_DAYS=7

PG_CONTAINER="quantdesk-postgres"
PG_USER="quantdesk"
PG_DB="quantdesk"

REDIS_CONTAINER="quantdesk-redis"
CONFIG_DIR="${QUANTDESK_ROOT:-/opt/quantdesk}/data/store"

# 计数器
TOTAL_TASKS=3
SUCCESS_COUNT=0
FAIL_COUNT=0
FAILED_TASKS=""

# ===== 辅助函数 =====
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

mark_success() {
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    log "✅ $1 备份成功"
}

mark_failure() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILED_TASKS="${FAILED_TASKS}  - $1\n"
    log "❌ $1 备份失败"
}

# ===== 初始化 =====
mkdir -p "$BACKUP_PATH"
log "=========================================="
log "QuantDesk 备份任务开始"
log "备份目录: ${BACKUP_PATH}"
log "=========================================="

# ===== 1. PostgreSQL 备份 =====
log "📦 开始备份 PostgreSQL..."
if docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner --no-acl 2>>"$LOG_FILE" | gzip > "${BACKUP_PATH}/quantdesk_postgres.sql.gz"; then
    backup_size=$(du -sh "${BACKUP_PATH}/quantdesk_postgres.sql.gz" | cut -f1)
    log "   PostgreSQL 备份大小: ${backup_size}"
    mark_success "PostgreSQL"
else
    mark_failure "PostgreSQL"
fi

# ===== 2. Redis 备份 =====
log "📦 开始备份 Redis..."
if docker cp "${REDIS_CONTAINER}:/data/dump.rdb" "${BACKUP_PATH}/dump.rdb" 2>>"$LOG_FILE"; then
    gzip -f "${BACKUP_PATH}/dump.rdb"
    backup_size=$(du -sh "${BACKUP_PATH}/dump.rdb.gz" | cut -f1)
    log "   Redis 备份大小: ${backup_size}"
    mark_success "Redis"
else
    mark_failure "Redis"
fi

# ===== 3. 配置文件备份 =====
log "📦 开始备份配置文件..."
if [ -d "$CONFIG_DIR" ]; then
    if tar -czf "${BACKUP_PATH}/config_store.tar.gz" -C "$(dirname "$CONFIG_DIR")" "$(basename "$CONFIG_DIR")" 2>>"$LOG_FILE"; then
        backup_size=$(du -sh "${BACKUP_PATH}/config_store.tar.gz" | cut -f1)
        log "   配置文件备份大小: ${backup_size}"
        mark_success "配置文件"
    else
        mark_failure "配置文件"
    fi
else
    log "⚠️  配置目录不存在: ${CONFIG_DIR}"
    mark_failure "配置文件"
fi

# ===== 4. 清理旧备份 =====
log "🧹 清理 ${RETENTION_DAYS} 天前的旧备份..."
DELETED_COUNT=0
if [ -d "$BACKUP_DIR" ]; then
    for old_dir in "$BACKUP_DIR"/*/; do
        if [ -d "$old_dir" ]; then
            dir_name=$(basename "$old_dir")
            # 验证目录名是日期格式
            if [[ "$dir_name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
                dir_epoch=$(date -d "$dir_name" +%s 2>/dev/null || echo 0)
                cutoff_epoch=$(date -d "${RETENTION_DAYS} days ago" +%s)
                if [ "$dir_epoch" -gt 0 ] && [ "$dir_epoch" -lt "$cutoff_epoch" ]; then
                    rm -rf "$old_dir"
                    DELETED_COUNT=$((DELETED_COUNT + 1))
                    log "   已删除旧备份: ${dir_name}"
                fi
            fi
        fi
    done
fi
log "   共清理 ${DELETED_COUNT} 个旧备份"

# ===== 汇总报告 =====
TOTAL_SIZE=$(du -sh "$BACKUP_PATH" | cut -f1)

log ""
log "=========================================="
log "📊 备份汇总报告"
log "=========================================="
log "  日期:        ${DATE}"
log "  总大小:      ${TOTAL_SIZE}"
log "  成功:        ${SUCCESS_COUNT}/${TOTAL_TASKS}"
log "  失败:        ${FAIL_COUNT}/${TOTAL_TASKS}"
if [ $FAIL_COUNT -gt 0 ]; then
    log ""
    log "⚠️  失败任务:"
    echo -e "$FAILED_TASKS" | tee -a "$LOG_FILE"
    log "=========================================="
    log "❌ 备份完成 (有错误)"
    exit 1
else
    log "=========================================="
    log "✅ 备份全部成功完成"
    exit 0
fi
