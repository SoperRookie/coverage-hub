# 各部署形态的接入片段

核心只有一件事：把 `covhub.py agent-opts <service>` 生成的参数串塞进目标 JVM 的
`JAVA_TOOL_OPTIONS`，并让 covhub 能连到 agent 的端口。

## Docker（不改镜像）

```bash
AGENT_OPTS=$(python covhub.py agent-opts my-service)

docker run -d --name my-service \
  -v /opt/coverage-hub/lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="$AGENT_OPTS" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/my-service:1.4.2
```

注意三点：agent jar 要挂进容器（路径需与 `agent-opts` 输出一致，可在
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
      - /opt/coverage-hub/lib:/opt/jacoco:ro
    ports:
      - "8080:8080"
      - "6300:6300"
```

## Kubernetes

agent jar 用 initContainer 拷进共享 volume，避免改业务镜像：

```yaml
spec:
  volumes:
    - name: jacoco
      emptyDir: {}
  initContainers:
    - name: fetch-agent
      image: myrepo/coverage-hub:latest      # 镜像里带 jacocoagent.jar
      command: ["sh","-c","cp /opt/jacoco/jacocoagent.jar /shared/"]
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
`covhub predeploy <service> --version <旧版本>`。

## systemd（裸机 / 虚拟机）

```ini
[Service]
Environment="JAVA_TOOL_OPTIONS=-javaagent:/opt/coverage-hub/lib/jacocoagent.jar=output=tcpserver,address=127.0.0.1,port=6300,includes=com.example.*"
ExecStart=/usr/bin/java -jar /opt/my-service/app.jar
# 停服前先结算，超时保护避免卡住重启
ExecStop=/usr/bin/timeout 60 /usr/bin/python3 /opt/coverage-hub/covhub.py \
         -c /opt/coverage-hub/targets.json predeploy my-service
```

## 部署脚本里的正确顺序

```bash
set -e

# 1. 结算旧版本 —— 必须在停服之前
python covhub.py predeploy my-service --version "$OLD_VERSION"

# 2. 停服、部署、启服
deploy.sh "$NEW_VERSION"

# 3. 更新 targets.json 里的 version 与 classfiles，指向新版本产物
python - <<PY
import json, pathlib
p = pathlib.Path("targets.json"); c = json.loads(p.read_text(encoding="utf-8"))
for s in c["services"]:
    if s["name"] == "my-service":
        s["version"] = "$NEW_VERSION"
        s["classfiles"] = ["/opt/artifacts/my-service/$NEW_VERSION/classes"]
p.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
PY

# 4. 确认新实例的 agent 已就绪
python covhub.py status my-service
```

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
  -Dsonar.coverage.jacoco.xmlReportPaths=/opt/coverage-hub/data/my-service/versions/1.4.2/jacoco.xml
```
