#!/bin/bash
# 用法：
#   ./update-console.sh                        # 不传参数 → 更新全部四个组件
#   ./update-console.sh management dashboard   # 只更新指定组件
#
# 约定：不传参数就更新全部。
# 这个脚本会被「NetBird 更新台」以非交互方式调用，所以不再依赖 whiptail 菜单；
# 想只升某几个组件，直接传参数即可（工具的「参数」那一格填上名字）。
#
# 为什么可以每次都拉全部四个：官方这轮没发新版的组件，digest 不变，下面会跳过，
# docker compose up -d 也只会重建镜像真的变了的容器。全部一起拉反而避免组件版本撕裂。

# ================= 配置区域 =================
DPANEL_COMPOSE_DIR="/home/dockge/compose"
STACK_NAME="netbird"
DATA_DIR="/home/netbird/management"
BACKUP_DIR="/home/netbird/backups"
RETENTION_DAYS=7
ALLOWED_SERVICES="management dashboard signal relay"
# ===========================================

for cmd in docker tar; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "❌ 未安装 $cmd"; exit 1; }
done

echo "========================================"
echo " 📡 [1/6] 定位堆栈位置..."

TARGET_DIR="$DPANEL_COMPOSE_DIR/$STACK_NAME"
[ -d "$TARGET_DIR" ] || { echo "❌ 路径错误：$TARGET_DIR 不存在"; exit 1; }

echo "✅ 堆栈目录: $TARGET_DIR"
cd "$TARGET_DIR" || exit 1
if [ ! -f "compose.yaml" ] && [ ! -f "docker-compose.yaml" ] && [ ! -f "docker-compose.yml" ]; then
    echo "❌ 错误：当前目录下未找到 compose.yaml 或 docker-compose.yaml"
    exit 1
fi

# --- 2. 确定要更新的组件 ---
if [ $# -gt 0 ]; then
    SELECTED_SERVICES="$*"
    echo "📦 命令行指定组件: $SELECTED_SERVICES"
else
    SELECTED_SERVICES="$ALLOWED_SERVICES"
    echo "📦 未指定组件，更新全部: $SELECTED_SERVICES"
fi

# 先校验名字，避免拼错后去拉 netbirdio/typo 才发现
for SVC in $SELECTED_SERVICES; do
    case " $ALLOWED_SERVICES " in
        *" $SVC "*) ;;
        *) echo "❌ 未知服务: $SVC（可选: $ALLOWED_SERVICES）"; exit 1 ;;
    esac
done

NOW_TIME=$(date +"%Y%m%d-%H%M%S")
CUTOFF_SEC=$(date -d "$RETENTION_DAYS days ago" +%s)

# --- 3. 清理过期备份镜像 ---
echo "========================================"
echo " 🧹 [2/6] 检查过期备份镜像 (> $RETENTION_DAYS 天)..."

docker images --format "{{.Repository}}:{{.Tag}}" | grep -E "^netbirdio/.*:backup-[0-9]{8}-[0-9]{6}$" | while read -r IMAGE_NAME; do
    DATE_PART=$(echo "$IMAGE_NAME" | grep -oE '[0-9]{8}-[0-9]{6}$')
    DATE_STR=$(echo "$DATE_PART" | cut -d'-' -f1)
    IMG_SEC=$(date -d "$DATE_STR" +%s 2>/dev/null)
    if [ -n "$IMG_SEC" ] && [ "$IMG_SEC" -lt "$CUTOFF_SEC" ]; then
        echo "       🗑️  发现过期备份，正在删除: $IMAGE_NAME"
        docker rmi -f "$IMAGE_NAME" > /dev/null 2>&1
    fi
done
echo "       ✅ 过期清理检查完成"

# --- 4. 备份数据 ---
echo " 🔒 [3/6] 备份数据..."
mkdir -p "$BACKUP_DIR" || { echo "❌ 无法创建备份目录 $BACKUP_DIR"; exit 1; }

