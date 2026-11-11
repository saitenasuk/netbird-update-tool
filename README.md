# NetBird 更新台

用一个网页，把散落在各台 Linux 机器上已有的更新脚本按顺序跑掉，日志实时回来看。不用再逐台 SSH 上去执行。

- **只做编排**：脚本已经在目标机上，工具只负责触发、排序、盯日志、对比版本；升级逻辑全在你的脚本里
- **零第三方依赖**：Python 3.11+ 标准库 + 系统 `ssh`，一个容器跑起来
- **断线型机器**（开了子网代理、或通过 netbird 隧道连的那台）会等它断线重连、等后台 systemd 单元结束，再读真实版本
- 按机器优先级逐台执行，一台失败默认不影响其他，最后汇总哪几台失败

## 快速开始

在**任意一台有 Docker 的 Linux 机器**上执行（不一定是被更新的机器）。

```bash
# 1. 写 compose 文件
mkdir -p ~/netbird-update-tool && cd ~/netbird-update-tool
cat > docker-compose.yml <<'EOF'
services:
  nbtool:
    image: ghcr.io/saitenasuk/netbird-update-tool:latest
    container_name: netbird-update-tool
    restart: unless-stopped
    # 关键：复用宿主机的路由表和 netbird 接口，容器才能像宿主机一样访问 100.x 和局域网
    network_mode: host
    environment:
      NB_BIND: "0.0.0.0"          # 改成局域网 IP 或 100.x 可以缩小暴露面
      NB_PORT: "8770"
      NB_COM_PASSWORD: "换成你的密码"   # ← 改这里
      TZ: "Asia/Shanghai"
    volumes:
      - ./data:/data
EOF

# 2. 数据目录（属主由容器启动时自动修正，这里只要把目录建出来）
mkdir -p data

# 3. 起服务
docker compose up -d

# 4. 看是否起来了
docker compose ps
curl -s http://127.0.0.1:8770/healthz     # {"ok": true}
```

浏览器打开 `http://<主机>:8770`，会跳到登录页。用户名 `admin`，密码就是 compose 里那行 `NB_COM_PASSWORD`。

登录后拿到一个 12 小时有效的会话 cookie；右上角「退出」可以立即失效。没设 `NB_COM_PASSWORD` 时不显示登录页（仅本机调试用）。

镜像在 GHCR 上是公开的，不需要登录。升级版本：

```bash
docker compose pull && docker compose up -d
```

### 不想用 compose

