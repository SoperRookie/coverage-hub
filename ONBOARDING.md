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
- [ ] covhub 所在机器能**网络访问**该服务的 agent 端口
- [ ] 能拿到该服务**编译产物（class 文件）** —— 且必须是线上跑的那一份

第三条是最容易在事后才发现做不到的。**JaCoCo 按类的 CRC64 指纹匹配数据，class 对不上，采到的数据就等于废的**，报告会显示全部未覆盖。所以先解决 class 产物归档，再谈其他。

### Step 2 · 构建期：加聚合模块（只影响单测覆盖率）

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

### Step 3 · 归档 class 产物（必须）

运行期出报告要用到，必须与线上版本一一对应。在构建流水线里加一步（`Jenkinsfile.build` 已经写好）：

```bash
rm -rf coverage-artifacts && mkdir -p coverage-artifacts
find . -type d -path '*/target/classes' -not -path './coverage-artifacts/*' \
  | while read -r dir; do
      module=$(echo "$dir" | sed 's|^\./||; s|/target/classes$||; s|/|_|g')
      cp -r "$dir" "coverage-artifacts/$module"
    done
tar czf coverage-classes-${VERSION}.tar.gz coverage-artifacts
```

归档后把压缩包传给 hub —— **报告是 hub 出的，class 就必须在 hub 上**：

```bash
covhub-client.sh upload-classes order-service 1.4.2 \
                 coverage-classes-1.4.2.tar.gz --retarget
```

hub 会解到自己的 `data/order-service/artifacts/1.4.2/`，`--retarget` 顺手把配置指过去。两台机器之间不需要 NFS、不需要 scp 免密。

（hub 上本来就有产物的话，也可以跳过上传，直接在 `targets.json` 里把 `classfiles` 写成本地路径。）

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

# 3. 看板
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
- [ ] `classfiles` 指向的 class 与线上运行版本一致（报告不是满屏全红）
- [ ] 构建流水线归档了 class 产物
- [ ] 发版流水线里 `predeploy` 排在停服之前
- [ ] agent 端口没有和同机其他服务撞车
- [ ] agent 端口**没有暴露到公网**（无认证，谁都能拉数据和清零）
- [ ] hub 配了 `serve.token`，且 8900 端口也没有暴露到公网

---

## 常见问题

| 现象 | 原因 |
|---|---|
| 客户端报 `HTTP 401` | `COVHUB_TOKEN` 没设或和 hub 的 `serve.token` 对不上 |
| 客户端报"连不上 hub" | `COVHUB_URL` 写错；hub 没起；8900 被防火墙挡了 |
| `status` 里 `"online": false` / 连通列是 `--` | 服务没起；`output` 不是 `tcpserver`；`bindAddress` 绑了回环但要跨机访问；容器端口没映射；防火墙 |
| dump 成功但覆盖率恒为 0 | `includes` 写错（用了 `/` 而不是 `.`，或包名拼错） |
| 报告满屏全红，触达类为 0 | `classfiles` 与运行中的版本对不上 —— 最常见的坑 |
| 部分类始终 0 | 被 `excludes` 排除了（采集阶段就没插桩），需改配置并**重启服务** |
| 报告分母比预期大很多 | `reportExcludes` 没配，dto/mapper/domain 这类都算进去了 |
| 源码页乱码 | `sourceEncoding` 没设成 `UTF-8` |
| 覆盖率数字只涨不跌，跨了好几个版本 | 发版时没跑 `predeploy`，数据一直累加。运行期覆盖率必须按版本切段 |
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

被测机器上再删掉 `/opt/jacoco-lib/jacocoagent.jar` 就干净了 —— 从头到尾这台机器上就只多过这一个文件。

构建期的聚合模块留着无害 —— 它只在 `verify` 阶段多生成一份报告。
