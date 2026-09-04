# 被测项目接入指南

从零把一个 Java 服务接入覆盖率采集。分两部分：**一次性搭建**（整个团队做一次）和**单服务接入**（每个服务各做一遍）。

接入不需要修改被测项目的业务代码。构建期要加一个聚合模块（只为出单测聚合报告），运行期完全靠 JVM 参数注入，零侵入。

---

## 第一部分：搭建 coverage-hub（一次性）

### 1. 选一台机器

要求：能连到所有被测服务的 agent 端口，装有 Python 3 和 java。通常就放在跑 Jenkins agent 的机器，或者测试环境的一台管理机。

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

验证：

```bash
python3 covhub.py status        # 应打印表头，服务列表为空或示例
```

### 3. 起看板

```bash
nohup python3 covhub.py serve --port 8900 > serve.log 2>&1 &
nohup python3 covhub.py watch  > watch.log 2>&1 &
```

生产化建议做成两个 systemd unit：

```ini
# /etc/systemd/system/covhub-serve.service
[Unit]
Description=covhub dashboard
After=network.target

[Service]
WorkingDirectory=/opt/coverage-hub
ExecStart=/usr/bin/python3 /opt/coverage-hub/covhub.py serve --port 8900
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/covhub-watch.service
[Unit]
Description=covhub collector
After=network.target

[Service]
WorkingDirectory=/opt/coverage-hub
ExecStart=/usr/bin/python3 /opt/coverage-hub/covhub.py watch
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now covhub-serve covhub-watch
```

打开 `http://<这台机器>:8900/` 应能看到空看板。

### 4. 规划 agent 端口

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

归档后按版本投放到 covhub 机器上：

```
/opt/artifacts/order-service/1.4.2/
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
  "classfiles":  ["/opt/artifacts/order-service/1.4.2"],
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

**`excludes` 与 `reportExcludes` 的区别很重要：**

- `excludes` 传给 agent，决定**是否插桩**。被排除的类连数据都不会产生，事后无法找回，改了要重启服务。
- `reportExcludes` 只影响**报告统计口径**，随时可改，`covhub.py report <service>` 重出即可。

拿不准就先只配 `reportExcludes` —— 采集时全都要，统计口径事后再调。

### Step 5 · 注入 agent

先取参数串：

```bash
cd /opt/coverage-hub
python3 covhub.py agent-opts order-service
# -javaagent:/opt/coverage-hub/lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*,sessionid=1.4.2
```

按部署方式选一种注入。**共同点只有一条：设成目标 JVM 的 `JAVA_TOOL_OPTIONS`。**

#### A. 裸机 / systemd

```bash
sudo mkdir -p /etc/systemd/system/order-service.service.d
sudo tee /etc/systemd/system/order-service.service.d/covhub.conf <<'EOF'
[Service]
Environment="JAVA_TOOL_OPTIONS=-javaagent:/opt/coverage-hub/lib/jacocoagent.jar=output=tcpserver,address=127.0.0.1,port=6300,includes=com.example.order.*"
EOF
sudo systemctl daemon-reload
sudo systemctl restart order-service
```

用 drop-in 片段，原始 unit 文件不动，撤下时删掉这个文件即可。

#### B. Docker

```bash
docker run -d --name order-service \
  -v /opt/coverage-hub/lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.order.*" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/order-service:1.4.2
```

三个必须注意的点：

1. agent jar 要**挂进容器**，且 `targets.json` 里的 `jacocoAgent` 要写**容器内路径**（`/opt/jacoco/jacocoagent.jar`）—— agent 是在容器里被加载的
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
      - /opt/coverage-hub/lib:/opt/jacoco:ro
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
          image: myrepo/coverage-hub:latest
          command: ["sh","-c","cp /opt/jacoco/jacocoagent.jar /shared/"]
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

```bash
cd /opt/coverage-hub

# 1. 连通性
python3 covhub.py status order-service
#   「连通」列应为 ok

# 2. 拉一次快照
python3 covhub.py dump order-service
#   应打印覆盖率数字，且「触达类」不为 0

# 3. 看板
#   打开 http://<covhub 机器>:8900/ 应看到该服务的卡片
```

刚启动的服务覆盖率通常在 1% 左右、触达类却有六七成 —— 这是正常的：Spring 把 Bean 都实例化了（构造器算覆盖），但业务方法一个没调。**手工点几下页面再 dump，数字应该明显上涨** —— 涨了就说明整条链路通了。

### Step 7 · 接流水线

装好 Shared Library 后（见 `integration/jenkins/README.md`），发版流水线按这个顺序：

```
1. predeploy   结算旧版本覆盖率   ← 必须在停服之前
2. copy        取新版本 class 产物
3. deploy      停 → 部署 → 起（agent 经 JAVA_TOOL_OPTIONS 注入）
4. retarget    更新 targets.json 的 version 与 classfiles
5. verify      确认新实例 agent 就绪
6. sonar       推旧版本的 jacoco.xml
```

第 1 步**一旦跑到停服之后，那段数据就永久丢失** —— agent 随进程消失，没有任何补救手段。

第 4 步最容易漏：class 产物必须跟着版本换，否则新采的 exec 和旧 class 对不上。

---

## 接入验收清单

- [ ] `covhub.py status <service>` 连通列为 `ok`
- [ ] `covhub.py dump <service>` 能出数字，触达类不为 0
- [ ] 手工操作几个页面后再 dump，覆盖率**有明显上涨**
- [ ] 看板上能看到该服务卡片，点进去能下钻到源码行、看到绿色标记
- [ ] `classfiles` 指向的 class 与线上运行版本一致（报告不是满屏全红）
- [ ] 构建流水线归档了 class 产物
- [ ] 发版流水线里 `predeploy` 排在停服之前
- [ ] agent 端口没有和同机其他服务撞车
- [ ] agent 端口**没有暴露到公网**（无认证，谁都能拉数据和清零）

---

## 常见问题

| 现象 | 原因 |
|---|---|
| `status` 连通列是 `--` | 服务没起；`output` 不是 `tcpserver`；`bindAddress` 绑了回环但要跨机访问；容器端口没映射；防火墙 |
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

构建期的聚合模块留着无害 —— 它只在 `verify` 阶段多生成一份报告。
