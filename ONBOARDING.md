# 被测项目接入指南

从零把一个 Java 服务接入覆盖率采集。分两部分：**一次性搭建**（整个团队做一次）和**单服务接入**（每个服务各做一遍）。

接入不需要修改被测项目的业务代码。构建期要加一个聚合模块（只为出单测聚合报告），运行期完全靠 JVM 参数注入，零侵入。

**服务端全公司只有一个。** 每接一个服务，被测机器上多出来的东西只有一个
`jacocoagent.jar`（还能从 hub 现下）；发版节点上一行 Python 都不需要装。谁需要装什么，一张表说清：

| 机器 | 需要什么 |
|---|---|
| covhub 那一台（唯一的服务端） | Python 3、java、`covhub.py`、`lib/*.jar`、`targets.json` |
| 被测服务所在机器 | `jacocoagent.jar`（`covhub fetch-agent` 下载），能被 hub 连上 6300 |
| 发版节点 / 流水线 | `curl`（用 `integration/covhub-client.sh` 包一层） |
| **被测项目本身** | **什么都不用改** —— 不改代码、不改 pom、不改构建流水线 |

---

## 第一部分：搭建 coverage-hub（一次性）

### 1. 选一台机器

**只需要一台**，整个团队共用。要求：能连到所有被测服务的 agent 端口，装有 Python 3 和 java。通常放测试环境的一台管理机。

被测服务和发版节点都不在这台机器上跑任何 covhub 进程 —— 它们通过 HTTP 让这台机器干活。

### 2. 部署

```bash
sudo mkdir -p /opt/coverage-hub
sudo chown "$USER" /opt/coverage-hub
cd /opt/coverage-hub

# 放入 covhub.py 与 integration/
# 放入 JaCoCo 发行包里的两个 jar
mkdir -p lib
cp <jacoco 发行包>/lib/jacocoagent.jar lib/
cp <jacoco 发行包>/lib/jacococli.jar   lib/

python3 covhub.py init          # 生成 targets.json 模板
```

打开 `targets.json`，把 `serve.token` 改成一串随机字符串：

```bash
python3 - <<'PY'
import json, secrets, pathlib
p = pathlib.Path("targets.json"); c = json.loads(p.read_text(encoding="utf-8"))
c.setdefault("serve", {})["token"] = secrets.token_urlsafe(24)
p.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
print("serve.token =", c["serve"]["token"])
PY
```

这个令牌是控制 API 的唯一门禁 —— 不配就是**任何能连上 8900 的人都能拉数据、清零计数器**。记下来，发版节点要用。

验证：

```bash
python3 covhub.py status        # 应打印表头，服务列表为空或示例
```

### 3. 起服务端

一个进程包含全部三件事：看板、控制 API、定时采集。

```bash
nohup python3 covhub.py serve --with-watch --port 8900 > covhub.log 2>&1 &
```

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

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now covhub
```

打开 `http://<这台机器>:8900/` 应能看到空看板，`curl http://<这台机器>:8900/api/health` 应返回 `{"ok": true, ...}`。

> 采集轮询和 API 在同一个进程里，写操作会互相排队 —— 这正是要的：结算和轮询不会打架。
> 想拆成两个进程也行（`serve` 不带 `--with-watch`，另起一个 `watch`），但那样就有两个进程在写同一份数据，只在你确定采集耗时会拖慢 API 时才这么做。

### 4. 把客户端脚本发给各团队

```bash
# 发版节点 / 被测机器上，只要这一个脚本 + curl
sudo cp integration/covhub-client.sh /opt/bin/covhub-client.sh
sudo chmod +x /opt/bin/covhub-client.sh
```

用之前设两个环境变量：

```bash
export COVHUB_URL=http://<covhub 机器>:8900
export COVHUB_TOKEN=<上一步生成的 serve.token>
/opt/bin/covhub-client.sh health
```

### 5. 规划 agent 端口

**一台宿主机上的多个服务必须用不同端口。** 提前定好，写进表里避免撞车：

| 服务 | agent 端口 |
|---|---|
| order-service | 6300 |
| user-service | 6301 |
| pay-service | 6302 |

容器里的服务可以都用容器内 6300，映射到宿主机的不同端口即可。

---

## 第二部分：接入一个服务

下面以 `order-service` 为例。

### Step 1 · 确认前提

三个条件，缺一个就接不了：

