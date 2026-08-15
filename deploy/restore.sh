#!/bin/bash
# QuantDesk 灾难恢复脚本
# 从指定日期的备份恢复 PostgreSQL、Redis 和配置文件
# 用法: ./restore.sh <备份日期，例如 2026-07-10>

set -euo pipefail

# ===== 配置 =====
BACKUP_DIR="${QUANTDESK_ROOT:-/opt/quantdesk}/backups"
PG_CONTAINER="quantdesk-postgres"
PG_USER="quantdesk"
PG_DB="quantdesk"
REDIS_CONTAINER="quantdesk-redis"
CONFIG_DIR="${QUANTDESK_ROOT:-/opt/quantdesk}/data/store"

# ===== 参数验证 =====
if [ $# -lt 1 ]; then
    echo "用法: $0 <备份日期>"
    echo "示例: $0 2026-07-10"
    echo ""
    echo "可用备份:"
    if [ -d "$BACKUP_DIR" ]; then
        ls -1 "$BACKUP_DIR" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | while read -r d; do
            size=$(du -sh "${BACKUP_DIR}/${d}" | cut -f1)
            echo "  ${d}  (${size})"
        done
    else
        echo "  (无可用备份)"
    fi
    exit 1
fi

RESTORE_DATE="$1"
RESTORE_PATH="${BACKUP_DIR}/${RESTORE_DATE}"

# 验证备份目录存在
if [ ! -d "$RESTORE_PATH" ]; then
    echo "❌ 错误: 备份目录不存在: ${RESTORE_PATH}"
    echo ""
    echo "可用备份:"
    ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || echo "  (无)"
    exit 1
fi

echo "=========================================="
echo "QuantDesk 灾难恢复"
echo "=========================================="
echo "恢复来源: ${RESTORE_PATH}"
echo ""

# 列出可用文件
echo "备份内容:"
[ -f "${RESTORE_PATH}/quantdesk_postgres.sql.gz" ] && echo "  ✅ PostgreSQL: quantdesk_postgres.sql.gz" || echo "  ❌ PostgreSQL 备份不存在"
[ -f "${RESTORE_PATH}/dump.rdb.gz" ] && echo "  ✅ Redis: dump.rdb.gz" || echo "  ❌ Redis 备份不存在"
[ -f "${RESTORE_PATH}/config_store.tar.gz" ] && echo "  ✅ 配置文件: config_store.tar.gz" || echo "  ❌ 配置文件备份不存在"
echo ""

# ===== 确认提示 =====
echo "⚠️  警告: 此操作将覆盖当前所有数据！"
echo "   - PostgreSQL 数据库 ${PG_DB} 将被删除并重建"
echo "   - Redis 数据将被完全替换"
echo "   - 配置文件将被覆盖"
echo ""
read -r -p "确认执行恢复？输入 YES 继续: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
    echo "已取消恢复操作。"
    exit 0
fi

echo ""
echo "=========================================="
echo "开始恢复..."
echo "=========================================="

ERRORS=0

# ===== 1. 恢复 PostgreSQL =====
if [ -f "${RESTORE_PATH}/quantdesk_postgres.sql.gz" ]; then
    echo "📦 正在恢复 PostgreSQL..."
    # 先终止所有连接
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${PG_DB}' AND pid <> pg_backend_pid();" \
        2>/dev/null || true
    # 删除并重建数据库
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS ${PG_DB};" || {
        echo "❌ 无法删除数据库"
        ERRORS=$((ERRORS + 1))
    }
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c "CREATE DATABASE ${PG_DB} OWNER ${PG_USER};" || {
        echo "❌ 无法创建数据库"
        ERRORS=$((ERRORS + 1))
    }
    # 恢复数据
    if gunzip -c "${RESTORE_PATH}/quantdesk_postgres.sql.gz" | docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" --quiet 2>/dev/null; then
        echo "✅ PostgreSQL 恢复成功"
    else
        echo "❌ PostgreSQL 恢复失败"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "⏭️  跳过 PostgreSQL (备份文件不存在)"
fi

# ===== 2. 恢复 Redis =====
if [ -f "${RESTORE_PATH}/dump.rdb.gz" ]; then
    echo "📦 正在恢复 Redis..."
    # 停止 Redis 写入
    docker exec "$REDIS_CONTAINER" redis-cli DEBUG SLEEP 0.1 2>/dev/null || true
    # 解压 RDB 文件
    gunzip -k -f "${RESTORE_PATH}/dump.rdb.gz" 2>/dev/null || true
    # 复制到容器
    if docker cp "${RESTORE_PATH}/dump.rdb" "${REDIS_CONTAINER}:/data/dump.rdb" 2>/dev/null; then
        docker restart "$REDIS_CONTAINER" 2>/dev/null
        sleep 2
        if docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG; then
            echo "✅ Redis 恢复成功"
        else
            echo "⚠️  Redis 已恢复但重启后无法连接"
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "❌ Redis 恢复失败"
        ERRORS=$((ERRORS + 1))
    fi
    # 清理临时解压文件
    rm -f "${RESTORE_PATH}/dump.rdb"
else
    echo "⏭️  跳过 Redis (备份文件不存在)"
fi

# ===== 3. 恢复配置文件 =====
if [ -f "${RESTORE_PATH}/config_store.tar.gz" ]; then
    echo "📦 正在恢复配置文件..."
    # 备份当前配置
    if [ -d "$CONFIG_DIR" ]; then
        cp -r "$CONFIG_DIR" "${CONFIG_DIR}.bak.$(date +%s)" 2>/dev/null || true
    fi
    mkdir -p "$(dirname "$CONFIG_DIR")"
    if tar -xzf "${RESTORE_PATH}/config_store.tar.gz" -C "$(dirname "$CONFIG_DIR")" 2>/dev/null; then
        echo "✅ 配置文件恢复成功"
    else
        echo "❌ 配置文件恢复失败"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "⏭️  跳过配置文件 (备份文件不存在)"
fi

# ===== 恢复报告 =====
echo ""
echo "=========================================="
if [ $ERRORS -eq 0 ]; then
    echo "✅ 恢复全部成功完成！"
    echo ""
    echo "建议操作:"
    echo "  1. 重启应用: docker compose restart"
    echo "  2. 验证服务: curl http://localhost:3000/health"
    echo "  3. 检查日志: docker compose logs -f --tail=50"
    exit 0
else
    echo "⚠️  恢复完成，但有 ${ERRORS} 个错误"
    echo "请检查上方日志并手动修复问题。"
    exit 1
fi
