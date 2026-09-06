# 各部署形态的接入片段

接入一个服务只有两件事：

1. **把 agent 参数串塞进目标 JVM 的 `JAVA_TOOL_OPTIONS`**
2. **让 hub 拿到这一版的 class 产物** —— 由 agent 的 `classDumpDir` 在运行期落盘，
   起服务后传一次给 hub

**这些机器上不需要装 covhub。** 整套方案只有一个服务端，下面所有片段里的 covhub
操作都是发给它的 HTTP 请求 —— 本机只要有 curl：

```bash
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=<hub 上配的 serve.token>
alias covhub='/opt/bin/covhub-client.sh'      # integration/covhub-client.sh，拷一份即可
```

> **参数串不要手写。** 一律用 `covhub agent-opts <服务名>` 生成 —— 它会按 hub 上的
> 配置带上 `includes`、`classdumpdir`、`sessionid`，以及正确的通道（`tcpserver`
> 还是 `tcpclient`）。手写最容易漏掉 `classdumpdir`，那会让报告全红。

## 先选通道

| | 什么时候用 | agent 输出 | 端口 |
|---|---|---|---|
| **pull**（默认） | hub 能连到被测端 | `output=tcpserver` | 被测端开 6300，且要能被 hub 访问 |
| **push** | 不能开入站端口 / 容器只出不进 / 多副本自动扩缩 | `output=tcpclient` | 被测端不开端口，连 hub 的 6400 |

通道在 hub 的 `targets.json` 里配（`"channel": "push"`），被测端的注入方式两者完全一样。
下面以 pull 为例；改 push 只需去掉端口映射那几行，参数串由 `agent-opts` 自动切换。

## Docker（不改镜像）

```bash
# agent jar 从 hub 取，本机不必预先铺
mkdir -p /opt/jacoco-lib
covhub fetch-agent /opt/jacoco-lib/jacocoagent.jar
AGENT_OPTS=$(covhub agent-opts my-service)

docker run -d --name my-service \
  -v /opt/jacoco-lib:/opt/jacoco:ro \
  -e JAVA_TOOL_OPTIONS="$AGENT_OPTS" \
  -p 8080:8080 \
  -p 6300:6300 \
  myrepo/my-service:1.4.2

# 起来之后把 agent 落盘的 class 传给 hub（路径取自 targets.json 的 classDumpDir）
docker cp my-service:/tmp/covhub-classes/my-service ./cls
tar czf cls.tgz -C ./cls . && covhub upload-classes my-service 1.4.2 cls.tgz --retarget
rm -rf ./cls cls.tgz
```

三个容易错的点：

1. agent jar 要**挂进容器**，且 hub 的 `targets.json` 里 `jacocoAgent` 要写**容器内
   路径**（`/opt/jacoco/jacocoagent.jar`）—— agent 是在容器里被加载的
2. `classDumpDir` 同理，写的是**容器内路径**，取的时候用 `docker cp`
3. pull 通道下 `bindAddress` 必须 `0.0.0.0` 且 **6300 要映射出来**，否则 hub 连不上；
   push 通道这两条都不需要

## docker compose

```yaml
services:
  my-service:
    image: myrepo/my-service:1.4.2
    environment:
      # 值用 covhub agent-opts my-service 生成后填进来
      JAVA_TOOL_OPTIONS: >-
        -javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*,classdumpdir=/tmp/covhub-classes/my-service,sessionid=1.4.2
    volumes:
      - /opt/jacoco-lib:/opt/jacoco:ro      # 先 covhub fetch-agent 下载到这里
    ports:
      - "8080:8080"
      - "6300:6300"                          # push 通道不需要这一行
```

## Kubernetes

agent jar 用 initContainer 从 hub 下载到共享 volume，避免改业务镜像，也不需要自己
维护一个带 jar 的镜像：

```yaml
spec:
  volumes:
    - name: jacoco
      emptyDir: {}
    - name: covclasses          # classdumpdir 落盘用，取完即弃
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
          value: "-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpclient,address=covhub.internal,port=6400,includes=com.example.*,classdumpdir=/tmp/covhub-classes/my-service,sessionid=my-service"
      ports:
        - { containerPort: 8080 }
      volumeMounts:
        - { name: jacoco,     mountPath: /opt/jacoco }
        - { name: covclasses, mountPath: /tmp/covhub-classes }
```

上面这份用的是 **push 通道**（`output=tcpclient`），这在 K8s 下通常更合适：