- [ ] 服务是 JVM 应用，且**能设置环境变量或 JVM 参数**
- [ ] covhub 所在机器能**网络访问**该服务的 agent 端口 —— 连不通也不要紧，改用
      push 通道让被测端连回来（见 Step 5 末尾）
- [ ] 能在被测机器上执行一条命令（打包一个目录并 curl 上传）

**注意第三条不再要求「拿到构建产物」**。v1.1.0 起 class 由 agent 自己落盘（`classDumpDir`），出报告用的就是运行时那一份 —— 指纹必然匹配，不再需要被测项目的构建流水线配合。

只有一种情况仍需构建产物：想在报告里**下钻到源码行**，那还要配 `sourcefiles` 指向对应版本的源码。只看类和方法级别的覆盖数字则不需要。

### Step 2 · 构建期：加聚合模块（只影响单测覆盖率）

> **这一步要改被测项目的 pom。** 如果你们的底线是不改研发的任何东西，直接跳过整个
> Step 2 —— 运行期覆盖率完全不依赖它，方案收敛成纯运行期即可。

只要运行期覆盖率的话，这步可以跳过。

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
mvn clean verify
ls coverage-report/target/site/jacoco-aggregate/jacoco.xml   # 应存在
```

Sonar 配置：

```properties
sonar.coverage.jacoco.xmlReportPaths=coverage-report/target/site/jacoco-aggregate/jacoco.xml
```

> 注意：构建时**不能加 `-DskipTests`**，否则没有 exec 数据，聚合报告是空的。

### Step 3 · 让 agent 自己交出 class（必须）

出报告要有 class，而且必须是**产生这批 exec 的那一份** —— JaCoCo 按类的 CRC64 指纹匹配，对不上报告就全红，而且**不会报错**。

v1.1.0 的做法是让 agent 自己把它加载到的 class 落盘，这样匹配是定义上必然成立的，不需要被测项目做任何事。在 `targets.json` 里给该服务加一项（**被测端路径**）：

```json
"classDumpDir": "/tmp/covhub-classes/order-service"
```

`agent-opts` 会把它拼进参数串。服务起来之后，把这个目录送到 hub：

```bash
# 裸机 / systemd
tar czf cls.tgz -C /tmp/covhub-classes/order-service .

# Docker
docker cp order-service:/tmp/covhub-classes/order-service ./cls && tar czf cls.tgz -C ./cls .

# K8s
kubectl cp order-service-xxxxx:/tmp/covhub-classes/order-service ./cls && tar czf cls.tgz -C ./cls .

covhub-client.sh upload-classes order-service 1.4.3 cls.tgz --retarget
rm -rf cls cls.tgz /tmp/covhub-classes/order-service
```

`--retarget` 会顺手把 hub 配置里的 `classfiles` 指向这份产物。两台机器之间不需要 NFS、不需要 scp 免密。

几个要点：

- **落盘的文件名自带指纹**（`OrderService.3f2a91c4e8b70d15.class`），所以这个目录可以直接当 `classfiles` 用
- **动态生成的类也在里面** —— Spring AOP、MyBatis 代理这类构建产物里根本没有的类，只有 agent 见过
- **传完就删**，别让它一直占被测机的磁盘。用 `includes` 收窄范围后，典型服务在几十 MB 量级
- 服务重启后 agent 会重新落一份，**换了版本记得重新传**

> **仍然想走构建期归档？** 也支持：构建流水线打包 `target/classes` 后同样用
> `upload-classes` 传上来即可（`Jenkinsfile.build` 里有现成的一步）。两者都有时优先
> 用 classdumpdir 那份 —— 它才是运行时真相。

日后推 Sonar 需要某个版本的 class 时，从 hub 取回来即可，本机不必囤：

```bash
covhub-client.sh fetch-classes order-service 1.4.2 ./classes-1.4.2
```

### Step 4 · 在 targets.json 里加一条

```json
{
  "name":        "order-service",
  "version":     "1.4.2",
  "address":     "10.0.1.21",
  "port":        6300,
  "bindAddress": "0.0.0.0",
  "includes":    ["com.example.order.*"],
  "excludes":    [],
  "classDumpDir": "/tmp/covhub-classes/order-service",
  "classfiles":  ["./data/order-service/artifacts/1.4.2"],
  "sourcefiles": ["/opt/src/order-service/src/main/java"],
  "reportExcludes": ["com/example/order/**/dto/**", "com/example/order/*/mapper/**"],
  "sourceEncoding": "UTF-8"
}
```

几个容易配错的地方：

| 字段 | 说明 |
|---|---|
| `address` | covhub **连过去**的地址（被测服务所在主机） |
| `bindAddress` | agent 在**被测端监听**的地址。容器/跨机必须 `0.0.0.0`，同机可用 `127.0.0.1` |
| `includes` | 传给 agent，类名用 `.`，如 `com.example.order.*`。范围开太大会把框架类也插桩，拖慢启动 |
| `classDumpDir` | **被测端**路径，agent 把加载到的 class 落在这里。配了它就不必依赖构建期归档 |
| `channel` | `pull`（默认，hub 去连 agent）或 `push`（agent 连回 hub）。push 服务不需要 `address` / `port` / `bindAddress` |
| `reportExcludes` | 报告端过滤，Ant 路径风格用 `/`。和 `includes` 是两个层次，见下 |
| `sourcefiles` | 可选，配了才能在报告里下钻到源码行 |

`classfiles` / `sourcefiles` 都是 **hub 那台机器上**的路径 —— 报告是 hub 出的。用 `upload-classes --retarget` 传产物的话，这一项会被自动填成 hub 的 `data/<service>/artifacts/<版本>/`，不用手写。

**`excludes` 与 `reportExcludes` 的区别很重要：**

- `excludes` 传给 agent，决定**是否插桩**。被排除的类连数据都不会产生，事后无法找回，改了要重启服务。
- `reportExcludes` 只影响**报告统计口径**，随时可改，`covhub.py report <service>` 重出即可。

拿不准就先只配 `reportExcludes` —— 采集时全都要，统计口径事后再调。

### Step 5 · 注入 agent

先在被测机器上把 agent jar 拿下来，再取参数串。**这台机器不需要装 covhub**：

```bash
export COVHUB_URL=http://<covhub 机器>:8900
export COVHUB_TOKEN=<serve.token>

