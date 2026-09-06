# Jenkins 接入

两条流水线 + 一个 Shared Library。

| 文件 | 用途 |
|---|---|
| `vars/covhub.groovy` | Shared Library，把 covhub 命令封装成 pipeline 步骤 |
| `vars/deployTarget.groovy` | Shared Library，五种部署方式的实现 |
| `Jenkinsfile.build` | 构建期：跑测试 → 聚合报告 → 推 Sonar → 归档 class 产物（**要改研发的 pom，不能改就整条跳过**） |
| `Jenkinsfile.deploy` | 发版：结算旧版本 → 部署 → 指向新产物 → 确认采集恢复 |

## 一、安装 Shared Library

把 `integration/jenkins` 目录作为一个 Git 仓库（`vars/` 必须在仓库根目录），然后：

**Manage Jenkins → System → Global Pipeline Libraries**

| 字段 | 值 |
|---|---|
| Name | `covhub` |
| Default version | `main` |
| Retrieval method | Modern SCM → Git，指向该仓库 |

之后在 Jenkinsfile 顶部写 `@Library('covhub') _` 即可使用。

## 二、节点前置条件

**节点上不需要装 covhub。** 覆盖率相关的活全由那一个 hub 完成，流水线只是发 HTTP 请求：

| 依赖 | 说明 |
|---|---|
| `curl` | 就这一个。不需要 Python、不需要 java、不需要 `targets.json` |
| 环境变量 `COVHUB_URL` | hub 地址，如 `http://covhub.internal:8900`。已写在 `Jenkinsfile.deploy` 的 `environment` 块里，改成你们的 |
| 凭据（可选） | hub 配了 `serve.token` 时，建一个 Secret text 凭据存令牌，把 ID 填进 `COVHUB_TOKEN_ID` |
| Jenkins 插件 | Pipeline Utility Steps（`covhub.diagnose` 用它的 `readJSON`）、Copy Artifacts、SonarQube Scanner；`Jenkinsfile.build` 里的 `jacoco` 步骤需要 JaCoCo 插件（可选，去掉不影响） |
| Config File Provider | 提供 Maven `settings.xml`，`fileId` 按你们实际的改 |

发版节点**不需要**能连到被测服务的 agent 端口 —— 连 agent 的是 hub。它只要能连上 hub 的 8900。
也**不需要**本地的 class 产物库：新产物传给 hub，旧产物推 Sonar 时用
`covhub.fetchClasses` 取回工作区。

> **本地模式（兜底）**：Jenkins agent 恰好就跑在 hub 那台机器上时，可以给各步骤传
> `home: '/opt/coverage-hub'` 而不是 `hub:`，库会退回到直接调 `covhub.py`。
> 此时才需要节点上有 Python 3、java 和 `targets.json`。没设 `COVHUB_URL` 也没传
> `hub:` 时自动走这条路。

## 三、构建期流水线要点

**不能加 `-DskipTests`。** 没有测试执行就没有 exec 数据，聚合报告是空的。

`-Dmaven.test.failure.ignore=true` 让个别用例失败时报告照样产出，但必须配合 `junit` 步骤把测试结果标记出来 —— 否则构建会假绿：Maven 返回 SUCCESS，实际有用例没过。

`API_VERSION=1.44` 这一条针对用 Testcontainers 的项目：Docker Engine 29+ 的最低 API 版本是 1.40，而 Testcontainers 1.21.x 内置的 docker-java 默认用 1.32，会被服务端以 HTTP 400 拒绝，症状是 `Could not find a valid Docker environment`。注意属性名是 `api.version`，不是 docker CLI 的 `DOCKER_API_VERSION`（后者设了完全无效）。

**归档 class 产物那一步现在是可选的。** 运行期出报告时 `--classfiles` 必须是当时运行的那份 class（JaCoCo 按 CRC64 class id 匹配，对不上报告全是"未覆盖"），但 v1.1.0 起这份 class 由 agent 的 `classDumpDir` 在运行期自己交出，不再需要构建流水线配合。

