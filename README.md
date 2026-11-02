# NetBird 更新台

用一个网页，把散落在各台 Linux 机器上已有的更新脚本按顺序跑掉，日志实时回来看。不用再逐台 SSH 上去执行。

- **只做编排**：脚本已经在目标机上，工具只负责触发、排序、盯日志、对比版本
- **零第三方依赖**：Python 3.11+ 标准库 + 系统 `ssh`，一个容器跑起来
- **断线型机器**（开了子网代理、或通过 netbird 隧道连的那台）会等它断线重连、等后台 systemd 单元结束，再读真实版本
- 控制台分组优先，失败即中止；其余分组逐台执行，一台失败不影响其他

## 快速开始

在**任意一台有 Docker 的 Linux 机器**上执行（不一定是被更新的机器）。

```bash
# 1. 只取部署用的 compose 文件，不需要 clone 整个仓库
mkdir -p ~/netbird-update-tool && cd ~/netbird-update-tool
curl -fsSLO https://raw.githubusercontent.com/saitenasuk/netbird-update-tool/main/docker-compose.yml

# 2. 数据目录（容器以 uid 1000 运行，目录必须归它）
mkdir -p data && sudo chown 1000:1000 data

# 3. 写密码。compose 会自动读同目录的 .env，
#    之后 ps / down / pull 都不用再 export
cat > .env <<'EOF'
NB_COM_PASSWORD=换成你的密码
EOF
chmod 600 .env

# 4. 起服务
docker compose up -d

# 5. 看是否起来了
docker compose ps
curl -s http://127.0.0.1:8770/healthz     # {"ok": true}
```

打开 `http://<主机>:8770`，用户名 `admin`，密码就是 `.env` 里的 `NB_COM_PASSWORD`。

镜像在 GHCR 上是公开的，不需要登录。升级版本：

```bash
docker compose pull && docker compose up -d
```

### 不想用 compose

```bash
mkdir -p ~/netbird-update-tool/data && cd ~/netbird-update-tool
sudo chown 1000:1000 data

docker run -d --name netbird-update-tool \
  --restart unless-stopped \
  --network host \
  -e NB_BIND=0.0.0.0 -e NB_PORT=8770 \
  -e NB_COM_PASSWORD='换成你的密码' \
  -e TZ=Asia/Shanghai \
  -v "$PWD/data:/data" \
  ghcr.io/saitenasuk/netbird-update-tool:latest
```

### 自己构建镜像

```bash
git clone https://github.com/saitenasuk/netbird-update-tool.git
cd netbird-update-tool
docker build -t netbird-update-tool .
```

仓库里没有 `build:` 指令，`docker compose up` 只会拉 GHCR 上的镜像。

> `network_mode: host` 是必须的：容器要复用宿主机的路由表和 netbird 接口，才能访问 100.64.0.0/10 和子网代理后面的机器。

## 第一次使用

1. **密钥** → 添加密钥 → 选「让服务端生成」→ 得到公钥
2. 把界面上给出的三行命令在目标机上执行（`~/.ssh` 权限 700、`authorized_keys` 600）
3. **机器** → 添加机器 → 填地址、用户、密钥、脚本 → 点「测试连接」→ 保存
4. **体检** 看每台的当前版本，**全部更新** 开始

脚本一行一个，顺序即执行顺序：

```
/root/netbird/update-console.sh | 更新控制台 | management,dashboard,signal,relay
/root/netbird/update-client.sh  | 更新客户端 |
```

第三段是选项名，更新时会渲染成复选框，选中项作为参数传给脚本。

## 地址怎么填

| 地址类型 | 说明 |
|---|---|
| 局域网 / 公网 IP | 推荐。升级 netbird 不会断开这条 SSH |
| netbird 的 100.x | 升级该机会断开 SSH，界面会红字警告。必须同时勾「升级会断线，后台执行」 |
| 本机 | 勾「这台是工具所在主机」，分组内排最后，避免更新自己把后面的机器全带停 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NB_BIND` | `127.0.0.1` | 监听地址。容器里用 `0.0.0.0` |
| `NB_PORT` | `8770` | 端口 |
| `NB_DATA` | `data` | 数据目录（`db.sqlite3`、`keys/`、`known_hosts`、`jobs/`） |
| `NB_COM_PASSWORD` | 无 | Basic 认证密码。不设则**不校验**，只适合本机调试 |
| `NB_AUTH_USER` | `admin` | 认证用户名 |
| `NB_SEED` | `$NB_DATA/seed.json` | 首次启动（库为空）时导入的初始清单 |

## 安全边界

- 界面只能触发数据库里登记过的脚本路径，以及脚本声明的选项名，**没有执行任意命令的入口**
- 私钥以 600 权限明文存在 `/data/keys`，没有主密码加密——能读 volume 就能读私钥。缓解手段是只绑内网 + Basic 认证 + 目录归 root
- 无 TLS，依赖 netbird 的 WireGuard 或前置反向代理
- 首次连接目标机是 TOFU（`StrictHostKeyChecking=accept-new`），指纹写入 `/data/known_hosts`

## 开发

```bash
python3 app.py                      # 本机跑，数据在 ./data
python3 -m unittest discover tests  # 单测（不需要机器）
```

集成测试需要一台本地 sshd（`/tmp/sshd/client_key`，监听 127.0.0.1:2222），没准备好会自动跳过。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 机器 + 密钥 + 任务 + 当前任务 |
| GET/POST | `/api/hosts` | 列表 / 新增 |
| PUT/DELETE | `/api/hosts/{id}` | 修改 / 删除 |
| POST | `/api/hosts/test` | 用未保存的表单值测试连接 |
| POST | `/api/hosts/{id}/check` | 体检：连通性、脚本可执行、版本 |
| GET | `/api/hosts/{id}/bootstrap` | 该机的公钥安装命令 |
| GET/POST/DELETE | `/api/keys` | 密钥管理 |
| POST | `/api/jobs` | 创建任务 |
| GET | `/api/jobs/{id}/events?from=N` | SSE 日志流（断线后从 N 补齐） |
| GET | `/api/jobs/{id}/log` | 纯文本日志 |
| POST | `/api/jobs/{id}/abort` | 中止（会杀掉正在跑的脚本） |