sudo mkdir -p /opt/jacoco-lib
covhub-client.sh fetch-agent /opt/jacoco-lib/jacocoagent.jar
covhub-client.sh agent-opts  order-service
# -javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*,sessionid=1.4.2
```

参数串里的 jar 路径取自 hub 的 `targets.json` 里的 `jacocoAgent`。被测机器上放在别处的话，把那一项配成**被测端的路径**（容器场景就是容器内路径）。

按部署方式选一种注入。**共同点只有一条：设成目标 JVM 的 `JAVA_TOOL_OPTIONS`。**

#### A. 裸机 / systemd

```bash
sudo mkdir -p /etc/systemd/system/order-service.service.d
sudo tee /etc/systemd/system/order-service.service.d/covhub.conf <<'EOF'
[Service]
Environment="JAVA_TOOL_OPTIONS=-javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*"
EOF
sudo systemctl daemon-reload
sudo systemctl restart order-service
```

用 drop-in 片段，原始 unit 文件不动，撤下时删掉这个文件即可。

#### B. Docker

```bash
docker run -d --name order-service \
  -v /opt/jacoco-lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/order-service:1.4.2
```

三个必须注意的点：

1. agent jar 要**挂进容器**（宿主机那份用 `covhub-client.sh fetch-agent` 下载），且 hub 的 `targets.json` 里 `jacocoAgent` 要写**容器内路径**（`/opt/jacoco/jacocoagent.jar`）—— agent 是在容器里被加载的
2. `bindAddress` 必须 `0.0.0.0`，绑回环地址容器外连不进去
3. **6300 端口要映射出来**，否则 covhub 连不上

#### C. docker compose

不改原始 compose 文件，加一个 override：

```yaml
# docker-compose.covhub.yml
services:
  order-service:
    environment:
      JAVA_TOOL_OPTIONS: "-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*"
    volumes:
      - /opt/jacoco-lib:/opt/jacoco:ro
    ports:
      - "6300:6300"
```

```bash
docker compose -f docker-compose.yml -f docker-compose.covhub.yml up -d order-service
```

#### D. Kubernetes

先给 Deployment 加 initContainer 把 agent jar 拷进共享卷（一次性改造）：

```yaml
spec:
  template:
    spec:
      volumes:
        - name: jacoco
          emptyDir: {}
      initContainers:
        - name: fetch-agent
          image: curlimages/curl:latest        # 不必自己维护带 jar 的镜像
          command: ["sh","-c","curl -sSf -o /shared/jacocoagent.jar http://covhub.internal:8900/api/agent.jar"]
          volumeMounts:
            - { name: jacoco, mountPath: /shared }
      containers:
        - name: order-service
          volumeMounts:
            - { name: jacoco, mountPath: /opt/jacoco }
          ports:
            - { containerPort: 8080 }
            - { containerPort: 6300, name: jacoco }
