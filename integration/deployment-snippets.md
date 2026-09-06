# 各部署形态的接入片段

核心只有一件事：把 `agent-opts` 生成的参数串塞进目标 JVM 的 `JAVA_TOOL_OPTIONS`，
并让 covhub 能连到 agent 的端口。

**这些机器上不需要装 covhub。** 整套方案只有一个服务端，下面所有片段里的 covhub
操作都是发给它的 HTTP 请求 —— 本机只要有 curl：

```bash
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=<hub 上配的 serve.token>
alias covhub='/opt/bin/covhub-client.sh'      # integration/covhub-client.sh，拷一份即可
```

## Docker（不改镜像）

```bash
# 参数串和 agent jar 都从 hub 取，本机不必预先铺任何东西
mkdir -p /opt/jacoco-lib
covhub fetch-agent /opt/jacoco-lib/jacocoagent.jar
AGENT_OPTS=$(covhub agent-opts my-service)

docker run -d --name my-service \
  -v /opt/jacoco-lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="$AGENT_OPTS" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/my-service:1.4.2
```

注意三点：agent jar 要挂进容器（路径需与 `agent-opts` 输出一致，可在 hub 的
`targets.json` 里把 `jacocoAgent` 配成容器内路径）；`bindAddress` 必须是
`0.0.0.0` 否则宿主机连不进去；6300 端口要映射出来。

## docker compose

```yaml
services:
  my-service:
    image: myrepo/my-service:1.4.2
    environment:
      JAVA_TOOL_OPTIONS: >-
        -javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*
    volumes:
      - /opt/jacoco-lib:/opt/jacoco:ro      # 先 covhub fetch-agent 下载到这里
    ports:
      - "8080:8080"
      - "6300:6300"
```

## Kubernetes

agent jar 用 initContainer 从 hub 下载到共享 volume，避免改业务镜像，
也不需要自己维护一个带 jar 的镜像：

```yaml
spec:
  volumes:
    - name: jacoco
      emptyDir: {}
  initContainers:
    - name: fetch-agent
      image: curlimages/curl:latest
      command: ["sh","-c","curl -sSf -o /shared/jacocoagent.jar http://covhub.internal:8900/api/agent.jar"]
      volumeMounts:
        - { name: jacoco, mountPath: /shared }
  containers:
    - name: app
      image: myrepo/my-service:1.4.2
      env:
        - name: JAVA_TOOL_OPTIONS
          value: "-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*"
      ports:
        - { containerPort: 8080 }
        - { containerPort: 6300, name: jacoco }
      volumeMounts:
        - { name: jacoco, mountPath: /opt/jacoco }
      lifecycle:
        preStop:
          exec:
            # 优雅停机前把数据推走。K8s 默认给 30s，够 dump 一次
            command: ["sh","-c","sleep 2"]
```

Pod 多副本时每个副本是独立的采集目标。可以给每个副本在 `targets.json` 里配一条，
或用 Service + 固定副本数；更省事的做法是让副本用 `output=tcpclient` 主动上报到
一个中心收集端。

**K8s 下发版尤其要注意**：滚动更新会直接杀掉旧 Pod，`preStop` 里来不及做完整的
dump + 归档。正确做法是在触发滚动更新**之前**，先在流水线里跑
`covhub predeploy <service> <旧版本>`（一条 curl，不需要在流水线节点上装 covhub）。

## systemd（裸机 / 虚拟机）

```ini
[Service]
Environment="JAVA_TOOL_OPTIONS=-javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*"
Environment="COVHUB_URL=http://covhub.internal:8900"
ExecStart=/usr/bin/java -jar /opt/my-service/app.jar
# 停服前先结算。这里只是一条 curl —— 本机不需要 Python、java 和 targets.json。
# 超时保护避免 hub 无响应时卡住重启。
ExecStop=/usr/bin/timeout 60 /opt/bin/covhub-client.sh predeploy my-service
```

`ExecStop` 只在 systemd 自己停服务时触发（`systemctl stop/restart`）。进程被
`kill -9`、机器掉电时不会执行 —— 那种情况下这段覆盖率就是丢了，没有补救手段。
真正可靠的做法还是把结算放进部署脚本的第一步。

## 部署脚本里的正确顺序

```bash
set -e
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=...

# 1. 结算旧版本 —— 必须在停服之前
covhub predeploy my-service "$OLD_VERSION"

# 2. 停服、部署、启服
deploy.sh "$NEW_VERSION"

# 3. 把新版本的 class 产物传给 hub，并让配置指向它
covhub upload-classes my-service "$NEW_VERSION" \
       "coverage-classes-$NEW_VERSION.tar.gz" --retarget

# 4. 确认新实例的 agent 已就绪
covhub wait-online my-service
```

整个脚本没有一行 Python —— 四步都是发给 hub 的 HTTP 请求，任何一步非 2xx 都会因
`set -e` 中断部署。

第 3 步是最容易被漏掉的：**class 产物必须跟着版本一起换**，否则新版本的 exec
会和旧 class 对不上，报告全是"未覆盖"。

## 把运行期报告推 SonarQube

用独立的 project key，与单元测试的 project 并列：

```bash
sonar-scanner \
  -Dsonar.projectKey=my-service-runtime \
  -Dsonar.projectName="my-service (runtime coverage)" \
  -Dsonar.sources=/opt/src/my-service/src/main/java \
  -Dsonar.java.binaries=/opt/artifacts/my-service/1.4.2/classes \
  -Dsonar.coverage.jacoco.xmlReportPaths=jacoco-runtime.xml
```

报告在 hub 上，先取回来（看板本身就是静态文件服务，按路径直接下）：

```bash
curl -sSf -o jacoco-runtime.xml \
  "$COVHUB_URL/my-service/versions/1.4.2/jacoco.xml"
```
