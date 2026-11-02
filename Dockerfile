# 运行时不装编译工具，只装 ssh 客户端和时区
FROM python:3.12-alpine

RUN apk add --no-cache openssh-client tzdata su-exec \
    && addgroup -g 1000 app \
    && adduser -D -u 1000 -G app -h /data app

WORKDIR /app
COPY app.py ./
COPY nbtool ./nbtool
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod -R a+rX /app && chmod +x /usr/local/bin/docker-entrypoint.sh

ENV HOME=/data \
    NB_DATA=/data \
    NB_BIND=0.0.0.0 \
    NB_PORT=8770 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data/keys /data/jobs && chown -R app:app /data
VOLUME ["/data"]

# 以 root 启动，入口脚本把 /data 的属主改成交给 app 后再降权执行。
# 直接 USER app 的话，宿主机上 root 建的 bind mount 会让容器起不来。
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request, os; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('NB_PORT', '8770') + '/healthz', timeout=3)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "python3", "app.py"]