```

之后注入环境变量：

```bash
kubectl -n prod set env deployment/order-service \
  JAVA_TOOL_OPTIONS="-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*"
kubectl -n prod rollout status deployment/order-service
```

多副本时每个 Pod 是独立采集目标，需要 Service 暴露到固定地址，或干脆固定单副本。

#### E. push 通道：端口连不通，或者多副本

上面 A–D 是**部署形态**，下面这条是**采集方向**，和用哪种部署形态无关。

默认 pull 要求 covhub 能连到被测端的 agent 端口。三种情况下这个前提不成立，改用
push 让被测端主动连回来：

- 被测端不允许开入站端口，或容器网络只出不进
- 服务有多副本，且会自动扩缩（用 pull 得给每个副本在 `targets.json` 里配一条，
  一扩缩就得改配置）
- 跨网段、跨防火墙，只有单向可达

hub 侧加一段全局配置（一次性）：

```json
"collect": {
  "port": 6400,
  "bindAddress": "0.0.0.0",
  "advertiseAddress": "covhub.internal"
}
```

`advertiseAddress` 是**被测端能访问到的 hub 地址**，不是 hub 自己的监听地址 ——
跨网段和容器里最容易在这儿配错。

服务改成 push，并去掉 `address` / `port` / `bindAddress`：

```json
{
  "name": "order-service",
  "channel": "push",
  "includes": ["com.example.order.*"],
  "classDumpDir": "/tmp/covhub-classes/order-service",
  "classfiles": ["./data/order-service/artifacts/current"]
}
```

之后 `agent-opts` 会自动生成 `output=tcpclient`，注入方式和 A–D 完全一样：

```bash
covhub-client.sh agent-opts order-service
# -javaagent:...=output=tcpclient,address=covhub.internal,port=6400,...,sessionid=order-service
```

**多副本不用做任何额外配置** —— 每个副本各连一条，hub 每轮向所有在线实例各取一次，
出报告时一起合并。`status` 会显示在线实例数（`ok(3)`）。

三件事要知道：

- **hub 必须用 `serve --with-watch` 启动。** 连接是长连接、握在收集端手上，
  另起一个 `watch` 进程够不着它们。单独跑 `serve` 会起收集端但不取数，
  启动日志里会告警。
- **`sessionid` 必须是服务名**（`agent-opts` 会自动设好）。收集端靠它认领连接 ——
  手工拼参数串时改了它，hub 会报「匹配不到任何服务」。
- **断代自动检测对 push 不生效**（每个副本有各自的会话）。push 的版本切段靠
  `predeploy`，或 class 指纹变化。

### Step 6 · 验证接入

在任意一台能访问 hub 的机器上（不需要是 hub 本机）：

```bash
export COVHUB_URL=http://<covhub 机器>:8900
export COVHUB_TOKEN=<serve.token>

# 1. 连通性
covhub-client.sh status order-service
#   "online": true

# 2. 拉一次快照
covhub-client.sh dump order-service
#   返回体里应有覆盖率数字，且 classesHit 不为 0

# 3. 诊断：确认 exec 与 class 对得上
covhub-client.sh diagnose order-service
#   指纹匹配应接近 100%，判定为「正常」

