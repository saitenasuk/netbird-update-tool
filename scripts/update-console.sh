#!/bin/bash
# 用法：
#   ./update-console.sh                              # 交互式，弹出 whiptail 选择服务
#   ./update-console.sh management dashboard         # 命令行指定，给自动化用
#   ./update-console.sh signal relay                 # 只更新部分组件
# 不带参数时的行为和原来完全一致。

# ================= 配置区域 =================
# 1. DPanel 路径及堆栈配置
DPANEL_COMPOSE_DIR="/home/dockge/compose"
STACK_NAME="netbird"  # 你的 DPanel 堆栈文件夹名称

# 2. 数据路径配置
DATA_DIR="/home/netbird/management"
BACKUP_DIR="/home/netbird/backups"
RETENTION_DAYS=7          # 备份镜像保留天数

# 3. VPS 已通过 /etc/docker/daemon.json 配置全局镜像加速器
# ===========================================

# [Refactor] 移除了脚本中实际并未使用的 jq 和 curl，避免无意义的依赖阻塞
# [Refactor] whiptail 只在交互模式下才需要，带参数调用时不检查
REQUIRED_CMDS="docker tar"
[ $# -eq 0 ] && REQUIRED_CMDS="$REQUIRED_CMDS whiptail"
for cmd in $REQUIRED_CMDS; do
    if ! command -v $cmd &> /dev/null; then echo "❌ 未安装 $cmd"; exit 1; fi
done

echo "========================================"
echo " 📡 [1/6] 定位堆栈位置..."

TARGET_DIR="$DPANEL_COMPOSE_DIR/$STACK_NAME"

if [ ! -d "$TARGET_DIR" ]; then
    echo "❌ 路径错误：$TARGET_DIR 不存在"
    exit 1
fi

echo "✅ 堆栈目录: $TARGET_DIR"
cd "$TARGET_DIR" || exit 1

if [ ! -f "compose.yaml" ] && [ ! -f "docker-compose.yaml" ] && [ ! -f "docker-compose.yml" ]; then
    echo "❌ 错误：当前目录下未找到 compose.yaml 或 docker-compose.yaml"
    exit 1
fi

# --- 2. 选择服务 ---
# [Refactor] 带参数时直接采用参数，便于 Ansible 之类的非交互调用
if [ $# -gt 0 ]; then
    SELECTED_SERVICES="$*"
    echo "📦 命令行指定服务: $SELECTED_SERVICES"
else
    CHOICES=$(whiptail --title "NetBird 更新助手" --checklist \
    "请空格选择要更新的组件：" 15 60 4 \
    "management" "管理后台" ON \
    "dashboard" "前端UI" ON \
    "signal" "信号服务器" ON \
    "relay" "中继服务器" ON \
    3>&1 1>&2 2>&3)

    if [ $? != 0 ] || [ -z "$CHOICES" ]; then echo "已取消"; exit 0; fi
    SELECTED_SERVICES=$(echo $CHOICES | tr -d '"')
fi

# [Refactor] 服务名先校验，避免拼错导致后面拉 netbirdio/typo 失败再中断
ALLOWED_SERVICES="management dashboard signal relay"
for SVC in $SELECTED_SERVICES; do
    case " $ALLOWED_SERVICES " in
        *" $SVC "*) ;;
        *) echo "❌ 未知服务: $SVC（可选: $ALLOWED_SERVICES）"; exit 1 ;;
    esac
done

NOW_TIME=$(date +"%Y%m%d-%H%M%S")
CUTOFF_SEC=$(date -d "$RETENTION_DAYS days ago" +%s)

# --- 3. 清理过期备份 (改为基于 Tag 的清理) ---
echo "========================================"
echo " 🧹 [2/6] 检查过期备份镜像 (> $RETENTION_DAYS 天)..."

