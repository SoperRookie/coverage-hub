# 被测项目接入指南

从零把一个 Java 服务接入运行期覆盖率采集。全文按**真实操作顺序**组织，每一步都给出
「做什么 / 怎么验证 / 错了会怎样」。

| 章节 | 谁看 | 做几次 |
|---|---|---|
| [§0 先读这一页](#0-先读这一页) | 所有人 | —— |
| [§1 接入前的准备](#1-接入前的准备一次性) | 平台 / 运维 | 一次 |
| [§2 搭建 hub](#2-搭建-hub一次性) | 平台 / 运维 | 一次 |
| [§3 接入一个服务](#3-接入一个服务每个服务各做一遍) | 服务负责人 + 发版同学 | 每个服务一次 |
| [§4 完整实操记录](#4-一次完整的实操记录) | 第一次接入的人 | 照着敲一遍 |
| [§5 接发版流水线](#5-接发版流水线) | 发版同学 | 每个服务一次 |
| [§6 构建期覆盖率（可选）](#6-构建期覆盖率可选要改-pom) | 研发 | 可跳过 |
| [§7 验收清单](#7-接入验收清单) | 接入的人 | 每次接入 |
| [§8 日常运维](#8-日常运维day-2) | 运维 | 持续 |
| [§9 常见问题](#9-常见问题) | 出问题时 | —— |
| [§10 撤下监控](#10-撤下监控) | 不再需要时 | —— |

---

## 0. 先读这一页

### 一句话原理

被测 JVM 启动时挂上 JaCoCo 的 `jacocoagent.jar`（通过环境变量 `JAVA_TOOL_OPTIONS`
注入，**不改代码、不改 pom、不改镜像**）。agent 在内存里记录哪些指令被执行过；hub
定时连过去把这份记录（exec）拉回来，配上 agent 自己落盘的那份 class，出 JaCoCo
原生报告，并在看板上展示。发版前 hub 把这一版的数据结算归档，新版本从零开始。

### 谁装什么

**服务端全公司只有一个。** 每接一个服务，被测机器上多出来的东西只有一个
`jacocoagent.jar`（还能从 hub 现下）；发版节点上一行 Python 都不需要装。

| 机器 | 需要什么 | 不需要什么 |
|---|---|---|
| **hub 那一台**（唯一的服务端） | Python 3.7+、`java`（8+）、`covhub.py`、`lib/` 下的两个 jar（版本库自带）、一份配置文件 | —— |
| **被测服务所在机器** | `jacocoagent.jar`（`covhub-client.sh fetch-agent` 下载）；pull 通道要能被 hub 连上 agent 端口 | Python、covhub、配置文件 |
| **发版节点 / 流水线** | `curl`（用 `integration/covhub-client.sh` 包一层） | Python、java、配置文件、历史 class 产物 |
| **被测项目本身** | **什么都不用改** | 代码、pom、Dockerfile、启动脚本 |

### 角色分工

| 角色 | 负责 | 对应章节 |
|---|---|---|
| 平台 / 运维 | 搭 hub、规划端口与网络放行、分发客户端脚本、日常运维 | §1、§2、§8 |
| 服务负责人 | 在 hub 配置里加一条、决定 `includes` 范围、验证接入 | §3、§4、§7 |
| 发版同学 | 把 `predeploy` / `upload-classes` 排进发版流程 | §5 |
| 研发 | 只有想要构建期（单测）覆盖率时才参与 | §6 |

### 三条不能违背的规矩

后面会反复出现，先记住：

1. **`predeploy` 必须在停服之前。** 服务一停，agent 随进程消失，那段覆盖率**永久丢失**，没有任何补救手段。
2. **class 必须跟着版本换。** JaCoCo 按类的 CRC64 指纹匹配 exec 和 class，对不上时**不报错**，报告直接全红。
3. **`includes` 改了要重启服务；`reportExcludes` 改了只需重出报告。** 前者决定是否插桩，被排除的类连数据都不产生。

---

## 1. 接入前的准备（一次性）

### 1.1 版本与前提

| 项 | 要求 | 说明 |
|---|---|---|
| hub 的 Python | 3.7+ | 只用标准库；YAML 配置额外要 `pip install PyYAML`，用 JSON 配置则零依赖 |
| hub 的 `java` | 8+ | 跑 `lib/jacococli.jar` 出报告。`java -version` 能出来即可，不需要 JDK |
| 被测服务的 JVM | 8+ | `lib/jacocoagent.jar` 的 manifest 写着 `Java-Version: 8` |
| JaCoCo 版本 | 0.8.16.1（`lib/` 自带） | 能插桩多高版本的 class 由它决定。被测服务用了比它更新的 Java，agent 启动时会报 `Unsupported class file major version`，届时按 `CLAUDE.md` 里的说明换 `lib/` 下两个 jar |
| 被测机器 | 能改环境变量或 JVM 参数 | 这是唯一的硬要求。容器、systemd、K8s、裸 `java -jar` 都满足 |
| 发版节点 | `curl` | 就这一个 |

### 1.2 路径在哪台机器上 —— 先建立这个心智模型

配置文件里的路径分属两台机器，**这是接入时出错最多的地方**。规则只有一条：
**agent 用的路径在被测端，出报告用的路径在 hub。**

| 配置项 | 在哪台机器上 | 为什么 | 容器场景写什么 |
|---|---|---|---|
| `jacocoAgent` | **被测端** | 它只用来拼进 `-javaagent:` 参数串，jar 是被测 JVM 加载的 | 容器内路径，如 `/opt/jacoco/jacocoagent.jar` |
| `classDumpDir` | **被测端** | agent 把加载到的 class 落在这里 | 容器内路径，取的时候 `docker cp` / `kubectl cp` |
| `jacocoCli` | hub | hub 自己跑 `java -jar` 出报告 | hub 本机路径 |
| `dataDir` | hub | 采集产物全在这 | hub 本机路径 |
| `classfiles` | hub | 报告是 hub 出的，class 必须在 hub 上。用 `upload-classes --retarget` 会自动填 | hub 本机路径 |
| `sourcefiles` | hub | 同上，下钻源码行时读 | hub 本机路径 |
| `address` / `port` | 被测端的**对外**地址 | hub 连过去的目标。容器要写宿主机 IP + 映射出来的端口 | 宿主机 IP:映射端口 |
| `bindAddress` | 被测端的**监听**地址 | agent 在被测 JVM 里监听 | 一律 `0.0.0.0` |
| `collect.advertiseAddress` | **被测端能访问到的** hub 地址 | push 通道 agent 连回来用 | hub 的对外 IP / 域名，不是 `127.0.0.1` |

一个小验证：`jacocoAgent` 填的路径 hub 本机不存在也没关系 —— `agent-opts` 照样输出
正确的参数串；只有 `fetch-agent`（从 hub 下载 agent）会因为找不到文件而 404。

### 1.3 网络放行清单

只有两条 TCP 连接需要打通，方向取决于通道：

| 通道 | 谁连谁 | 端口 | 谁开 | 认证 |
|---|---|---|---|---|
| **pull**（默认） | hub → 被测端 | 被测端的 agent 端口（默认 6300） | 被测端 | **无**，靠防火墙限制来源 |
| **push** | 被测端 → hub | hub 的收集端口（`collect.port`，默认 6400） | hub | **无**，靠防火墙限制来源 |
| 两者都要 | 发版节点 / 浏览器 → hub | hub 的 HTTP 端口（默认 8900） | hub | `serve.token` |
| 两者都要 | 被测端 → hub（可选） | 8900，`fetch-agent` 下载 agent 用 | hub | `serve.token` |

**agent 端口和收集端口都没有认证**，谁连上都能拉数据、清零计数器。三条底线：

- 不暴露到公网
- 用安全组 / 防火墙把来源限定在 hub 与被测端所在网段
- 8900 一定配 `serve.token`（§2.3）

### 1.4 端口规划

**pull 通道下，一台宿主机上的多个服务必须用不同 agent 端口。** 提前定好，写进表里避免撞车：

| 服务 | 宿主机 | agent 端口 |
|---|---|---|
| order-service | 10.0.1.21 | 6300 |
| user-service | 10.0.1.21 | 6301 |
| pay-service | 10.0.1.22 | 6300 |

容器里的服务可以都用容器内 6300，映射到宿主机的不同端口即可（`-p 6301:6300`），
配置里 `port` 写宿主机那个。

push 通道不需要规划：所有服务都连 hub 的同一个 6400，靠 `sessionid`（= 服务名）区分。

### 1.5 命名规范：多环境、多服务

hub 里的服务名是所有东西的主键：配置项、`data/<service>/` 目录、API 的 `service=`
参数、push 通道的 `sessionid`、看板卡片。定了就别改 —— 改名等于换了一个服务，历史
数据留在旧目录里不再显示。

**一个 hub 管一个环境**是最省心的。要用一个 hub 管多个环境，用后缀区分：

```
order-service-sit
order-service-uat
```

两条配置各自独立，互不影响；发版流水线里传对应环境的名字即可。

服务名只用字母、数字、`-`、`_`、`.` —— 它会出现在 URL 路径和目录名里。

---

## 2. 搭建 hub（一次性）

### 2.1 选一台机器

**只需要一台**，整个团队共用。要求：

- 能连到所有 pull 通道服务的 agent 端口；push 通道的被测端能连到它
- 装有 Python 3.7+ 和 java 8+
- 磁盘留出余量：`data/` 会持续增长（§8.2 有估算）

通常放测试环境的一台管理机。被测服务和发版节点都不在这台机器上跑任何 covhub
进程 —— 它们通过 HTTP 让这台机器干活。

### 2.2 部署

```bash
sudo mkdir -p /opt/coverage-hub
sudo chown "$USER" /opt/coverage-hub
cd /opt/coverage-hub

git clone <本仓库> .                # covhub.py、integration/、lib/ 下两个 jar 都在版本库里
python3 --version                   # ≥ 3.7
java -version                       # ≥ 8
pip3 install PyYAML                 # 用 YAML 配置才需要；装不上就用 targets.json

python3 covhub.py init              # 生成 targets.yaml 模板（--json 生成 JSON 版）
```

`init` 生成的是带注释的模板，先不用改 `services`，把 hub 级别的几项配好即可：

```yaml
jacocoAgent: ./lib/jacocoagent.jar      # 被测端路径！容器场景改成容器内路径（§1.2）
jacocoCli: ./lib/jacococli.jar
dataDir: ./data

serve:
  port: 8900
  token: ""                             # 下一步填

watch:
  intervalSeconds: 300                  # 轮询间隔，也是断代时数据丢失的上界

collect:                                # 只有要用 push 通道才需要；不用就整段删掉
  port: 6400
  bindAddress: 0.0.0.0
  advertiseAddress: covhub.internal     # 被测端能访问到的 hub 地址
  dumpTimeoutSeconds: 20
```

相对路径一律相对配置文件所在目录解析，整个目录可以原样搬到别的机器。

> `jacocoAgent` 的默认值 `./lib/jacocoagent.jar` 只在「被测服务和 hub 同机、且工作
> 目录相同」时能用。绝大多数场景要改成被测端的绝对路径，例如
> `/opt/jacoco-lib/jacocoagent.jar`（裸机）或 `/opt/jacoco/jacocoagent.jar`（容器内）。
> 不同服务放在不同位置时，以多数为准，个别服务在注入时手工改参数串里的 jar 路径即可
> —— 参数串里只有这一项是可以手改的。

### 2.3 令牌

生成一串随机字符串，填进 `serve.token`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

这个令牌是 8900 的**唯一门禁**，控制 API 和静态报告目录都归它管。不配就是**任何能
连上 8900 的人都能拉数据、清零计数器、并下载 `data/` 底下的一切** —— 含 `artifacts/`
里线上跑的那份字节码（反编译即源码）、`exec/` 里不可再生的执行轨迹。启动日志里会
用一行 `[未设置 serve.token：写接口与 data/ 整个目录都对外敞开]` 提醒。

记下来，发版节点和浏览器都要用：

| 场景 | 怎么带 |
|---|---|
| 浏览器看看板 | 第一次打开 `http://<hub>:8900/?token=<令牌>`。hub 种一个 `HttpOnly` Cookie 再 302 跳回干净地址，之后点报告、翻历史版本都不必再带 |
| `curl` / 流水线 | `-H "X-Covhub-Token: <令牌>"` |
| `covhub-client.sh` | 设环境变量 `COVHUB_TOKEN`，脚本自己加头 |

也可以不写进配置文件，改给 hub 进程设环境变量 `COVHUB_TOKEN` —— 配置文件要进配置
中心、不想让令牌落盘时用这种。

### 2.4 起服务

一个进程包含全部三件事：看板、控制 API、定时采集。**push 通道还额外要求收集端和
采集在同一进程**，所以一律带 `--with-watch`：

```bash
nohup python3 covhub.py serve --with-watch --port 8900 > covhub.log 2>&1 &
```

启动日志应该长这样：

```
[10:00:01] push 收集端已监听 0.0.0.0:6400（等待 output=tcpclient 的 agent 连入）   ← 配了 collect.port 才有
[10:00:01] 采集线程已启动，每 300 秒轮询一次
[10:00:01] covhub 1.3.0 已启动： http://127.0.0.1:8900/  （根目录 /opt/coverage-hub/data）
[10:00:01] 控制 API： http://127.0.0.1:8900/api/health
```

第四行末尾如果多了 `[未设置 serve.token：……]`，回 §2.3。

生产化做成一个 systemd unit：

```ini
# /etc/systemd/system/covhub.service
[Unit]
Description=covhub —— 覆盖率看板 / 控制 API / 采集
After=network.target

[Service]
WorkingDirectory=/opt/coverage-hub
ExecStart=/usr/bin/python3 /opt/coverage-hub/covhub.py serve --with-watch --port 8900
Restart=always
RestartSec=5
# 令牌不想写进配置文件就放这里
# Environment=COVHUB_TOKEN=xxxx

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now covhub
sudo journalctl -u covhub -f          # 看日志
```

> 采集轮询和 API 在同一个进程里，写操作会互相排队 —— 这正是要的：结算和轮询不会
> 打架。想拆成两个进程也行（`serve` 不带 `--with-watch`，另起一个 `watch`），但那样
> 就有两个进程在写同一份数据，且 **push 通道在这种拆法下不工作**（连接握在 `serve`
> 进程手上，`watch` 进程够不着）。只在你确定不用 push、且采集耗时确实拖慢了 API 时
> 才这么拆。

### 2.5 验证 hub

```bash
curl -s http://127.0.0.1:8900/api/health
# {"ok": true, "version": "1.3.0", ...}

curl -s http://127.0.0.1:8900/order-service/state.json
# {"ok": false, "error": "..."}  ← 401，说明令牌生效了；返回 404 或文件内容都说明没生效

curl -s -H "X-Covhub-Token: <令牌>" http://127.0.0.1:8900/api/status
# {"ok": true, "services": []}

python3 covhub.py status
# 服务                   连通     指令%     分支%     版本       最后更新
# ------------------------------------------------------------------------------
```

浏览器打开 `http://<hub>:8900/?token=<令牌>` 应能看到空看板。

### 2.6 把客户端脚本发给各团队

```bash
# 发版节点 / 被测机器上，只要这一个脚本 + curl
sudo cp integration/covhub-client.sh /opt/bin/covhub-client.sh
sudo chmod +x /opt/bin/covhub-client.sh
```

用之前设两个环境变量：

```bash
export COVHUB_URL=http://<hub>:8900
export COVHUB_TOKEN=<serve.token>
/opt/bin/covhub-client.sh health
# {"ok": true, ...}
```

脚本是 POSIX `sh`，任何 Linux 都能跑。它的每个子命令都对应 hub 的一个 HTTP 接口，
**hub 返回非 2xx 时一律非零码退出** —— 放进 `set -e` 的部署脚本里，出错就会停下来。

子命令一览（`covhub-client.sh` 不带参数也会打印）：

```
health                                         存活探测
status [service]                               连通性与最新覆盖率
agent-opts <service>                           打印 -javaagent 参数串
fetch-agent [目标路径]                          下载 jacocoagent.jar（默认存到 ./jacocoagent.jar）
dump <service>                                 拉一次快照（累加）
predeploy <service> [version] [--allow-missing] 结算归档，停服之前调
report <service>                               用已有 exec 重出报告
diagnose <service> [version]                   exec 与 class 指纹是否对得上
retarget <service> <version> [classfiles,...]  改配置里的版本与 class 路径
upload-classes <service> <version> <包> [--retarget]  上传 class 产物
fetch-classes <service> <version> <目标目录>     取回某版本的 class 产物
wait-online <service> [超时秒数，默认 120]       等新实例的 agent 就绪
```

---

## 3. 接入一个服务（每个服务各做一遍）

下面以 `order-service` 为例，按**实际能执行的顺序**走：先在 hub 配置里登记 → 被测端
取 agent 和参数串 → 注入并启动 → 把 class 送回 hub → 验证。

### Step 1 · 确认前提

三个条件，缺一个就接不了：

- [ ] 服务是 JVM 应用，且**能设置环境变量或 JVM 参数**
- [ ] 网络能通：pull 通道 hub 能连到被测端的 agent 端口；连不通就改 push 让被测端连回来（Step 2）
- [ ] 能在被测机器上执行一条命令（打包一个目录并 curl 上传）

**不需要拿到构建产物。** v1.1.0 起 class 由 agent 自己落盘（`classDumpDir`），出报告用的
就是运行时那一份 —— 指纹必然匹配，不需要被测项目的构建流水线配合。

只有一种情况仍需构建产物：想在报告里**下钻到源码行**，那还要配 `sourcefiles` 指向
对应版本的源码（Step 8）。只看类和方法级别的覆盖数字则不需要。

### Step 2 · 选通道

| | pull（默认） | push |
|---|---|---|
| 方向 | hub 主动连 agent | agent 主动连回 hub |
| agent 输出 | `output=tcpserver` | `output=tcpclient` |
| 被测端要开端口 | 要（6300） | 不要 |
| 多副本 | 每个副本要配一条 | 天然汇聚，扩缩容不用改配置 |
| 断代检测抓的是 | 进程重启（自动结算上一周期） | 混版本（只告警不结算） |
| CLI 单独跑 `status` | 能看到 | 看不到（显示 `?`，要看 API 或看板） |
| 适合 | 单实例、裸机、hub 能访问被测端 | K8s、容器网络只出不进、跨防火墙只有单向可达、多副本自动扩缩 |

拿不准就用 pull；K8s 多副本直接用 push。**通道只在 hub 配置里定**（`channel:`），
被测端的注入方式两者完全一样，参数串由 `agent-opts` 自动切换。

### Step 3 · 在 hub 配置里加一条

编辑 hub 上的 `targets.yaml`，在 `services:` 下加一项：

```yaml
services:
  - name: order-service
    version: "1.4.2"          # 版本号一律加引号，裸写的 1.4 会被读成数字
    channel: pull
    address: 10.0.1.21        # pull：hub 连过去的地址（被测服务所在主机的对外地址）
    port: 6300                # pull：agent 端口（容器场景是映射到宿主机的那个）
    bindAddress: 0.0.0.0      # pull：agent 在被测端监听的地址
    includes:
      - com.example.order.*
    excludes: []
    classDumpDir: /tmp/covhub-classes/order-service     # 被测端路径
    classfiles:               # 先随便填一个，Step 6 upload-classes --retarget 会自动改
      - ./data/order-service/artifacts/1.4.2
    sourcefiles: []           # 可选，见 Step 8
    reportExcludes:
      - com/example/order/**/dto/**
      - com/example/order/*/mapper/**
    sourceEncoding: UTF-8
```

push 通道的写法（去掉 `address` / `port` / `bindAddress`，hub 级别要有 `collect` 段）：

```yaml
  - name: order-service
    version: "1.4.2"
    channel: push
    includes:
      - com.example.order.*
    classDumpDir: /tmp/covhub-classes/order-service
    classfiles:
      - ./data/order-service/artifacts/1.4.2
```

> 用 `targets.json` 的话，同样一条写成 JSON 对象放进 `services` 数组，字段名完全一样。

**配置每次请求、每轮轮询都重读**，改完不用重启 hub。

#### 字段逐项说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 主键，见 §1.5 |
| `version` | 建议 | 当前在线版本。`predeploy` 不带版本号时用它做归档目录名；pull 通道还会作为 agent 的 `sessionid`。发版流水线里的 `retarget` / `upload-classes --retarget` 会自动更新它 |
| `channel` | 否 | `pull`（默认）或 `push` |
| `address` / `port` | pull 必填 | hub 连过去的目标。push 不需要 |
| `bindAddress` | 否 | agent 监听地址，默认 `0.0.0.0`。容器 / 跨机**必须** `0.0.0.0`；只有 hub 与服务同机时才可以 `127.0.0.1` |
| `includes` | 强烈建议 | 传给 agent，决定**哪些类插桩**。类名用 `.` 分隔，`*` 匹配任意字符（含 `.`），`?` 匹配一个字符。多项工具会用 `:` 拼起来。**不配就插桩所有类**，把 Spring、MyBatis 全插一遍，启动慢几倍还撑大 exec |
| `excludes` | 否 | 同上，反向。被排除的类连数据都不产生，**事后找不回**，改了要重启服务 |
| `classDumpDir` | 强烈建议 | **被测端**路径。agent 把加载到的每个 class 落在这里，文件名自带指纹（`OrderService.3f2a91c4e8b70d15.class`）。不配就得靠构建期归档 class，见 §6 |
| `classfiles` | 是 | **hub 上**出报告用的 class 目录（可多个），必须是运行中那一份产物。走 `upload-classes --retarget` 会自动填成 `data/order-service/artifacts/<版本>/` |
| `sourcefiles` | 否 | **hub 上**的源码根目录（`src/main/java` 那一级），配了才能下钻到行 |
| `reportExcludes` | 否 | 报告端过滤，Ant 路径风格用 `/`：`**` 跨目录、`*` 不跨目录。只影响统计口径，改了跑一次 `report` 即可 |
| `sourceEncoding` | 否 | 默认 `UTF-8`。源码页乱码时看这里 |
| `dumpRetry` | 否 | 默认 3。传给 `jacococli dump --retry`，服务刚起来端口还没监听时多试几次 |

#### `includes` 怎么定

原则：**只插桩自己写的业务代码**。看被测服务的顶层包名：

```bash
# 从 jar 里看包结构，取最短的公共前缀
unzip -l order-service.jar | grep 'BOOT-INF/classes/com/' | awk '{print $4}' | cut -d/ -f3-5 | sort -u | head
# com/example/order/
```

写成 `com.example.order.*`。多个顶层包就写多条：

```yaml
includes:
  - com.example.order.*
  - com.example.common.*
```

范围开太大的代价不只是启动慢：exec 变大、每轮采集变慢、报告分母被框架类撑大。
范围开太小则漏掉的类**事后补不回来**。宁可稍大，再用 `reportExcludes` 收口径。

#### `excludes` 与 `reportExcludes` 的区别

| | `excludes` | `reportExcludes` |
|---|---|---|
| 作用点 | agent，采集阶段 | hub，出报告阶段 |
| 效果 | 不插桩，**数据根本不产生** | 只从统计里剔除 |
| 改了之后 | 重启服务才生效 | `covhub-client.sh report <svc>` 重出即可 |
| 语法 | 类名，`.` 分隔，`com.example.order.dto.*` | Ant 路径，`/` 分隔，`com/example/order/**/dto/**` |
| 典型用途 | 排除第三方库、生成代码（`*.protobuf.*`） | 排除 dto / mapper / config 这类不想算进覆盖率的 |

拿不准就先只配 `reportExcludes` —— 采集时全都要，统计口径事后再调。

### Step 4 · 被测机器上取 agent 和参数串

**这台机器不需要装 covhub**，只需要 `covhub-client.sh` + curl：

```bash
export COVHUB_URL=http://<hub>:8900
export COVHUB_TOKEN=<serve.token>

sudo mkdir -p /opt/jacoco-lib
covhub-client.sh fetch-agent /opt/jacoco-lib/jacocoagent.jar
# [covhub] 已下载 agent -> /opt/jacoco-lib/jacocoagent.jar

covhub-client.sh agent-opts order-service
# -javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*,classdumpdir=/tmp/covhub-classes/order-service,sessionid=1.4.2
```

参数串里各段的含义：

| 段 | 来自配置 | 说明 |
|---|---|---|
| `-javaagent:<路径>` | `jacocoAgent` | 被测端的 jar 路径。不对就改配置，或**只改这一段** |
| `output=tcpserver` / `tcpclient` | `channel` | 通道 |
| `address` / `port` | pull：`bindAddress` / `port`；push：`collect.advertiseAddress` / `collect.port` | 两个通道里含义相反：pull 是自己监听在哪，push 是连去哪 |
| `includes` / `excludes` | 同名 | 多项用 `:` 拼 |
| `classdumpdir` | `classDumpDir` | 落盘目录 |
| `sessionid` | pull：`version`；push：`name` | push 通道靠它认领连接，**不能手改** |

> **参数串不要手写。** 手写最容易漏掉 `classdumpdir`（报告全红）或改错 `sessionid`
> （push 认领失败）。一律 `agent-opts` 生成，只有 jar 路径那一段允许按被测端实际位置改。

### Step 5 · 注入 agent

按部署方式选一种。**共同点只有一条：把参数串设成目标 JVM 的 `JAVA_TOOL_OPTIONS`。**
JVM 启动时会自动读这个环境变量，`java -jar`、Spring Boot、Tomcat、`run-java.sh`、
K8s 都认，不用关心服务是怎么启动的。

#### A. 裸机 / systemd

用 drop-in 片段，原始 unit 文件不动，撤下时删掉这个文件即可：

```bash
sudo mkdir -p /etc/systemd/system/order-service.service.d
sudo tee /etc/systemd/system/order-service.service.d/covhub.conf <<EOF
[Service]
Environment="JAVA_TOOL_OPTIONS=$(covhub-client.sh agent-opts order-service)"
EOF
sudo systemctl daemon-reload
sudo systemctl restart order-service
```

#### B. Docker

```bash
docker run -d --name order-service \
  -v /opt/jacoco-lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="$(covhub-client.sh agent-opts order-service)" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/order-service:1.4.2
```

三个必须注意的点：

1. agent jar 要**挂进容器**，且 hub 配置里 `jacocoAgent` 要写**容器内路径**（`/opt/jacoco/jacocoagent.jar`）—— agent 是在容器里被加载的
2. `classDumpDir` 同理是容器内路径，取的时候 `docker cp`
3. pull 通道下 `bindAddress` 必须 `0.0.0.0`，且 **6300 要映射出来**；push 通道这两条都不需要

#### C. docker compose

不改原始 compose 文件，加一个 override：

```yaml
# docker-compose.covhub.yml
services:
  order-service:
    environment:
      # 值用 covhub-client.sh agent-opts order-service 生成后贴进来
      JAVA_TOOL_OPTIONS: >-
        -javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*,classdumpdir=/tmp/covhub-classes/order-service,sessionid=1.4.2
    volumes:
      - /opt/jacoco-lib:/opt/jacoco:ro
    ports:
      - "6300:6300"                # push 通道不需要这一行
```

```bash
docker compose -f docker-compose.yml -f docker-compose.covhub.yml up -d order-service
```

#### D. Kubernetes

先给 Deployment 加 initContainer 把 agent jar 拷进共享卷（一次性改造，不必自己维护
带 jar 的镜像）：

```yaml
spec:
  template:
    spec:
      volumes:
        - name: jacoco
          emptyDir: {}
        - name: covclasses               # classdumpdir 落盘用，取完即弃
          emptyDir: {}
      initContainers:
        - name: fetch-agent
          image: curlimages/curl:latest
          command: ["sh","-c","curl -sSf -H 'X-Covhub-Token: $(COVHUB_TOKEN)' -o /shared/jacocoagent.jar http://covhub.internal:8900/api/agent.jar"]
          env:
            - name: COVHUB_TOKEN
              valueFrom: { secretKeyRef: { name: covhub, key: token } }
          volumeMounts:
            - { name: jacoco, mountPath: /shared }
      containers:
        - name: order-service
          volumeMounts:
            - { name: jacoco,     mountPath: /opt/jacoco }
            - { name: covclasses, mountPath: /tmp/covhub-classes }
          ports:
            - { containerPort: 8080 }
```

之后注入环境变量（K8s 下推荐 push 通道，不用暴露 6300、不用 Service 固定地址、多副本天然汇聚）：

```bash
kubectl -n prod set env deployment/order-service \
  JAVA_TOOL_OPTIONS="$(covhub-client.sh agent-opts order-service)"
kubectl -n prod rollout status deployment/order-service
```

坚持用 pull 的话，每个 Pod 是独立采集目标，需要 Service 暴露到固定地址，或干脆固定单副本。

#### E. 直接 `java -jar` / 启动脚本 / Tomcat

```bash
export JAVA_TOOL_OPTIONS="$(covhub-client.sh agent-opts order-service)"
java -jar order-service.jar               # 或 ./start.sh、catalina.sh run，都认这个变量
```

Tomcat 部署 WAR 时也一样，环境变量放在 `setenv.sh` 或调用 `catalina.sh` 的 shell 里
即可，不必动 `CATALINA_OPTS`。

#### 已有 `JAVA_TOOL_OPTIONS` 或其他 `-javaagent` 怎么办

`JAVA_TOOL_OPTIONS` 里可以放多个参数，空格分隔；多个 `-javaagent` 也可以共存
（SkyWalking、Arthas、Pinpoint 之类都常见）：

```bash
export JAVA_TOOL_OPTIONS="-javaagent:/opt/skywalking/skywalking-agent.jar $(covhub-client.sh agent-opts order-service) -Xmx2g"
```

两点注意：

- 参数串里**没有空格**，所以不需要额外引号；但整个变量值要用双引号包住
- 另一个 agent 如果也会改写字节码（APM 类基本都会），把 JaCoCo 的 `-javaagent` 放在**它前面**，
  让 JaCoCo 先插桩原始 class；否则 JaCoCo 看到的是被 APM 改过的 class，指纹会和 classdumpdir
  里落盘的对不上

### Step 6 · 起服务后：确认 agent 挂上了

在被测机器上做三个检查，**每一个都能在 hub 那边看到数据之前定位问题**：

```bash
# 1. JVM 认到了环境变量 —— 启动日志（stderr）第一行会多出这个
docker logs order-service 2>&1 | head -1       # 或 journalctl -u order-service | head
# Picked up JAVA_TOOL_OPTIONS: -javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,...

# 2. pull 通道：agent 端口在监听
ss -lnt | grep 6300                            # 容器里：docker exec order-service sh -c 'cat /proc/net/tcp' 看 189C（=6300 的十六进制）
# LISTEN 0 50 *:6300 *:*

# 3. classDumpDir 有文件了（包名目录 + 带指纹的文件名）
ls /tmp/covhub-classes/order-service/com/example/order | head -3
# OrderApplication.a3f9c2e1b7d04568.class
# OrderApplication$Config.9b1e4d7f2c3a8056.class
```

三个检查都过了才进下一步。任何一个不过，看 §9 对应的条目。

> `Picked up JAVA_TOOL_OPTIONS:` 这一行是 JVM 打到 stderr 的，某些日志采集规则会把它当
> 错误告警 —— 提前跟运维说一声。

### Step 7 · 把 class 送到 hub

出报告要有 class，而且必须是**产生这批 exec 的那一份**。agent 已经把它们落在
`classDumpDir` 里了，打包传给 hub：

```bash
# 裸机 / systemd
tar czf cls.tgz -C /tmp/covhub-classes/order-service .

# Docker
docker cp order-service:/tmp/covhub-classes/order-service ./cls && tar czf cls.tgz -C ./cls .

# K8s（多副本是同一份产物，随便挑一个 Pod）
POD=$(kubectl -n prod get pod -l app=order-service -o name | head -1)
kubectl -n prod cp "${POD#pod/}:/tmp/covhub-classes/order-service" ./cls && tar czf cls.tgz -C ./cls .

covhub-client.sh upload-classes order-service 1.4.2 cls.tgz --retarget
# {"ok": true, "log": "order-service：已接收 1.4.2 的 class 产物 1832 个 -> /opt/coverage-hub/data/order-service/artifacts/1.4.2\norder-service -> version=1.4.2 classfiles=['/opt/coverage-hub/data/order-service/artifacts/1.4.2']", ...}

rm -rf cls cls.tgz
```

`--retarget` 会顺手把 hub 配置里该服务的 `version` 和 `classfiles` 指向这份产物 ——
Step 3 里 `classfiles` 随便填的那个值到这里被自动纠正。两台机器之间不需要 NFS、
不需要 scp 免密。

几个要点：

- **什么时候传**：服务起来、**主要的类都加载过之后**。JaCoCo 是在类加载时落盘，服务刚
  起来只加载了启动路径上的类，冷门业务类要等第一次被调到才落盘。**但这不影响正确性**：
  没落盘的类 exec 里也没有它的数据，报告里只是不出现。发版流水线里在 `wait-online`
  之后传一次即可；想让报告分母更完整，跑一遍冒烟测试再传
- **换了版本必须重新传**（`predeploy` → 部署 → `upload-classes --retarget`，§5）
- **传完就删被测端那份**，别让它一直占磁盘。用 `includes` 收窄范围后，典型服务在几十 MB 量级
- 包里的**顶层目录会自动剥掉**：`tar czf cls.tgz cls/` 和 `tar czf cls.tgz -C cls .` 两种打法都行
- 支持 `tar.gz` 和 `zip`；返回体里 `已接收 ... 0 个` 说明打包方式不对，检查包里是不是真有 `.class`
- **动态生成的类也在里面** —— Spring AOP、MyBatis 代理这类构建产物里根本没有的类，只有 agent 见过

> **仍然想走构建期归档？** 也支持：构建流水线打包 `target/classes` 后同样用
> `upload-classes` 传上来即可（`Jenkinsfile.build` 里有现成的一步）。两者都有时优先
> 用 classdumpdir 那份 —— 它才是运行时真相。

日后推 Sonar 需要某个版本的 class 时，从 hub 取回来即可，本机不必囤：

```bash
covhub-client.sh fetch-classes order-service 1.4.2 ./classes-1.4.2
```

### Step 8 · 可选：让报告能下钻到源码行

不配 `sourcefiles` 时，报告到方法级别为止，类页面显示「Source file ... was not found」。
要看到具体哪一行被执行过，hub 上得有**对应版本**的源码：

```bash
# hub 上
sudo mkdir -p /opt/src && cd /opt/src
git clone <order-service 仓库> order-service
cd order-service && git checkout v1.4.2        # 版本要和线上的一致，否则行号错位
```

配置里指向 `src/main/java` 那一级（多模块项目每个模块一条）：

```yaml
    sourcefiles:
      - /opt/src/order-service/order-api/src/main/java
      - /opt/src/order-service/order-core/src/main/java
```

`retarget` 也能改这一项（`--sourcefiles`），发版流水线里可以在 checkout 新版本源码后
一并更新。源码是 Lombok 生成的 getter/setter 这类，报告里会标在 `@Data` 那一行，属正常。

### Step 9 · 验证接入

在任意一台能访问 hub 的机器上（不需要是 hub 本机）：

```bash
export COVHUB_URL=http://<hub>:8900
export COVHUB_TOKEN=<serve.token>

# 1. 连通性
covhub-client.sh status order-service
# {"ok": true, "services": [{"name": "order-service", "channel": "pull", "online": true, ...}]}
#   pull：online 是端口连得通；push：是当前有实例连着（instances 里能看到每个副本的 peer）

# 2. 拉一次快照
covhub-client.sh dump order-service
# {"ok": true, "log": "order-service：dump\n  指令 1.3%（412/31680）  分支 0.4%  触达类 1103/1832\n..."}
#   触达类不为 0 就说明 exec 和 class 对上了

# 3. 诊断：确认 exec 与 class 对得上
covhub-client.sh diagnose order-service
# {"ok": true, "matchRate": 100.0, "verdict": "正常", ...}

# 4. 看板
#   打开 http://<hub>:8900/?token=<令牌> 应看到 order-service 的卡片
```

刚启动的服务覆盖率通常在 1% 左右、触达类却有六七成 —— 这是正常的：Spring 把 Bean
都实例化了（构造器算覆盖），但业务方法一个没调。**手工点几下页面再 dump，数字应该
明显上涨** —— 涨了就说明整条链路通了。

之后不用再手工 dump：hub 每 `watch.intervalSeconds`（默认 300 秒）自动采一轮，看板
每 60 秒刷新。

---

## 4. 一次完整的实操记录

第一次接入的人照着敲一遍，每步都有预期输出。场景：hub 在 `10.0.0.5`，被测服务
`order-service` 是个 Spring Boot jar，跑在 `10.0.1.21` 的 systemd 下，pull 通道。

**hub 上**（`10.0.0.5`）：

```bash
$ cd /opt/coverage-hub
$ cat >> targets.yaml <<'EOF'
  - name: order-service
    version: "1.4.2"
    address: 10.0.1.21
    port: 6300
    bindAddress: 0.0.0.0
    includes:
      - com.example.order.*
    classDumpDir: /tmp/covhub-classes/order-service
    classfiles:
      - ./data/order-service/artifacts/1.4.2
    reportExcludes:
      - com/example/order/**/dto/**
EOF
$ grep jacocoAgent targets.yaml
jacocoAgent: /opt/jacoco-lib/jacocoagent.jar        # 已按被测端路径改过

$ python3 covhub.py status
服务                   连通     指令%     分支%     版本       最后更新
------------------------------------------------------------------------------
order-service          --       -         -         1.4.2      从未采集
```

`--` 是对的：服务还没挂 agent。

**被测机器上**（`10.0.1.21`）：

```bash
$ export COVHUB_URL=http://10.0.0.5:8900 COVHUB_TOKEN=xxxx
$ sudo mkdir -p /opt/jacoco-lib
$ covhub-client.sh fetch-agent /opt/jacoco-lib/jacocoagent.jar
[covhub] 已下载 agent -> /opt/jacoco-lib/jacocoagent.jar

$ covhub-client.sh agent-opts order-service
-javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*,classdumpdir=/tmp/covhub-classes/order-service,sessionid=1.4.2

$ sudo mkdir -p /etc/systemd/system/order-service.service.d
$ sudo tee /etc/systemd/system/order-service.service.d/covhub.conf <<EOF
[Service]
Environment="JAVA_TOOL_OPTIONS=$(covhub-client.sh agent-opts order-service)"
EOF
$ sudo systemctl daemon-reload && sudo systemctl restart order-service

$ journalctl -u order-service -n 200 | grep 'Picked up'
Picked up JAVA_TOOL_OPTIONS: -javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,...
$ ss -lnt | grep 6300
LISTEN 0      50           *:6300            *:*
$ ls /tmp/covhub-classes/order-service/com/example/order | head -2
OrderApplication.a3f9c2e1b7d04568.class
OrderApplication$Config.9b1e4d7f2c3a8056.class

$ curl -s localhost:8080/api/orders/1 > /dev/null      # 随便调几个接口，让业务类加载

$ tar czf cls.tgz -C /tmp/covhub-classes/order-service .
$ covhub-client.sh upload-classes order-service 1.4.2 cls.tgz --retarget
{"ok": true, "log": "order-service：已接收 1.4.2 的 class 产物 1832 个 -> /opt/coverage-hub/data/order-service/artifacts/1.4.2\norder-service -> version=1.4.2 classfiles=['/opt/coverage-hub/data/order-service/artifacts/1.4.2']"}
$ rm -f cls.tgz
```

**回到 hub**（或任何能访问 hub 的机器）：

```bash
$ python3 covhub.py status
服务                   连通     指令%     分支%     版本       最后更新
------------------------------------------------------------------------------
order-service          ok       -         -         1.4.2      从未采集

$ python3 covhub.py dump order-service
[10:23:41] order-service：dump
[10:23:42]   class 过滤：保留 1790，按 reportExcludes 剔除 42
[10:23:44]   指令 2.1%（665/31680）  分支 0.6%  触达类 1103/1790

$ python3 covhub.py diagnose order-service
服务        order-service
exec        1 个快照 · 1103 个类
            会话 "1.4.2"  启动于 Fri Sep 11 10:15:02 CST 2026
classfiles  /opt/coverage-hub/data/order-service/artifacts/1.4.2
            1832 个类

指纹匹配    1103 / 1103  (100.0%)
判定        正常
```

浏览器打开 `http://10.0.0.5:8900/?token=xxxx`，看到 `order-service` 卡片，点进去是
JaCoCo 原生报告。再去页面上点几个功能，等一个轮询周期（或再 `dump` 一次），覆盖率
应该涨。**到这里接入完成**，接下来把 §5 排进发版流程。

---

## 5. 接发版流水线

### 5.1 顺序

```
1. predeploy       结算旧版本覆盖率              ← 必须在停服之前
2. deploy          停 → 部署 → 起（agent 经 JAVA_TOOL_OPTIONS 注入）
3. wait-online     确认新实例 agent 就绪
4. upload-classes  把新版本的 class 传给 hub 并指过去（--retarget）
5. diagnose        体检：确认这一版的 class 真的对得上
6. sonar           推旧版本的 jacoco.xml（可选）
```

全是发给 hub 的 HTTP 请求，**发版节点只要有 curl**。

- **第 1 步一旦跑到停服之后，那段数据就永久丢失。** 所以 `predeploy` 在目标不可达时
  **故意报错退出**（HTTP 409），让部署流程停下来，而不是静默丢数据。确实要跳过时
  才加 `--allow-missing`
- **第 4 步最容易漏。** class 产物必须跟着版本换，否则新采的 exec 和旧 class 对不上，
  报告全红且不报错。第 5 步就是为了兜住它

### 5.2 裸 shell 版

```bash
#!/bin/sh
set -e
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=...
SVC=order-service
OLD=$1; NEW=$2

# 1. 结算旧版本 —— 必须在停服之前
covhub-client.sh predeploy "$SVC" "$OLD"

# 2. 你自己的部署
deploy.sh "$NEW"

# 3. 等新实例的 agent 起来
covhub-client.sh wait-online "$SVC" 180

# 4. 新版本的 class 传给 hub 并指过去
tar czf cls.tgz -C /tmp/covhub-classes/$SVC .
covhub-client.sh upload-classes "$SVC" "$NEW" cls.tgz --retarget
rm -rf cls.tgz /tmp/covhub-classes/$SVC

# 5. 体检
covhub-client.sh diagnose "$SVC"
```

任何一步非 2xx 都会因 `set -e` 中断。各部署形态（docker / compose / k8s / systemd）
对应的第 2 步写法见 `integration/deployment-snippets.md`。

### 5.3 Jenkins 版

装好 Shared Library 后（`integration/jenkins/README.md`），`Jenkinsfile.deploy` 就是
上面这个顺序的模板，`DEPLOY_MODE` 参数选部署方式，`DIAGNOSE_MIN_MATCH`（默认 90）
控制第 5 步的匹配率阈值 —— 低于它流水线失败。

### 5.4 手工发版 / 没有流水线

最低限度只要记住一件事：**重启前先跑一条 `predeploy`**。

```bash
covhub-client.sh predeploy order-service 1.4.2      # 然后再 systemctl restart
```

漏了也不会静默累加：hub 每轮采集会比对 exec 里的会话启动时刻，发现进程换过就自动
结算上一周期，并在看板和 `diagnose` 的「断代记录」里记一笔。但**重启前最后一个轮询
周期的数据仍然会丢**（上界 = `watch.intervalSeconds`），能主动结算就还是要主动结算。

换了版本记得重传 class（Step 7）；只是同版本重启则不用。

### 5.5 K8s 滚动更新

滚动更新会直接杀掉旧 Pod，`preStop` 钩子里来不及做完整的 dump + 归档。正确做法是在
触发滚动更新**之前**先跑 `predeploy`，即流水线第 1 步 —— 别依赖 Pod 生命周期钩子。

push 通道下滚动发版中途新旧副本同时在线，hub 会检出「混版本」并告警（不自动封存）。
发版流程里第 1 步先把旧版本结算掉，就不会出现这条告警。

---

## 6. 构建期覆盖率（可选，要改 pom）

> 这一节统计的是**单元测试**跑到了哪些代码，和运行期是两条独立的线。如果你们的底线
> 是不改研发的任何东西，整节跳过 —— 运行期覆盖率完全不依赖它。

把 `integration/maven-aggregate-module/pom.xml` 拷进项目，建议放在 `coverage-report/` 目录：

1. 改 `<parent>` 指向项目的父 pom
2. 在 `<dependencies>` 里列出所有要统计的模块
3. 父 pom 的 `<modules>` **末尾**加一行（必须最后，确保其他模块已构建）：

```xml
<modules>
  <module>order-api</module>
  <module>order-core</module>
  <module>coverage-report</module>   <!-- 放最后 -->
</modules>
```

验证：

```bash
mvn clean verify                      # 不能加 -DskipTests，否则没有 exec，聚合报告是空的
ls coverage-report/target/site/jacoco-aggregate/jacoco.xml   # 应存在
```

Sonar 配置：

```properties
sonar.coverage.jacoco.xmlReportPaths=coverage-report/target/site/jacoco-aggregate/jacoco.xml
```

运行期那份建议推到**独立的 project key**（`order-service-runtime`），和单测的并列，
原因与做法见 `integration/sonar/README.md`。

---

## 7. 接入验收清单

- [ ] 被测端启动日志有 `Picked up JAVA_TOOL_OPTIONS:` 且内容与 `agent-opts` 输出一致
- [ ] `covhub-client.sh status <service>` 里 `"online": true`（push：`instances` 数量等于副本数）
- [ ] `covhub-client.sh dump <service>` 能出数字，触达类不为 0
- [ ] 手工操作几个页面后再 dump，覆盖率**有明显上涨**
- [ ] `covhub-client.sh diagnose <service>` 指纹匹配接近 100%、判定为「正常」
- [ ] 看板上能看到该服务卡片；配了 `sourcefiles` 的话点进去能看到绿色行标记
- [ ] class 产物已传到 hub，配置里 `classfiles` 指向 `data/<service>/artifacts/<版本>/`
- [ ] 被测端的 `classDumpDir` 传完已清理
- [ ] 发版流水线里 `predeploy` 排在停服之前，`upload-classes --retarget` 排在起服之后
- [ ] agent 端口没有和同机其他服务撞车
- [ ] agent 端口 / 收集端口**没有暴露到公网**（无认证）
- [ ] hub 配了 `serve.token`，且 8900 也没有暴露到公网
- [ ] 不带令牌访问 `http://<hub>:8900/<service>/state.json` 返回 401
- [ ] 运维知道 stderr 会多一行 `Picked up JAVA_TOOL_OPTIONS`，不会当成告警

---

## 8. 日常运维（Day-2）

### 8.1 日志在哪

| 什么 | 在哪 |
|---|---|
| hub 的采集 / API 日志 | `serve` 进程的 stdout：`nohup` 方式是 `covhub.log`，systemd 方式是 `journalctl -u covhub` |
| 每次 API 调用的执行日志 | 也在返回体的 `log` 字段里，流水线输出中直接能看 |
| agent 自己的错误 | 被测 JVM 的 stderr。agent 启动失败（jar 路径错、class 版本不支持）会在 `Picked up` 那行之后紧跟一段异常 |
| 看板的「需要关注」 | 采集停了（`stale`：超过 3 个轮询周期没新数据）、有断代记录、push 状态未知，卡片会排到最前并标出 |

`/api/health` 和 `/api/openapi.json` 的请求不进日志（它们会被反复轮询）。

### 8.2 磁盘：`data/` 会长多大

每轮采集在 `data/<service>/exec/` 落一个 exec 文件，大小和插桩的类数成正比，典型
服务在几百 KB 到几 MB。默认 300 秒一轮就是每天 288 个；`predeploy` 时整个目录搬进
`versions/<版本>/` 并清空，所以 `exec/` 的大小取决于**两次发版之间隔多久**。

估算：`每轮 exec 大小 × (86400 / intervalSeconds) × 版本周期天数 × 服务数`。
2 MB × 288 × 14 天 × 10 个服务 ≈ 80 GB —— 服务多、发版慢的团队要留意。

两条对策：

- 把 `watch.intervalSeconds` 调大（代价是断代时数据丢失的上界变大）
- 定期把 `versions/` 下老版本的归档搬到对象存储 / 制品库。**归档里的 exec 是不可
  再生的执行轨迹**，搬走可以，别直接删

**别把 `data/` 当缓存删。** 尤其手工测试采的那些 exec，删了重跑也回不来。

### 8.3 备份

要备份的只有两样：配置文件和 `data/`。`data/` 里最值钱的是 `versions/<版本>/`
（报告 + 全部 exec + `manifest.json`）—— manifest 记录了该 exec 对应哪份 class，
是日后重新出报告的唯一依据。`artifacts/` 也一起备，否则 exec 有了 class 没了。

### 8.4 改了配置要不要重启

| 改了什么 | 要做什么 |
|---|---|
| `reportExcludes` / `sourcefiles` / `sourceEncoding` | 不用重启任何东西，`covhub-client.sh report <svc>` 重出报告 |
| `includes` / `excludes` / `classDumpDir` / `bindAddress` / `port` | 重新取 `agent-opts`，**重启被测服务**。改了 `includes` 之后 class 集合变了，记得重传 class |
| `address` / `version` / `classfiles` | 不用重启，下一轮采集生效 |
| `watch.intervalSeconds` / `serve.*` / `collect.*` | **重启 hub** |
| 加 / 删一条 service | 不用重启 hub。删的话先跑一次 `predeploy` 把数据结算掉；`data/<service>/` 不会自动删 |

### 8.5 换令牌

改 `serve.token` → 重启 hub → 更新发版节点的 `COVHUB_TOKEN` 与 Jenkins 凭据 →
浏览器重新用 `?token=` 打开一次看板。K8s 用 initContainer 下载 agent 的话，Secret 也要换。

### 8.6 升级 covhub

```bash
cd /opt/coverage-hub && git pull && sudo systemctl restart covhub
```

配置文件和 `data/` 都不在版本库里，`git pull` 不会碰它们。看一眼 README 顶部的版本号
和 `git log`，有配置项变化时同步改 `targets.yaml`。

### 8.7 升级 JaCoCo

被测服务升到了 agent 不支持的 Java 版本时才需要。换 `lib/` 下的两个 jar（取法见
`CLAUDE.md`），重启 hub；**被测端的 agent jar 也要换**（重新 `fetch-agent` 并重启服务）。
两边版本不一致时 exec 格式通常仍兼容，但别指望，换就一起换。

### 8.8 hub 迁移

整个 `/opt/coverage-hub`（含 `data/`）打包搬过去即可 —— 配置里的相对路径相对配置文件
解析，`upload-classes --retarget` 写进去的 `classfiles` 是绝对路径，搬家后目录不同要
批量改一下。然后改所有发版节点的 `COVHUB_URL`；push 通道还要改 `collect.advertiseAddress`
并重启被测服务（参数串里带着旧地址）。

---

## 9. 常见问题

**任何覆盖率数字不对劲，先跑 `covhub-client.sh diagnose <service>`** —— 指纹匹配率、
会话数、断代记录三样能定位下面绝大多数情况。

### 接入阶段

| 现象 | 怎么查 | 原因 |
|---|---|---|
| 启动日志没有 `Picked up JAVA_TOOL_OPTIONS` | `docker inspect` / `systemctl show -p Environment` 看变量到底有没有传到进程 | 环境变量没设到目标 JVM：compose override 没生效、drop-in 文件名不是 `.conf`、K8s `set env` 打到了别的容器 |
| `Picked up` 后面跟着 `Error opening zip file or JAR manifest missing` | 到被测端看参数串里的 jar 路径存不存在 | `jacocoAgent` 配的路径在被测端不存在，或容器没挂进去 |
| `Picked up` 后面跟着 `Unsupported class file major version` | `java -version` | 被测服务的 Java 比 `lib/` 里的 JaCoCo 新，换 jar（§8.7） |
| 服务起来了但 `ss -lnt` 没有 6300 | 看启动日志有没有 agent 异常；确认 `output=tcpserver` | 参数串是 push 的（`tcpclient`）却按 pull 配；端口被占；agent 没起来 |
| `classDumpDir` 是空的 | 等服务完全起来再看；确认参数串里有 `classdumpdir=` | 手写参数串漏了；目录没写权限；`includes` 写错了什么都没匹配上 |
| `status` 里 `"online": false` / 连通列是 `--` | 从 hub 上 `nc -zv <address> <port>` | 服务没起；`bindAddress` 绑了 `127.0.0.1` 但要跨机访问；容器端口没映射；防火墙；`address` 写成了容器 IP |
| push：日志说「匹配不到任何服务」 | 比对参数串里的 `sessionid` 和配置里的 `name` | 手工改了 `sessionid`。用 `agent-opts` 生成就不会错 |
| push：实例连上了但没数据 | hub 启动日志有没有「没带 --with-watch」告警 | hub 没带 `--with-watch`，收集端起了但没人取数 |
| push：`status` 显示 `?` | 改用 API 或看板 | 你在**另一个进程**里跑的 CLI，看不到收集端手上的连接 —— 那是「不知道」不是「离线」 |
| push：agent 端日志反复重连 | 被测端 `nc -zv <advertiseAddress> 6400` | `advertiseAddress` 配成了 hub 自己的监听地址（`0.0.0.0` / `127.0.0.1`）或容器外不可达的地址 |
| 客户端报 `HTTP 401` | —— | `COVHUB_TOKEN` 没设或和 `serve.token` 对不上 |
| 客户端报「连不上 hub」 | `curl -v $COVHUB_URL/api/health` | `COVHUB_URL` 写错；hub 没起；8900 被防火墙挡了 |
| `fetch-agent` 报 404 | hub 上看 `jacocoAgent` 指的文件在不在 | `jacocoAgent` 配的是被测端路径，hub 本机不存在这个文件。改回 `./lib/jacocoagent.jar` 下载一次，或直接从版本库 `lib/` 拷 |
| `upload-classes` 返回「已接收 0 个」 | `tar tzf cls.tgz \| head` | 包里没有 `.class`：打错了目录，或服务还没加载任何匹配 `includes` 的类 |

### 数据阶段

| 现象 | 怎么查 | 原因 |
|---|---|---|
| dump 成功但覆盖率恒为 0、触达类为 0 | `diagnose` 看 exec 里的类数 | `includes` 写错（用了 `/` 而不是 `.`，或包名拼错），agent 什么都没插桩 |
| 报告满屏全红，触达类为 0 | `diagnose` 匹配率 | `classfiles` 与运行中的版本对不上 —— 最常见的坑。重传 class 并 `--retarget` |
| 匹配率 50% 上下 | `diagnose` 的会话数 | 两个版本的 exec 混在同一周期：发版没跑 `predeploy`，或 push 下滚动发版中途 |
| 部分类始终 0 | 看 `excludes` | 采集阶段就没插桩，改配置并**重启服务** |
| 报告分母比预期大很多 | 看报告的包列表 | `reportExcludes` 没配，dto / mapper / config 都算进去了；或 `includes` 开太大把框架类也算了 |
| 报告里有些类根本不出现 | `ls classDumpDir` 里有没有它 | 传 class 时那个类还没被加载过。调一次相关功能后重传 |
| 源码页显示「Source file ... was not found」 | —— | 没配 `sourcefiles`，或路径没指到 `src/main/java` 那一级 |
| 源码页乱码 | —— | `sourceEncoding` 没设成 `UTF-8` |
| 源码页绿色标记打在错误的行上 | hub 上 `git -C /opt/src/... describe` | 源码版本和线上的不一致 |
| 覆盖率只涨不跌，跨了好几个版本 | `diagnose` 的会话数和断代记录 | 发版时没跑 `predeploy`。hub 会自动检测重启并结算，但重启前最后一个轮询周期的数据丢了 |
| 看板上莫名多出一个版本归档 | `diagnose` 的「断代记录」 | 自动断代：hub 发现被测进程重启过（手工重启、OOM、驱逐），替你结算了上一周期 |
| 归档目录名带 `-2` 后缀 | —— | 同一版本号结算了两次。归档不可覆盖，宁可多一个目录 |
| push：看板报「在线实例跑着两份不同的 class」 | —— | 滚动发版正在进行，新旧副本同时在线。发版流程里补一次 `predeploy` |
| push：某个副本的数据突然不见了 | hub 日志找「已断开（timed out）」 | 取数超时（默认 20 秒）后该实例被丢弃，会重连，但那一段覆盖率随实例消失 |
| `predeploy` 报 409 | —— | 目标已不可达。服务已经停了？那这段数据已经丢了。确实要跳过用 `--allow-missing` |
| `predeploy` 日志「指纹匹配率只有 x%」但仍归档 | `diagnose <svc> <版本>` | 这是有意的：exec 不可再生，对不上也先留下。把 class 对上后可以重出这一版的报告 |

### 运行阶段

| 现象 | 怎么查 | 原因 |
|---|---|---|
| 服务启动明显变慢 | 看 `includes` | 范围太大，把框架类也插桩了。收窄到自己的业务包 |
| 看板卡片提示「采集可能已经停了」 | hub 日志 | 超过 3 个轮询周期没新数据：hub 进程挂了、`--with-watch` 没带、或服务下线了 |
| 浏览器打开看板是一段 401 JSON | —— | 配了 `serve.token` 但地址没带令牌。用 `?token=` 打开一次，之后靠 Cookie |
| 看板本来能开，某天开始要令牌 | —— | hub 加了 `serve.token`。令牌同时管着静态目录 |
| 日志采集把 `Picked up JAVA_TOOL_OPTIONS` 当错误告警 | —— | 那是 JVM 打到 stderr 的正常提示，加个过滤规则 |
| APM agent 与 JaCoCo 同时挂，匹配率异常 | 调整 `-javaagent` 顺序 | JaCoCo 要放在会改字节码的 agent **前面** |

---

## 10. 撤下监控

去掉注入即可，被测项目自始至终没被改过：

| 方式 | 操作 |
|---|---|
| systemd | 删掉 `/etc/systemd/system/<unit>.d/covhub.conf` 后 `daemon-reload` + 重启 |
| Docker | 去掉 `-e JAVA_TOOL_OPTIONS` 重新起容器 |
| compose | 不再传 `-f docker-compose.covhub.yml` |
| k8s | `kubectl set env deployment/X JAVA_TOOL_OPTIONS-`（末尾减号表示删除该变量） |
| 直接 `java -jar` | `unset JAVA_TOOL_OPTIONS` |

撤之前先跑一次 `predeploy` 把最后一段数据结算掉。push 通道的服务撤下时，被测进程
一停连接自然断开，hub 侧无需操作。

被测机器上再删掉 `/opt/jacoco-lib/jacocoagent.jar` 和 `classDumpDir` 指向的目录就
干净了 —— 从头到尾这台机器上就只多过这一个文件。

hub 配置里那条 service 可以留着（看板上会显示离线），也可以删掉；`data/<service>/`
里的归档不会自动删，需要的话手工搬走。

构建期的聚合模块留着无害 —— 它只在 `verify` 阶段多生成一份报告。