# 4. 看板
#   打开 http://<covhub 机器>:8900/ 应看到该服务的卡片
```

刚启动的服务覆盖率通常在 1% 左右、触达类却有六七成 —— 这是正常的：Spring 把 Bean 都实例化了（构造器算覆盖），但业务方法一个没调。**手工点几下页面再 dump，数字应该明显上涨** —— 涨了就说明整条链路通了。

### Step 7 · 接流水线

装好 Shared Library 后（见 `integration/jenkins/README.md`），发版流水线按这个顺序：

```
1. predeploy       结算旧版本覆盖率   ← 必须在停服之前
2. copy            取新版本 class 产物
3. deploy          停 → 部署 → 起（agent 经 JAVA_TOOL_OPTIONS 注入）
4. upload-classes  把新产物传给 hub 并指过去（= retarget）
5. verify          确认新实例 agent 就绪
6. sonar           推旧版本的 jacoco.xml
```

六步全是发给 hub 的 HTTP 请求，**发版节点只要有 curl**。不用 Jenkins 的话，`integration/deployment-snippets.md` 里有等价的裸 shell 版本。

第 1 步**一旦跑到停服之后，那段数据就永久丢失** —— agent 随进程消失，没有任何补救手段。

第 4 步最容易漏：class 产物必须跟着版本换，否则新采的 exec 和旧 class 对不上。

---

## 接入验收清单

- [ ] `covhub-client.sh status <service>` 里 `"online": true`
- [ ] `covhub-client.sh dump <service>` 能出数字，`classesHit` 不为 0
- [ ] 手工操作几个页面后再 dump，覆盖率**有明显上涨**
- [ ] 看板上能看到该服务卡片，点进去能下钻到源码行、看到绿色标记
- [ ] `covhub-client.sh diagnose <service>` 指纹匹配接近 100%、判定为「正常」
- [ ] class 产物已经传到 hub（classdumpdir 那份，或构建期归档那份）
- [ ] 发版流水线里 `predeploy` 排在停服之前
- [ ] agent 端口没有和同机其他服务撞车
- [ ] agent 端口**没有暴露到公网**（无认证，谁都能拉数据和清零）
- [ ] hub 配了 `serve.token`，且 8900 端口也没有暴露到公网

---

## 常见问题

| 现象 | 原因 |
|---|---|
| **任何覆盖率数字不对劲** | **先跑 `covhub-client.sh diagnose <service>`** —— 指纹匹配率、会话数、断代记录三样能定位下面绝大多数情况 |
| push：日志说「匹配不到任何服务」 | agent 的 `sessionid` 和 `targets.json` 里的服务名对不上。用 `agent-opts` 生成参数串就不会错 |
| push：实例连上了但没数据 | hub 没带 `--with-watch`，收集端起了但没人去取数 |
| push：`status` 显示 `?` | 你在**另一个进程**里跑的 CLI，看不到收集端手上的连接 —— 那是「不知道」不是「离线」，看 API 或看板 |
| 客户端报 `HTTP 401` | `COVHUB_TOKEN` 没设或和 hub 的 `serve.token` 对不上 |
| 客户端报"连不上 hub" | `COVHUB_URL` 写错；hub 没起；8900 被防火墙挡了 |
| `status` 里 `"online": false` / 连通列是 `--` | 服务没起；`output` 不是 `tcpserver`；`bindAddress` 绑了回环但要跨机访问；容器端口没映射；防火墙 |
| dump 成功但覆盖率恒为 0 | `includes` 写错（用了 `/` 而不是 `.`，或包名拼错） |
| 报告满屏全红，触达类为 0 | `classfiles` 与运行中的版本对不上 —— 最常见的坑 |
| 部分类始终 0 | 被 `excludes` 排除了（采集阶段就没插桩），需改配置并**重启服务** |
| 报告分母比预期大很多 | `reportExcludes` 没配，dto/mapper/domain 这类都算进去了 |
| 源码页乱码 | `sourceEncoding` 没设成 `UTF-8` |
| 覆盖率数字只涨不跌，跨了好几个版本 | 发版时没跑 `predeploy`。v1.1.0 起 hub 会自动检测进程重启并结算，但重启前最后一个轮询周期的数据仍会丢 —— 能在停服前调 `predeploy` 就还是要调 |
| 看板上莫名多出一个版本归档 | 这是自动断代：hub 发现被测进程重启过，替你结算了上一周期。`diagnose` 的「断代记录」里能看到前后的会话启动时刻 |
| 服务启动明显变慢 | `includes` 范围太大，把框架类也插桩了。收窄到自己的业务包 |

---

## 撤下监控

去掉注入即可，被测项目自始至终没被改过：

| 方式 | 操作 |
|---|---|
| systemd | 删掉 `/etc/systemd/system/<unit>.d/covhub.conf` 后 `daemon-reload` + 重启 |
| Docker | 去掉 `-e JAVA_TOOL_OPTIONS` 重新起容器 |
| compose | 不再传 `-f docker-compose.covhub.yml` |
| k8s | `kubectl set env deployment/X JAVA_TOOL_OPTIONS-`（末尾减号表示删除该变量） |

push 通道的服务撤下时，被测进程一停连接自然断开，hub 侧无需操作。

被测机器上再删掉 `/opt/jacoco-lib/jacocoagent.jar` 和 `classDumpDir` 指向的目录就干净了 —— 从头到尾这台机器上就只多过这一个文件。

构建期的聚合模块留着无害 —— 它只在 `verify` 阶段多生成一份报告。