- Pod 不用暴露 6300，也不用 Service 固定地址
- **多副本天然汇聚** —— 每个副本自己连回 hub，扩缩容不用改 `targets.json`；
  用 pull 的话得给每个副本配一条

class 产物取一次即可（多副本是同一份产物，随便挑一个 Pod）：

```bash
POD=$(kubectl -n prod get pod -l app=my-service -o name | head -1)
kubectl -n prod cp "${POD#pod/}:/tmp/covhub-classes/my-service" ./cls
tar czf cls.tgz -C ./cls . && covhub upload-classes my-service 1.4.3 cls.tgz --retarget
```

**K8s 下发版尤其要注意**：滚动更新会直接杀掉旧 Pod，`preStop` 里来不及做完整的
dump + 归档。正确做法是在触发滚动更新**之前**，先跑
`covhub predeploy <service> <旧版本>`（一条 curl，不需要在流水线节点上装 covhub）。

> 漏跑了也不会静默累加：hub 每轮采集会比对 exec 里的会话启动时刻，发现进程换过就
> 自动结算上一周期。但**重启前最后一个轮询周期的数据仍然会丢**，能主动结算就还是
> 要主动结算。

## systemd（裸机 / 虚拟机）

用 drop-in 片段（`/etc/systemd/system/<unit>.d/covhub.conf`），原始 unit 文件不动，
撤下时删掉这个文件即可：

```ini
[Service]
# 值用 covhub agent-opts my-service 生成
Environment="JAVA_TOOL_OPTIONS=-javaagent:/opt/jacoco-lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*,classdumpdir=/tmp/covhub-classes/my-service"
Environment="COVHUB_URL=http://covhub.internal:8900"
# 停服前先结算。这里只是一条 curl —— 本机不需要 Python、java 和 targets.json。
# 超时保护避免 hub 无响应时卡住重启。
ExecStop=/usr/bin/timeout 60 /opt/bin/covhub-client.sh predeploy my-service
```

`ExecStop` 只在 systemd 自己停服务时触发（`systemctl stop/restart`）。进程被
`kill -9`、机器掉电时不会执行 —— 那种情况下 hub 的断代检测会兜住（自动结算并记一笔），
但最后一个轮询周期的数据就是丢了。真正可靠的做法还是把结算放进部署脚本的第一步。

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
#    产物来自 agent 的 classdumpdir，不需要构建流水线配合
tar czf cls.tgz -C /tmp/covhub-classes/my-service .
covhub upload-classes my-service "$NEW_VERSION" cls.tgz --retarget
rm -rf cls.tgz /tmp/covhub-classes/my-service

# 4. 确认新实例的 agent 已就绪
covhub wait-online my-service

# 5. 体检：确认这一版的 class 真的对得上
covhub diagnose my-service
```

整个脚本没有一行 Python —— 五步都是发给 hub 的 HTTP 请求，任何一步非 2xx 都会因
`set -e` 中断部署。

第 3 步是最容易被漏掉的：**class 产物必须跟着版本一起换**，否则新版本的 exec 会和旧
class 对不上，报告全是"未覆盖"**且不会报错**。第 5 步就是为了兜住它 —— 匹配率低会
当场看出来，而不是等一个月后才发现归档的全是废数据。

（`predeploy` 自己也会在归档前做一次同样的体检，匹配率低时告警但**不阻断结算** ——
exec 不可再生，因为对不上就拒绝归档只会两头落空。）

## 把运行期报告推 SonarQube

报告和 class 产物都在 hub 上，先取回来 —— 本机不必留任何历史产物：

```bash
# 报告：看板本身就是静态文件服务，按路径直接下
curl -sSf -o jacoco-runtime.xml \
  "$COVHUB_URL/my-service/versions/1.4.2/jacoco.xml"

# class：必须是采集时运行的那一份，否则 Sonar 上是 0%
covhub fetch-classes my-service 1.4.2 ./classes-1.4.2
```

```bash
sonar-scanner \
  -Dsonar.projectKey=my-service-runtime \
  -Dsonar.projectName="my-service (runtime coverage)" \
  -Dsonar.sources=/opt/src/my-service/src/main/java \
  -Dsonar.java.binaries=./classes-1.4.2 \
  -Dsonar.coverage.jacoco.xmlReportPaths=jacoco-runtime.xml
```

用独立的 project key，与单元测试的 project 并列，两者交叉才能看出既无单测、线上也
没人跑的代码（考虑删除），以及线上频繁执行却没有单测保护的（补测试的最高优先级）。
