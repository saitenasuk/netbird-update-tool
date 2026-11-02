#!/bin/sh
# 容器以 root 启动：先修好 $NB_DATA 的属主，再降权到 app 运行。
# 这样 bind mount 的目录不管是 root 建的、还是上一次跑崩留下的，都能直接起来。
set -e

DATA="${NB_DATA:-/data}"

if [ "$(id -u)" = "0" ]; then
    if ! mkdir -p "$DATA/keys" "$DATA/jobs" 2>/dev/null; then
        echo "错误：无法创建 $DATA 下的目录。检查挂载是否可写、以及宿主目录是否存在。" >&2
        exit 1
    fi

    if chown -R app:app "$DATA" 2>/dev/null; then
        exec su-exec app "$@"
    fi

    # 只读挂载、NFS root squash 之类改不动属主的情况：宁可降级，也不要起不来
    echo "警告：无法把 $DATA 的属主改成 app，改用 root 继续运行（数据仍会写入 $DATA）" >&2
fi

exec "$@"