# [Refactor] 遵循 Docker 规范，使用 Repository:Tag 格式匹配备份镜像
docker images --format "{{.Repository}}:{{.Tag}}" | grep -E "^netbirdio/.*:backup-[0-9]{8}-[0-9]{6}$" | while read -r IMAGE_NAME; do

    # 提取日期部分并转换为秒数
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
# [Refactor] 增加 mkdir 失败退出机制，防止后续 tar 报错
mkdir -p "$BACKUP_DIR" || { echo "❌ 无法创建备份目录 $BACKUP_DIR"; exit 1; }

BACKUP_FILE="$BACKUP_DIR/netbird-data-$NOW_TIME.tar.gz"

# [Refactor] 动态解析父目录和目标文件夹，消除硬编码带来的脆弱性
DATA_PARENT_DIR=$(dirname "$DATA_DIR")
DATA_TARGET_DIR=$(basename "$DATA_DIR")

if tar -czf "$BACKUP_FILE" -C "$DATA_PARENT_DIR" "$DATA_TARGET_DIR" 2>/dev/null; then
    echo "       ✅ 数据备份完成: $BACKUP_FILE"
else
    echo "       ⚠️ 数据备份异常，脚本终止"
    exit 1
fi

# --- 5. 镜像处理 ---
echo " 🐳 [4/6] 处理镜像..."

# [Refactor] 增加状态标记。如果部分组件拉取失败，阻止后续 up -d，防止组件版本不一致
UPDATE_FAILED_FLAG=0
UPDATED_COUNT=0

for SERVICE in $SELECTED_SERVICES; do
    OFFICIAL_IMAGE="netbirdio/$SERVICE"
    BACKUP_TAG="backup-$NOW_TIME"

    echo "       -> $SERVICE"

    OLD_IMAGE_ID=$(docker image inspect "$OFFICIAL_IMAGE:latest" --format '{{.Id}}' 2>/dev/null)

    PULL_OUTPUT=$(docker pull "$OFFICIAL_IMAGE:latest" 2>&1)
    PULL_EXIT_CODE=$?

    if [ $PULL_EXIT_CODE -ne 0 ]; then
        echo "          ❌ 拉取失败，保留旧版"
        echo "          📝 错误信息: $(echo "$PULL_OUTPUT" | tail -1)"
        UPDATE_FAILED_FLAG=1
        continue
    fi

    NEW_IMAGE_ID=$(docker image inspect "$OFFICIAL_IMAGE:latest" --format '{{.Id}}' 2>/dev/null)

    if [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_IMAGE_ID" = "$NEW_IMAGE_ID" ]; then
        echo "          ⏭️  已是最新版本，跳过"
        continue
    fi

    # [Refactor] 使用标准 Tag 命名法替代新建 Repository
    if [ -n "$OLD_IMAGE_ID" ]; then
        echo "          💾 备份旧镜像为: $OFFICIAL_IMAGE:$BACKUP_TAG"
        docker tag "$OLD_IMAGE_ID" "$OFFICIAL_IMAGE:$BACKUP_TAG"
    else
        echo "          ℹ️  无旧镜像可备份（首次部署）"
    fi

    echo "          ✅ 更新成功"
    UPDATED_COUNT=$((UPDATED_COUNT + 1))
done

# --- 6. 重建容器 ---
echo " 🚀 [5/6] 重建容器..."

# [Refactor] 严格控制重建逻辑，防止集群组件版本撕裂
if [ $UPDATE_FAILED_FLAG -eq 1 ]; then
    echo "❌ 警告：部分镜像拉取失败！为防止组件版本不兼容，已中止容器重建流程。"
    echo "💡 提示：当前正在运行的容器未受影响，请检查网络后重试。"
    exit 1
elif [ $UPDATED_COUNT -eq 0 ]; then
    echo "✅ 所有选中的组件均已是最新版本，无需重建。"
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
if [ $UPDATED_COUNT -gt 0 ]; then
    echo " 💾 本次旧版镜像备份标签: backup-$NOW_TIME"
fi
echo "========================================"