保留构建期归档只对「愿意改构建、想两份都留着」的项目有意义；两份都有时 covhub 优先用 classdumpdir 那份 —— 它是运行时真相，还包含构建产物里根本不存在的动态生成类。

## 四、发版流水线的顺序

```
1. predeploy       结算旧版本覆盖率   ← 必须在停服之前
2. copy            取新版本 class 产物
3. deploy          停旧实例、部署、起新实例（agent 经 JAVA_TOOL_OPTIONS 注入）
4. upload-classes  把产物传给 hub，并把配置指过去（retarget）
5. verify          轮询确认 agent 就绪 → 打基线快照 → 校验 class 指纹对得上
6. sonar           取回旧版本的 jacoco.xml 与 class 产物，推上去
```

**第 1 步跑到停服之后，那段数据就永久丢失了** —— agent 随进程消失，tcpserver 端口关闭，没有任何补救手段。所以 `predeploy` 在目标不可达时会让流水线**失败退出**，这是有意的设计；确实要跳过时才勾 `ALLOW_MISSING`。

**第 4 步最容易漏。** class 产物必须跟着版本一起换，否则新版本采到的 exec 和旧 class 对不上。报告是 hub 出的，所以产物要传到 hub 上去 —— `covhub.uploadClasses(..., retarget: true)` 一步做完上传和指向。

**第 5 步的 `requireMatch` 就是为了兜住第 4 步。** JaCoCo 在 class 对不上时不会报错，只是把报告渲染成「全部未覆盖」—— 不主动校验的话，要等到有人去看报告才会发现，那时这段时间的数据已经全废了。`DIAGNOSE_MIN_MATCH` 参数控制阈值（默认 90%，填 0 关闭）。刚起的服务还没有执行数据时匹配率不适用，此时只告警不失败。

流水线加了 `disableConcurrentBuilds()`：同一服务的发版不能并行，否则两次结算会互相干扰。

## 四·五、五种部署方式

发版流水线的 `DEPLOY_MODE` 参数选择部署方式，实现在 `vars/deployTarget.groovy`。
所有方式的共同点只有一个：**把 agent 参数塞进目标 JVM 的 `JAVA_TOOL_OPTIONS`**，
被测服务本身不需要任何改动。

| 方式 | 做法 | 关键参数 |
|---|---|---|
| `docker` | `docker rm -f` 旧容器 → `pull` → `run` 带 `-e JAVA_TOOL_OPTIONS` | `IMAGE`、`CONTAINER_NAME`、`APP_PORT`、`AGENT_PORT`、`AGENT_LIB_DIR`（留空则从 hub 下载 agent 到工作区） |
| `compose` | 生成 `docker-compose.covhub.yml` override 注入环境变量与端口，**不改原始 compose 文件** → `compose up -d` | `COMPOSE_FILE`、`COMPOSE_SERVICE`、`IMAGE`、`AGENT_PORT` |
| `k8s` | `kubectl set image` + `set env` 合并成一次滚动更新 → `rollout status` 等待完成 | `K8S_NAMESPACE`、`K8S_DEPLOYMENT`、`K8S_CONTAINER`、`IMAGE` |
| `systemd` | 写 drop-in 片段 `/etc/systemd/system/<unit>.d/covhub.conf` 注入环境变量，**不改原始 unit 文件** → `daemon-reload` + `restart` | `SYSTEMD_UNIT`、`ARTIFACT_SRC`、`ARTIFACT_DEST` |
| `script` | 调你们自己的部署脚本，参数以**环境变量**传入（避免命令行转义问题） | `DEPLOY_SCRIPT`，脚本内可用 `$COVHUB_JAVA_TOOL_OPTIONS`、`$COVHUB_SERVICE`、`$COVHUB_VERSION` |

### 容器方式的一个易错点

`docker` 和 `compose` 会把宿主机的 `AGENT_LIB_DIR` 挂到容器内（默认 `/opt/jacoco`）。
此时 **`targets.json` 里的 `jacocoAgent` 必须写成容器内路径**（如
`/opt/jacoco/jacocoagent.jar`），因为 agent 是在容器里被 JVM 加载的；而 `jacocoCli`
仍然是执行采集那台机器上的路径。两者不在同一个文件系统里。

