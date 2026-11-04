#!/bin/bash
# 客户端升级：用官方 install.sh 装最新版。
#
# 这个脚本会被「NetBird 更新台」通过局域网 SSH 非交互调用，所以：
#   - 不依赖终端，不弹任何交互菜单
#   - 不依赖 netbird 连接本身：脚本跑在本机，升级时断网不影响调用方
#   - 任何一步失败都要留下可用的 netbird，不能把机器停在「服务已停止」状态
set -euo pipefail

log() { echo "[$(date '+%F %T')] $*"; }

log "开始 NetBird 升级流程"
BEFORE=$(netbird version 2>/dev/null || echo "未安装")
log "当前版本: $BEFORE"

# 1. 先把安装脚本下载下来并确认非空。
#    不要写 `curl ... | sh`：curl 失败时 sh 会读到空内容并返回 0，
#    任务看起来成功，实际什么都没装。
TMP=$(mktemp /tmp/netbird-install.XXXXXX.sh)
trap 'rm -f "$TMP"' EXIT

if ! curl -fsSL --retry 3 --connect-timeout 15 https://pkgs.netbird.io/install.sh -o "$TMP"; then
    log "❌ 下载 install.sh 失败，netbird 保持原样运行"
    exit 1
fi
if [ ! -s "$TMP" ]; then
    log "❌ install.sh 是空文件，中止"
    exit 1
fi
log "install.sh 已下载（$(wc -c < "$TMP") 字节）"

# 2. 停服务（放在下载之后：网络不通时根本不会动服务）
log "停止 netbird"
systemctl stop netbird || log "  服务本来就没运行，继续"

# 3. 安装。失败时要把旧服务拉起来，别把机器留在断网状态。
if sh "$TMP"; then
    log "安装完成，重启服务"
    if ! systemctl restart netbird; then
        log "❌ 安装成功但重启失败，请手动检查 systemctl status netbird"
        exit 1
    fi
else
    log "❌ 安装失败，用旧版本把服务拉回来"
    systemctl start netbird || true
    exit 1
fi

# 4. 等一下再报结果，方便在工具的日志里核对
sleep 5
systemctl --no-pager --full status netbird | head -5 || true
AFTER=$(netbird version 2>/dev/null || echo "未知")
log "版本: $BEFORE → $AFTER"
log "升级流程结束"