BACKUP_FILE="$BACKUP_DIR/netbird-data-$NOW_TIME.tar.gz"
DATA_PARENT_DIR=$(dirname "$DATA_DIR")
DATA_TARGET_DIR=$(basename "$DATA_DIR")

if tar -czf "$BACKUP_FILE" -C "$DATA_PARENT_DIR" "$DATA_TARGET_DIR" 2>/dev/null; then
    echo "       ✅ 数据备份完成: $BACKUP_FILE"
else
    echo "       ⚠️ 数据备份异常，脚本终止"
    exit 1
fi

# --- 5. 逐个组件处理镜像 ---
echo " 🐳 [4/6] 处理镜像..."

UPDATE_FAILED_FLAG=0
UPDATED_COUNT=0
UPDATED_LIST=""
SKIPPED_LIST=""

for SERVICE in $SELECTED_SERVICES; do
    OFFICIAL_IMAGE="netbirdio/$SERVICE"
    BACKUP_TAG="backup-$NOW_TIME"

    echo "       -> $SERVICE"

    OLD_IMAGE_ID=$(docker image inspect "$OFFICIAL_IMAGE:latest" --format '{{.Id}}' 2>/dev/null)

    PULL_OUTPUT=$(docker pull "$OFFICIAL_IMAGE:latest" 2>&1)
    if [ $? -ne 0 ]; then
        echo "          ❌ 拉取失败，保留旧版"
        echo "          📝 错误信息: $(echo "$PULL_OUTPUT" | tail -1)"
        UPDATE_FAILED_FLAG=1
        continue
    fi

    NEW_IMAGE_ID=$(docker image inspect "$OFFICIAL_IMAGE:latest" --format '{{.Id}}' 2>/dev/null)

    # 官方这轮没发新版：digest 没变，跳过；容器也不会被重建
    if [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_IMAGE_ID" = "$NEW_IMAGE_ID" ]; then
        echo "          ⏭️  已是最新版本，跳过"
        SKIPPED_LIST="$SKIPPED_LIST $SERVICE"
        continue
    fi

    if [ -n "$OLD_IMAGE_ID" ]; then
        echo "          💾 备份旧镜像为: $OFFICIAL_IMAGE:$BACKUP_TAG"
        docker tag "$OLD_IMAGE_ID" "$OFFICIAL_IMAGE:$BACKUP_TAG"
    else
        echo "          ℹ️  无旧镜像可备份（首次部署）"
    fi

    echo "          ✅ 更新成功"
    UPDATED_COUNT=$((UPDATED_COUNT + 1))
    UPDATED_LIST="$UPDATED_LIST $SERVICE"
done

# --- 6. 重建容器 ---
echo " 🚀 [5/6] 重建容器..."

if [ $UPDATE_FAILED_FLAG -eq 1 ]; then
    echo "❌ 警告：部分镜像拉取失败！为防止组件版本不兼容，已中止容器重建流程。"
    echo "💡 提示：当前正在运行的容器未受影响，请检查网络后重试。"
    exit 1
elif [ $UPDATED_COUNT -eq 0 ]; then
    echo "✅ 选中的组件均已是最新版本，无需重建。"
else
    docker compose -p "$STACK_NAME" up -d
    if [ $? -ne 0 ]; then
        echo "❌ 启动失败"
        exit 1
    fi
fi

# --- 7. 清理 ---
echo " 🧹 [6/6] 清理悬空镜像..."
docker image prune -f > /dev/null

echo "========================================"
echo " 🎉 更新流程执行完毕！"
[ -n "$UPDATED_LIST" ] && echo " 🔄 本次更新:$UPDATED_LIST"
[ -n "$SKIPPED_LIST" ] && echo " ⏭️  本来已是最新:$SKIPPED_LIST"
[ $UPDATED_COUNT -gt 0 ] && echo " 💾 旧版备份标签: backup-$NOW_TIME"
echo "========================================"
