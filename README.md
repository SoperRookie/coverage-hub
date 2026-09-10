# coverage-hub v1.3.0

通用 JaCoCo 覆盖率方案：**构建期**自动出聚合报告推 SonarQube，**运行期**随服务启动自动采集、发版前自动结算、并提供实时在线看板。

与被测服务无关 —— 任何 Java 服务只要能加 JVM 参数就能接入，不需要改被测项目的代码或 pom。

**整套方案只部署一个服务端。** 被测服务所在的机器、发版节点都不装 Python、不装
java、不放配置文件 —— 它们只需要 `curl`，以及被测 JVM 里挂的那个
`jacocoagent.jar`（还能直接从 hub 下载）。详见 [§ 二·六 单点部署](#二六单点部署与远程-api)。

**也不需要被测项目的构建流水线配合。** v1.1.0 起，出报告用的 class 由 agent 自己
落盘（`classDumpDir`），版本切段由 hub 读 exec 里的会话信息自动判断 —— 不必改被测
项目的 pom，也不必让它的构建流水线归档 class 产物。详见
[§ 一·四 不依赖研发的采集](#4-不依赖研发的采集)。

> **要接入一个新服务，直接看 [ONBOARDING.md](ONBOARDING.md)** —— 从搭建到验收的完整步骤、四种部署方式的注入方法、验收清单和常见问题。本文说明的是设计与命令细节。

---

## 一、运行期覆盖率

### 1. 服务启动时开始采集

给被测服务注入 agent，`output=tcpserver` 模式。**用环境变量注入，不需要改镜像、不需要改启动类**：

```bash
export JAVA_TOOL_OPTIONS="$(python covhub.py agent-opts my-service)"
# 然后照常启动服务
```

`agent-opts` 会根据配置里该服务的这几项生成完整参数串，例如：

```
-javaagent:/opt/coverage-hub/lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*,sessionid=1.4.2
```

`JAVA_TOOL_OPTIONS` 是 JVM 标准环境变量，`java -jar`、Spring Boot、Tomcat、`run-java.sh`、K8s 都认，无需关心服务是怎么启动的。

### 2. 重启 / 发版前自动 dump

```bash
python covhub.py predeploy my-service --version 1.4.2
```

部署脚本跑在别的机器上时，用同名的远程调用 —— 活还是 hub 干的，那台机器只要有 curl：

```bash
COVHUB_URL=http://covhub.internal:8900 \
  integration/covhub-client.sh predeploy my-service 1.4.2
```

它会：dump 并 `--reset` → 生成终版报告 → **体检 exec 与 class 指纹对不对得上** → 连同该周期全部 exec 归档到 `data/<service>/versions/1.4.2/` → 写 manifest 记录对应的 class 产物与体检结论。

体检匹配率低时会大声告警，但**不会阻断结算**：

```
[18:44:59]   !! 指纹匹配率只有 0.0% —— 这一版的报告基本是废的
[18:44:59]      class 产物对不上，报告会几乎全部显示未覆盖……
[18:44:59]      exec 照常归档（不可再生），但重出报告前得先把 class 产物对上
```

不阻断是有意的：走到这一步服务马上要停，exec 是不可再生的 —— 因为指纹对不上就拒绝
归档，只会让这段数据既对不上、又没留下。结论会写进 `manifest.json` 的 `matchRate`，
日后能追。

> **顺序不能反。** 服务一停，agent 随之消失，那段覆盖率数据永久丢失。`predeploy` 必须在停服之前执行 —— 把它放进部署脚本的第一步。

命令在目标不可达时会**报错退出**（非零码），这是有意的：让部署流程停下来，而不是静默丢数据。确实需要跳过时用 `--allow-missing`。

### 3. 实时在线查看

```bash
python covhub.py serve --with-watch    # 看板 + 控制 API + 定时采集，一个进程全包
```

也可以拆成两个进程跑（`serve` 只托管看板与 API，`watch` 单独轮询）：

```bash
python covhub.py watch          # 守护进程，按间隔轮询全部目标
python covhub.py serve          # HTTP 服务：看板 + 控制 API，默认 8900 端口
```

看板首页列出全部服务：在线状态、指令/分支覆盖率、触达类数、趋势曲线、已结算版本列表，可下钻到 JaCoCo 原生报告看到具体哪一行被执行过。页面每 60 秒自动刷新。

单次采集用 `python covhub.py dump my-service`（累加，不清零）。

### 4. 不依赖研发的采集

出报告要两样东西：exec，和**产生这批 exec 的那份 class**。后者过去要靠被测项目的
构建流水线归档，这是整套方案里唯一需要研发配合的地方，也是最容易出错的地方 ——
class 对不上，报告不会报错，只会全红。

v1.1.0 把这两件事都挪到了运行期：

**class 由 agent 自己交出来。** 配置里加一项 `classDumpDir`，agent 会把它实际加载
到的每一个 class 落盘。这份 class 与 exec 的指纹不是「应该匹配」，是定义上必然匹配。

```json
"classDumpDir": "/tmp/covhub-classes/order-service",
"classfiles":   ["/opt/artifacts/order-service/current"]
```

服务起来之后，由部署侧把这个目录送到 hub（一条命令，不碰研发的任何流程）：

```bash
tar czf cls.tgz -C /tmp/covhub-classes/order-service .
covhub-client.sh upload-classes order-service 1.4.3 cls.tgz --retarget
```

> 落盘的文件名形如 `OrderService.3f2a91c4e8b70d15.class`，指纹就在文件名里 ——
> 这个目录可以直接当 `classfiles` 用，covhub 也据此免去一次 JVM 调用。
>
> 另一个附带好处：Spring AOP、MyBatis 代理这类**运行时生成的类**在构建产物里根本
> 不存在，只有 agent 见过。

**版本切段由 hub 自己发现。** 每次采集时读 exec 里的 `SessionInfo`，被测进程的
启动时刻一旦变化，就说明它重启过 —— hub 会自动把上一周期结算归档、开新桶，并在
`state.json` 的 `breaks` 里记一笔：

```
[16:18:54] 检测到断代：会话启动时刻 Sun Sep 06 16:17:50 → Sun Sep 06 16:18:44
[16:18:54]   被测进程重启过，先结算上一周期为版本 1.0.0
[16:18:55]   已归档 → data/order-service/versions/1.0.0
```

它**不能替代** `predeploy`：agent 随进程消失，最后一次成功 dump 到重启之间的数据
还是丢了，丢失上界等于轮询间隔（把 `watch.intervalSeconds` 调到 60 可以把窗口压到
一分钟）。能在停服前调 `predeploy` 就仍然应该调，那是零丢失的。

自动检测的价值在于**堵住 predeploy 覆盖不到的洞**：手工重启、OOM 被杀、K8s 驱逐
—— 这些流水线根本不知道，以前的表现是新旧两个进程的数据混进同一个桶，且不报错。

### 5. 多副本 / 不能开入站端口：push 通道

默认的 `pull` 通道要求 covhub 能连到被测端的 agent 端口。三种场景下这个前提不成立：
被测端不允许开入站端口、容器网络只出不进、多副本还会自动扩缩（用 pull 得给每个副本
配一条，扩缩容一变就得改配置）。

这时把服务配成 `push`，方向反过来 —— agent 主动连回 hub：

```json
{
  "collect": { "port": 6400, "bindAddress": "0.0.0.0",
               "advertiseAddress": "covhub.internal" },
  "services": [
    { "name": "order-service", "channel": "push", "includes": ["com.example.order.*"],
      "classDumpDir": "/tmp/covhub-classes/order-service",
      "classfiles": ["./data/order-service/artifacts/current"] }
  ]
}
```

`advertiseAddress` 是**被测端能访问到的 hub 地址**，不是 hub 自己的监听地址 ——
跨网段、容器里最容易在这儿配错。push 服务不需要 `address` / `port` / `bindAddress`。

`agent-opts` 会相应生成 `output=tcpclient`：

```
-javaagent:...=output=tcpclient,address=covhub.internal,port=6400,includes=com.example.order.*,sessionid=order-service
```

**多副本天然汇聚。** 每个副本各连一条，hub 每轮向所有在线实例各取一次数，落成多个
exec 文件；出报告时一起喂给 `cli report`，等价于隐式合并。副本扩缩不用改任何配置。

`status` 会显示在线实例数：

```
服务                   连通     指令%     分支%     版本       最后更新
order-service          ok(3)    62.4      48.1      1.4.3      2026-09-06T18:14:17
```

三个限制要知道：

- **push 通道要求 `serve --with-watch`。** 连接是长连接、握在收集端手上，另起一个
  `watch` 进程够不着它们。单独跑 `serve` 会起收集端但不取数，启动日志里会警告。
- **收集端口没有认证**，和 agent 端口一样，靠网络策略限制来源。
- **断代自动检测对 push 不生效。** 每个副本有各自的会话，全局比对会互相打架。
  push 服务的版本切段靠 `predeploy`，或 class 指纹变化。

> 协议是自己实现的：`jacococli` 只有 `dump`（去连 tcpserver），没有收集端命令，
> 接 tcpclient 方向反了，官方工具帮不上忙。实现在 `covhub.py` 的
> 「JaCoCo exec 二进制格式与 remote control 协议」一节，格式常量是从真实 exec
> 文件头实测出来的，读写与 JaCoCo 字节级一致。

### 6. 报告全红了怎么查

```bash
covhub-client.sh diagnose order-service        # 或 python covhub.py diagnose order-service
```

它把 exec 里记录的 class 指纹和 `classfiles` 的指纹求交集，直接给结论：

```
exec        18 个快照 · 1832 个类
            会话 "order-service"  启动于 Sun Sep 06 09:12:44 JST 2026
classfiles  /opt/artifacts/order-service/1.4.3
            1795 个类

指纹匹配    219 / 1832  (12.0%)
判定        class 产物对不上，报告会几乎全部显示未覆盖。
            最可能的原因：classfiles 指向的是另一次构建的产物
```

匹配率、会话数、断代记录三样凑一起，接入时最贵的几个坑就都能当场定位，不用靠经验猜。

### 为什么必须按版本切段

JaCoCo 用类的 CRC64 指纹（class id）把 exec 数据和 class 文件对应起来。**发版换了 class，旧 exec 就作废了** —— 拿新 class 去渲染旧 exec，只会得到一份"全部未覆盖"的假报告。

所以运行期覆盖率不能像单元测试那样一直累加，必须以版本为周期：一个版本一个采集周期，发版时 `predeploy` 结算归档，新版本从零开始。归档目录里的 `manifest.json` 记录了该 exec 对应哪份 class 产物，这是日后能重新出报告的唯一依据。

---

## 二、构建期覆盖率（推 SonarQube）

> **这一节要改被测项目的 pom。** 如果你们的底线是不改研发的任何东西，整节跳过即可
> —— 运行期覆盖率（§一）完全不依赖它，方案收敛成纯运行期照样成立。
>
> 它和运行期是两条独立的线：这条统计的是**单元测试**跑到了哪些代码，§一 统计的是
> **线上/测试环境真实流量**跑到了哪些代码。两者都有当然最好，只有后者也完全够用。

多模块 Maven 项目要在打包时直接产出聚合报告，标准做法是加一个聚合模块。见 `integration/maven-aggregate-module/`，把它作为一个模块加进被测项目即可：

```xml
<!-- 父 pom -->
<modules>
  ...
  <module>coverage-report</module>   <!-- 放在最后，确保其他模块已构建 -->
</modules>
```

之后 `mvn verify` 会自动在 `coverage-report/target/site/jacoco-aggregate/jacoco.xml` 产出聚合报告，Sonar 直接读：

```properties
sonar.coverage.jacoco.xmlReportPaths=coverage-report/target/site/jacoco-aggregate/jacoco.xml
```

运行期那份 XML 也能推 Sonar，**建议用独立的 project key**（如 `myapp-runtime`），和单元测试的 project 并列。两者交叉能看出最有价值的两类代码：既无单测、线上也没人跑的（考虑删除），以及线上频繁执行却没有单测保护的（补测试的最高优先级）。

### 构建期归档 class 产物：现在是可选项

v1.1.0 之前，构建流水线**必须**归档 `target/classes`，否则运行期采到的 exec 找不到
配套的 class，归档下来就是废数据。现在 class 由 agent 的 `classDumpDir` 在运行期
自己交出（见 [§一·四](#4-不依赖研发的采集)），这一步降级为可选。

两者都有时优先用 classdumpdir 那份 —— 它是运行时真相，还包含 Spring AOP、MyBatis
代理这类构建产物里根本不存在的动态类。构建期归档的价值只剩一个：如果你还想在报告里
下钻到**源码行**，配 `sourcefiles` 时需要对应版本的源码，而源码通常和构建产物一起管理。

---

## 二·五、Jenkins 接入

见 `integration/jenkins/`：一个 Shared Library（`vars/covhub.groovy`）加两条流水线模板。

- `Jenkinsfile.build` —— 构建期：跑测试 → 聚合报告 → 推 Sonar → **归档 class 产物**
- `Jenkinsfile.deploy` —— 发版：结算旧版本 → 部署 → 指向新产物 → 确认采集恢复 → 推 Sonar

安装步骤、节点前置条件与各步骤的注意事项见 `integration/jenkins/README.md`。

---

## 二·六、单点部署与远程 API

**服务端只有一个。** 覆盖率的采集本来就是 hub 主动发起的：agent 以 `output=tcpserver`
模式监听，hub 连过去把数据拉回来。被测端只有一个 jar，没有任何 Python 进程。

真正需要"在别处执行"的只有发版时那几步（结算、换产物、确认恢复），它们现在都是
HTTP 接口，由 hub 代劳：

| 接口 | 方法 | 用途 |
|---|---|---|
| `/api/health` | GET | 存活探测，不需要令牌 |
| `/api/status[?service=X]` | GET | 连通性与最新覆盖率（JSON） |
| `/api/agent-opts?service=X` | GET | 该服务应注入的 `-javaagent` 参数串（加 `&format=text` 出纯文本） |
| `/api/agent.jar` | GET | 下载 `jacocoagent.jar` |
| `/api/diagnose?service=X[&version=V]` | GET | 诊断 exec 与 class 是否对得上 |
| `/api/dump?service=X` | POST | 拉一次快照（累加） |
| `/api/predeploy?service=X&version=V` | POST | 结算并归档；加 `&allowMissing=1` 允许目标已离线 |
| `/api/report?service=X` | POST | 用已有 exec 重出报告 |
| `/api/retarget?service=X&version=V&classfiles=/a,/b` | POST | 更新版本与 class 产物路径 |
| `/api/upload-classes?service=X&version=V[&retarget=1]` | POST | 上传 class 产物压缩包（tar.gz / zip，正文为二进制） |
| `/api/classes?service=X&version=V` | GET | 把该版本的 class 产物打成 tar.gz 回传 |

写操作在 hub 内部串行执行，返回体里带着这次执行的日志；**HTTP 非 2xx 表示失败**，
调用方应当据此让部署流程停下来。

发版节点上用 `integration/covhub-client.sh` 包一层，只依赖 curl：

```bash
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=<hub 上配的 serve.token>

covhub-client.sh predeploy      order-service 1.4.2      # 1. 结算，必须在停服之前
deploy.sh 1.4.3                                          # 2. 你自己的部署
covhub-client.sh upload-classes order-service 1.4.3 \
                 coverage-classes-1.4.3.tar.gz --retarget # 3. 产物传给 hub 并指过去
covhub-client.sh wait-online    order-service            # 4. 确认新实例采集恢复
```

Jenkins 的 Shared Library 同样默认走远程模式，只要设了 `COVHUB_URL`（见
`integration/jenkins/README.md`）。

### class 产物为什么要传给 hub

报告是 hub 出的，那 class 就必须在 hub 上，而且必须是线上跑的那一份（JaCoCo 按
CRC64 class id 匹配）。`upload-classes` 把构建期归档的压缩包直接 POST 过来，解到
`data/<service>/artifacts/<版本>/`，两台机器之间不需要 NFS 或共享目录。包里如果只有
一个顶层目录（构建脚本打的 `coverage-artifacts/`），会自动剥掉。

反过来，`/api/classes` 把某个版本的产物打包回传 —— 推 Sonar 时
`-Dsonar.java.binaries` 要的正是**采集时运行的那份 class**：

```bash
covhub-client.sh fetch-classes order-service 1.4.2 ./classes-1.4.2
```

hub 按两个来源找：先看 `artifacts/<版本>/`（`upload-classes` 传上来的），没有就退
回 `versions/<版本>/manifest.json` 里记的 `classfiles` 路径（结算时实际用来出报告
的那几个目录）。两个都没有就返回 404 —— 那说明该版本发版时没走 `upload-classes`，
产物已经找不回来了。

**发版节点因此不必囤任何历史产物**：新产物传上去，旧产物要用时取回来。

### 访问控制

配了 `serve.token`（或给 hub 进程设了环境变量 `COVHUB_TOKEN`）之后，除
`/api/health` 外所有接口都要带 `X-Covhub-Token` 请求头或 `?token=`。**不配就是谁都
能调**，包括 `predeploy` 那个会清零计数器的动作 —— 共享环境务必配上。

看板本身（静态报告）不校验令牌，它和 agent 端口一样，应当靠网络策略限制来源。
push 通道的收集端口（`collect.port`）同样没有认证 —— 任何能连上它的进程都能往里
报数据，务必用防火墙或安全组限定来源网段。

---

## 三、配置

配置文件可以是 **YAML 或 JSON**，按扩展名自动分派；`-c` 不给时按
`targets.yaml` → `targets.yml` → `targets.json` 的顺序探测。多服务场景推荐 YAML ——
能写注释、不用数逗号引号。可从 `targets.example.yaml` 复制，或用 `covhub.py init`
生成（加 `--json` 生成 JSON 模板）。

> YAML 需要 `pip install PyYAML`，这是本工具唯一的第三方依赖。装不上第三方包的
> 机器（离线内网、老镜像）继续用 JSON 即可，两种格式功能完全等价。

它含各环境地址与路径，每台机器不同，已被 `.gitignore` 排除 —— 版本库里维护的是
`targets.example.yaml` 和等价的 `targets.example.json`。

同样被排除的还有 `lib/*.jar`（由 JaCoCo 发行包提供，按需放入）和 `data/`（采集产物）。

> `data/<service>/versions/` 下的 exec 是**不可再生**的真实执行轨迹。需要长期留存的话请归档到对象存储或制品库，别指望 git。

```yaml
jacocoAgent: ./lib/jacocoagent.jar
jacocoCli: ./lib/jacococli.jar
dataDir: ./data

serve:
  port: 8900
  token: 改成一串随机字符串
watch:
  intervalSeconds: 300
collect:
  port: 6400
  bindAddress: 0.0.0.0
  advertiseAddress: covhub.internal

services:
  - name: my-service
    version: "1.4.2"          # 版本号一律加引号，裸写的 1.4 会被读成数字
    channel: pull
    address: 127.0.0.1
    port: 6300
    bindAddress: 0.0.0.0
    includes:
      - com.example.*
    excludes: []
    classDumpDir: /tmp/covhub-classes/my-service
    classfiles:
      - /opt/artifacts/my-service/1.4.2/classes
    sourcefiles:
      - /opt/src/my-service/src/main/java
    reportExcludes:
      - com/example/**/dto/**
      - com/example/*/mapper/**
    sourceEncoding: UTF-8

  - name: another-service     # 多服务就在这里往下加，各服务之间互不影响
    version: "2.0.1"
    address: 10.0.1.22
    port: 6300
    includes:
      - com.example.another.*
    classDumpDir: /tmp/covhub-classes/another-service
    classfiles:
      - /opt/artifacts/another-service/2.0.1/classes
```

| 字段 | 说明 |
|---|---|
| `address` / `port` | covhub 连过去拉数据的地址；`bindAddress` 是 agent 在被测端监听的地址 |
| `includes` / `excludes` | **传给 agent 的**，类名用 `.` 分隔，多项用 `:`（工具会自动拼） |
| `classDumpDir` | 让 agent 把实际加载的 class 落到这个目录（**被测端路径**）。配了它就不必再依赖构建期归档 |
| `classfiles` | 出报告用的 class，**必须与运行中的服务是同一份产物**。用 `upload-classes` 传上来的话这项会自动指过去 |
| `reportExcludes` | **报告端过滤**，Ant 风格路径模式（用 `/`）。CLI 的 `report` 不支持排除，工具会先过滤出一份 class 副本再出报告 |
| `sourcefiles` | 可选。配了才能在报告里下钻到源码行 |
| `dumpRetry` | 可选，默认 3。传给 `jacococli dump --retry`，服务刚起来时端口可能还没监听 |
| `watch.intervalSeconds` | 轮询间隔，默认 300。它同时是**断代时数据丢失的上界** —— 调小可以压缩这个窗口 |
| `collect.port` / `bindAddress` | push 通道的收集端口。**只有配了 `collect.port`，`serve` 才会起收集端** |
| `channel` | `pull`（默认，hub 去连 agent）或 `push`（agent 连回 hub，见 §一·五） |
| `collect.advertiseAddress` | push 通道用：**被测端连回 hub 的地址**，不是 hub 的监听地址 |
| `serve.token` | 控制 API 的访问令牌。不配则任何能连上 8900 的人都能调写接口 |

`includes`/`excludes` 与 `reportExcludes` 是两个层次：前者决定**是否插桩**（被排除的类连数据都不会产生，事后无法找回），后者只影响**报告统计口径**（随时可调，重出报告即可）。

相对路径一律相对配置文件所在目录解析，整个目录可以直接搬到别的机器上。

`retarget`（发版后改 `version` / `classfiles`）会回写配置文件。YAML 配置只替换目标
服务的那几行，**注释和排版原样保留**；JSON 配置则整体重写。

---

## 四、命令一览

| 命令 | 用途 |
|---|---|
| `init [--json]` | 生成配置模板，默认 `targets.yaml`，`--json` 生成 `targets.json` |
| `agent-opts <service>` | 打印启动时应注入的 `-javaagent` 参数串 |
| `status [service]` | 目标连通性与最新覆盖率 |
| `dump <service>` | 拉一次快照并出报告（累加，不清零） |
| `predeploy <service> [--version V]` | 发版/重启前结算：`dump --reset` + 归档 |
| `report <service>` | 用已有 exec 重新出报告（改了 `reportExcludes` 后用） |
| `diagnose <service> [--version V]` | 诊断 exec 与 class 产物是否对得上 |
| `retarget <service> --version V [--classfiles ...]` | 发版后把配置指向新版本产物 |
| `watch [--interval N]` | 守护进程，定时轮询全部目标 |
| `serve [--port N] [--with-watch]` | HTTP 服务：看板 + 控制 API，`--with-watch` 顺带在同进程里采集 |

只依赖 Python 3 标准库和 `java`。唯一的第三方包是 YAML 配置要用的 `PyYAML` ——
用 JSON 配置则完全零依赖。

---

## 五、目录布局

```
data/
  index.html                    看板首页
  <service>/
    current/                    当前版本周期的最新报告
      html/index.html           JaCoCo 原生报告，可下钻源码
      jacoco.xml                推 SonarQube 用
      jacoco.csv
    exec/<时间戳>.exec           本周期历次快照
    versions/<版本>/             周期结算归档（报告 + exec + merged.exec + manifest）
    artifacts/<版本>/            经 upload-classes 传上来的 class 产物
    classes/                    按 reportExcludes 过滤后的 class 副本
    state.json                  历史统计、会话基线（sessionStart）、断代记录（breaks）
```

---

## 六、注意事项

- **控制 API 的写接口能清零计数器**（`predeploy` 带 `--reset`）。配上 `serve.token`，
  并且不要把 8900 暴露到公网。
- **tcpserver 端口没有认证。** 任何能连上的人都能拉数据、并通过 `--reset` 清空计数器。生产/共享环境务必用防火墙或安全组限制来源，不要暴露到公网。
- **性能开销通常在个位数百分比**，可用于测试环境常驻，但不建议长期挂在生产上。
- **归档不会被覆盖。** 同名版本已有归档时会自动存成 `<版本>-2` —— 归档里的 exec 是不可再生的执行轨迹，宁可多一个目录也不能覆盖掉。
- **exec 是不可再生资产**，尤其是手工测试采集的数据。归档时务必连同对应的 class 产物一起保存，否则日后无法重新出报告。
- **覆盖率不是质量指标。** 它只说明代码被执行过，不说明断言是否有效。分支覆盖率通常比指令覆盖率更有参考价值。
