# Jenkins 接入

两条流水线 + 一个 Shared Library。

| 文件 | 用途 |
|---|---|
| `vars/covhub.groovy` | Shared Library，把 covhub 命令封装成 pipeline 步骤 |
| `vars/deployTarget.groovy` | Shared Library，五种部署方式的实现 |
| `Jenkinsfile.build` | 构建期：跑测试 → 聚合报告 → 推 Sonar → **归档 class 产物** |
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

## 二、构建节点前置条件

| 依赖 | 说明 |
|---|---|
| Python 3 | covhub 只用标准库，不需要装任何包 |
| `java` | 用于跑 jacococli |
| covhub 安装目录 | 默认 `/opt/coverage-hub`，含 `covhub.py`、`lib/`、`targets.json` |
| Jenkins 插件 | Pipeline Utility Steps、Copy Artifacts、SonarQube Scanner；`Jenkinsfile.build` 里的 `jacoco` 步骤需要 JaCoCo 插件（可选，去掉不影响） |
| Config File Provider | 提供 Maven `settings.xml`，`fileId` 按你们实际的改 |

发版节点需要能连到被测服务的 agent 端口（默认 6300）。

## 三、构建期流水线要点

**不能加 `-DskipTests`。** 没有测试执行就没有 exec 数据，聚合报告是空的。

`-Dmaven.test.failure.ignore=true` 让个别用例失败时报告照样产出，但必须配合 `junit` 步骤把测试结果标记出来 —— 否则构建会假绿：Maven 返回 SUCCESS，实际有用例没过。

`API_VERSION=1.44` 这一条针对用 Testcontainers 的项目：Docker Engine 29+ 的最低 API 版本是 1.40，而 Testcontainers 1.21.x 内置的 docker-java 默认用 1.32，会被服务端以 HTTP 400 拒绝，症状是 `Could not find a valid Docker environment`。注意属性名是 `api.version`，不是 docker CLI 的 `DOCKER_API_VERSION`（后者设了完全无效）。

**归档 class 产物那一步不是可选的。** 运行期覆盖率出报告时，`--classfiles` 必须是当时运行的那份 class —— JaCoCo 按 CRC64 class id 匹配数据，class 对不上，报告全是"未覆盖"。构建时不归档，日后就没有任何办法为归档的 exec 重新出报告。

## 四、发版流水线的顺序

```
1. predeploy   结算旧版本覆盖率   ← 必须在停服之前
2. copy        取新版本 class 产物
3. deploy      停旧实例、部署、起新实例（agent 经 JAVA_TOOL_OPTIONS 注入）
4. retarget    更新 targets.json 的 version 与 classfiles
5. verify      轮询确认新实例 agent 就绪，打基线快照
6. sonar       把旧版本的 jacoco.xml 推上去
```

**第 1 步跑到停服之后，那段数据就永久丢失了** —— agent 随进程消失，tcpserver 端口关闭，没有任何补救手段。所以 `predeploy` 在目标不可达时会让流水线**失败退出**，这是有意的设计；确实要跳过时才勾 `ALLOW_MISSING`。

**第 4 步最容易漏。** class 产物必须跟着版本一起换，否则新版本采到的 exec 和旧 class 对不上。

流水线加了 `disableConcurrentBuilds()`：同一服务的发版不能并行，否则两次结算会互相干扰。

## 四·五、五种部署方式

发版流水线的 `DEPLOY_MODE` 参数选择部署方式，实现在 `vars/deployTarget.groovy`。
所有方式的共同点只有一个：**把 agent 参数塞进目标 JVM 的 `JAVA_TOOL_OPTIONS`**，
被测服务本身不需要任何改动。

| 方式 | 做法 | 关键参数 |
|---|---|---|
| `docker` | `docker rm -f` 旧容器 → `pull` → `run` 带 `-e JAVA_TOOL_OPTIONS` | `IMAGE`、`CONTAINER_NAME`、`APP_PORT`、`AGENT_PORT`、`AGENT_LIB_DIR` |
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