```bash
mkdir -p ~/netbird-update-tool/data && cd ~/netbird-update-tool

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

## 第一次使用

1. **密钥** → 添加密钥 → 选「让服务端生成」→ 得到公钥
2. 把界面上给出的三行命令在目标机上执行（`~/.ssh` 权限 700、`authorized_keys` 600）
3. **机器** → 添加机器 → 填地址、用户、密钥、脚本 → 点「测试连接」→ 保存
   保存时会自动检查一次：连通性、每个脚本是否可执行、当前 netbird 版本
4. **全部更新** 开始；每台机器更新完，版本会自动回填到列表里

脚本在「添加机器」里按行填，每行三个格子：

| 格子 | 说明 |
|---|---|
| 命令 | 目标机上的绝对路径，必填 |
| 参数 | 原样传给脚本，按空格拆分、支持引号，可留空 |

从上到下依次执行，可以用 ↑ ↓ 调整顺序。

比如控制台脚本要更新全部组件：

```
命令  /root/netbird/update-console.sh
参数  management dashboard signal relay
```

工具不理解这些参数是什么意思，只负责传过去——要更新哪些组件、什么顺序，全在脚本里决定。

## 机器状态是怎么来的

- **添加 / 编辑机器**：自动做完整检查（连通性 + 每个脚本 `test -x` + 版本）
- **每次任务结束**：用任务里已经读到的版本回填，不再额外连一次
- **手动刷新**：机器面板标题栏的 `↻` 按钮，只查连通性和版本（不再查脚本）
- 结果存在数据库里，**重启服务不会全变回「未检查」**；每行会显示「检查于 X 前」

## 优先级

每台机器有一个优先级数字，**小的先执行**（1、2、3 这样编号，同优先级按添加顺序）。
「这台是工具所在主机」勾上后永远排在最后——更新自己会把工具自己的网络带断。

任务默认是**跑完所有机器**：某台失败只标红，后面的继续跑，结束时汇总「失败 N 台：xxx」。
「全部更新」的确认框里可以勾掉「某台失败后继续」，改成遇错即停。

## 地址怎么填

| 地址类型 | 说明 |
|---|---|
| 局域网 / 公网 IP | 推荐。升级 netbird 不会断开这条 SSH，**不需要**勾「后台执行」 |
| netbird 的 100.x | 升级该机会断开 SSH，界面会红字警告。必须同时勾「升级会断线，后台执行」 |
| 本机 | 勾「这台是工具所在主机」，地址填 `127.0.0.1`。容器用 host 网络，回环就是宿主机；这条 SSH 不经过任何网卡，升级自己的 netbird 也不会断。sshd 如果只监听特定网卡，就填那个网卡地址 |

## 登录用户不是 root 的机器

云镜像（OCI / AWS / GCP 的 Ubuntu）通常只能用 `ubuntu` 这类用户登录，root 不允许直接 SSH。
这类机器勾上**用 sudo 执行**即可，工具会把每条远端命令套成 `sudo -n <命令>`，脚本仍然以 root 跑。

前提是目标机配了免密 sudo（云镜像默认就有），先确认：

```bash
sudo -n true && echo ok
```

如果没有，加一条 sudoers：

```bash
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/nbtool
```

想收紧到只放行更新脚本（不带参数限制，脚本加参数仍然可用）：

```
ubuntu ALL=(root) NOPASSWD: /root/netbird/update-client.sh
```

用 `-n` 而不是等密码，是因为工具没有交互终端：没配免密 sudo 会立刻报错，而不是卡住。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NB_BIND` | `127.0.0.1` | 监听地址。容器里用 `0.0.0.0` |
| `NB_PORT` | `8770` | 端口 |
| `NB_DATA` | `data` | 数据目录（`db.sqlite3`、`keys/`、`known_hosts`、`jobs/`） |
| `NB_COM_PASSWORD` | 无 | Basic 认证密码。不设则**不校验**，只适合本机调试 |
| `NB_AUTH_USER` | `admin` | 认证用户名 |
| `NB_SEED` | `$NB_DATA/seed.json` | 首次启动（库为空）时导入的初始清单 |

## 脚本用什么写都行

工具不解释脚本，只把它当**可执行文件**调用，所以 bash / sh / fish / Python 都支持：

- 必须有 shebang（`#!/usr/bin/env fish` 这种），并且 `chmod +x`
- 参数按空格传进去，fish 里就是 `$argv`
- 远端非交互 SSH **不会**加载 `config.fish`，所以别依赖 alias / abbr / 你在配置里加的 PATH；用绝对路径，或者在脚本开头自己设

> 这一步是实测过的：本地集成测试跑在一台 root 登录 shell 为 fish 的机器上（`test_quoting_survives_remote_login_shell`）。

## 安全边界

- 界面只能触发数据库里登记过的脚本路径，以及脚本声明的选项名，**没有执行任意命令的入口**
- 私钥以 600 权限明文存在 `/data/keys`，没有主密码加密——能读 volume 就能读私钥。缓解手段是只绑内网 + Basic 认证 + 目录归 root
- 无 TLS，依赖 netbird 的 WireGuard 或前置反向代理
- 首次连接目标机是 TOFU（`StrictHostKeyChecking=accept-new`），指纹写入 `/data/known_hosts`
- 中止会 kill 掉正在跑的那条 ssh 进程；但已经 `systemd-run` 到后台的升级单元收不回来，这类机器只能事后看日志

## 排错

**容器一直重启，日志里是 `PermissionError: '/data/keys'`**
旧镜像以 uid 1000 直接运行，碰到 root 建的 `data/` 就会起不来。现在的镜像以 root 启动、把 `/data` 的属主交给 `app` 后降权运行，`docker compose pull && docker compose up -d` 即可。

**首次连接提示 host key 校验失败**
指纹存在 `/data/known_hosts`，删掉数据卷就会重新 TOFU。

## 开发

```bash
python3 app.py                      # 本机跑，数据在 ./data
python3 -m unittest discover tests  # 单测（不需要机器）
```

集成测试需要一台本地 sshd（`/tmp/sshd/client_key`，监听 127.0.0.1:2222），没准备好会自动跳过。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` / `/api/logout` | 登录拿会话 cookie / 退出 |
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