另外 `AGENT_PORT` 必须映射出来，否则 covhub 连不到 agent —— 且 `targets.json` 里
该服务的 `bindAddress` 要是 `0.0.0.0`，绑回环地址时容器外无法访问。

### K8s 的两点额外要求

**一、agent jar 要能进 Pod。** 业务镜像不方便改时，标准做法是给 Deployment 加一个
initContainer，把 `jacocoagent.jar` 拷进 `emptyDir` 共享卷。清单片段见
`../deployment-snippets.md`。这属于 Deployment 的一次性改造，不在流水线范围内。

**二、结算必须在滚动更新之前。** 滚动更新直接杀旧 Pod，`preStop` 钩子来不及做完整的
dump + 归档。流水线第 1 步就是干这个的，顺序不能调整。

### 撤下监控

不再采集时：`docker`/`compose` 去掉环境变量重启即可；`systemd` 删掉那个 drop-in 文件
再 `daemon-reload`；`k8s` 用 `kubectl set env deployment/X JAVA_TOOL_OPTIONS-`（末尾减号
表示删除该变量）。原始的 unit 文件、compose 文件、镜像自始至终没被改过。

## 五、Sonar 的 project 划分

建议**单元测试和运行期用两个独立的 project key**：

| project key | 数据来源 |
|---|---|
| `my-service` | 构建期聚合报告 |
| `my-service-runtime` | 运行期归档的 jacoco.xml |

并列之后能做横向对比，交叉出来的两类代码最有价值：

- **既无单测、线上也没人跑** —— 可以考虑删除
- **线上频繁执行却没有单测保护** —— 补测试的最高优先级

## 六、K8s 滚动更新的额外注意

滚动更新会直接杀掉旧 Pod，`preStop` 钩子里来不及做完整的 dump + 归档。正确做法是在触发滚动更新**之前**，先在流水线里跑 `predeploy`（也就是 `Jenkinsfile.deploy` 的第 1 步），而不是依赖 Pod 生命周期钩子。

## 六、Shared Library 提供的步骤

公共参数：`hub`（或环境变量 `COVHUB_URL`）、`tokenCredentialsId`；
本地模式下则是 `home` / `config` / `python`。

| 步骤 | 用途 |
|---|---|
| `covhub.agentOpts(service:)` | 取该服务应注入的 `-javaagent` 参数串 |
| `covhub.predeploy(service:, version:, allowMissing:)` | 结算并归档，**停服之前**调用 |
| `covhub.dump(service:)` | 拉一次快照（累加） |
| `covhub.status([service:])` | 打印连通性与最新覆盖率 |
| `covhub.online(service:)` | 目标 agent 是否可连通，返回 boolean |
| `covhub.retarget(service:, version:, classfiles:)` | 更新 hub 配置里的版本与 class 路径 |
| `covhub.uploadClasses(service:, version:, archive:, retarget:)` | 把 class 产物压缩包传给 hub |
| `covhub.fetchAgent(dest:)` | 从 hub 下载 `jacocoagent.jar` |
| `covhub.fetchClasses(service:, version:, dest:)` | 从 hub 取回某版本的 class 产物并解包，返回目录 |
| `covhub.diagnose(service:, version:)` | 诊断 exec 与 class 是否对得上，返回含 `matchRate` / `verdict` 的 Map |
| `covhub.requireMatch(service:, min:)` | 指纹匹配率低于 `min`（默认 90）就让流水线失败 |
| `covhub.fetchReport(service:, version:, dest:)` | 从 hub 取回某版本的 `jacoco.xml` |
| `covhub.pushSonar(projectKey:, xmlReport:, binaries:, sources:)` | 推 SonarQube |

除 `agentOpts` / `online` / `status` 外，任何一步在 hub 返回非 2xx 时都会让流水线失败 ——
覆盖率结算失败必须停住发版，而不是带着已丢失的数据继续。
