# coverage-hub

通用 JaCoCo 覆盖率方案：**构建期**自动出聚合报告推 SonarQube，**运行期**随服务启动自动采集、发版前自动结算、并提供实时在线看板。

与被测服务无关 —— 任何 Java 服务只要能加 JVM 参数就能接入，不需要改被测项目的代码或 pom。

> **要接入一个新服务，直接看 [ONBOARDING.md](ONBOARDING.md)** —— 从搭建到验收的完整步骤、四种部署方式的注入方法、验收清单和常见问题。本文说明的是设计与命令细节。

---

## 一、运行期覆盖率

### 1. 服务启动时开始采集

给被测服务注入 agent，`output=tcpserver` 模式。**用环境变量注入，不需要改镜像、不需要改启动类**：

```bash
export JAVA_TOOL_OPTIONS="$(python covhub.py agent-opts my-service)"
# 然后照常启动服务
```

`agent-opts` 会根据 `targets.json` 里该服务的配置生成完整参数串，例如：

```
-javaagent:/opt/coverage-hub/lib/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,includes=com.example.*,sessionid=1.4.2
```

`JAVA_TOOL_OPTIONS` 是 JVM 标准环境变量，`java -jar`、Spring Boot、Tomcat、`run-java.sh`、K8s 都认，无需关心服务是怎么启动的。

### 2. 重启 / 发版前自动 dump

```bash
python covhub.py predeploy my-service --version 1.4.2
```

它会：dump 并 `--reset` → 生成终版报告 → 连同该周期全部 exec 归档到 `data/<service>/versions/1.4.2/` → 写 manifest 记录对应的 class 产物。

> **顺序不能反。** 服务一停，agent 随之消失，那段覆盖率数据永久丢失。`predeploy` 必须在停服之前执行 —— 把它放进部署脚本的第一步。

命令在目标不可达时会**报错退出**（非零码），这是有意的：让部署流程停下来，而不是静默丢数据。确实需要跳过时用 `--allow-missing`。

### 3. 实时在线查看

```bash
python covhub.py watch          # 守护进程，按间隔轮询全部目标
python covhub.py serve          # HTTP 服务托管看板，默认 8900 端口
```

看板首页列出全部服务：在线状态、指令/分支覆盖率、触达类数、趋势曲线、已结算版本列表，可下钻到 JaCoCo 原生报告看到具体哪一行被执行过。页面每 60 秒自动刷新。

单次采集用 `python covhub.py dump my-service`（累加，不清零）。

### 为什么必须按版本切段

JaCoCo 用类的 CRC64 指纹（class id）把 exec 数据和 class 文件对应起来。**发版换了 class，旧 exec 就作废了** —— 拿新 class 去渲染旧 exec，只会得到一份"全部未覆盖"的假报告。

所以运行期覆盖率不能像单元测试那样一直累加，必须以版本为周期：一个版本一个采集周期，发版时 `predeploy` 结算归档，新版本从零开始。归档目录里的 `manifest.json` 记录了该 exec 对应哪份 class 产物，这是日后能重新出报告的唯一依据。

---

## 二、构建期覆盖率（推 SonarQube）

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

---

## 二·五、Jenkins 接入

见 `integration/jenkins/`：一个 Shared Library（`vars/covhub.groovy`）加两条流水线模板。

- `Jenkinsfile.build` —— 构建期：跑测试 → 聚合报告 → 推 Sonar → **归档 class 产物**
- `Jenkinsfile.deploy` —— 发版：结算旧版本 → 部署 → 指向新产物 → 确认采集恢复 → 推 Sonar

安装步骤、节点前置条件与各步骤的注意事项见 `integration/jenkins/README.md`。

---

## 三、配置

`targets.json`（可从 `targets.example.json` 复制，或用 `covhub.py init` 生成）。它含各环境地址与路径，每台机器不同，已被 `.gitignore` 排除 —— 版本库里维护的是 `targets.example.json`。

同样被排除的还有 `lib/*.jar`（由 JaCoCo 发行包提供，按需放入）和 `data/`（采集产物）。

> `data/<service>/versions/` 下的 exec 是**不可再生**的真实执行轨迹。需要长期留存的话请归档到对象存储或制品库，别指望 git。

```json
{
  "jacocoAgent": "./lib/jacocoagent.jar",
  "jacocoCli":   "./lib/jacococli.jar",
  "dataDir":     "./data",
  "serve":  { "port": 8900 },
  "watch":  { "intervalSeconds": 300 },
  "services": [
    {
      "name":        "my-service",
      "version":     "1.4.2",
      "address":     "127.0.0.1",
      "port":        6300,
      "bindAddress": "0.0.0.0",
      "includes":    ["com.example.*"],
      "excludes":    [],
      "classfiles":  ["/opt/artifacts/my-service/1.4.2/classes"],
      "sourcefiles": ["/opt/src/my-service/src/main/java"],
      "reportExcludes": ["com/example/**/dto/**", "com/example/*/mapper/**"],
      "sourceEncoding": "UTF-8"
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `address` / `port` | covhub 连过去拉数据的地址；`bindAddress` 是 agent 在被测端监听的地址 |
| `includes` / `excludes` | **传给 agent 的**，类名用 `.` 分隔，多项用 `:`（工具会自动拼） |
| `classfiles` | 出报告用的 class，**必须与运行中的服务是同一份产物** |
| `reportExcludes` | **报告端过滤**，Ant 风格路径模式（用 `/`）。CLI 的 `report` 不支持排除，工具会先过滤出一份 class 副本再出报告 |
| `sourcefiles` | 可选。配了才能在报告里下钻到源码行 |

`includes`/`excludes` 与 `reportExcludes` 是两个层次：前者决定**是否插桩**（被排除的类连数据都不会产生，事后无法找回），后者只影响**报告统计口径**（随时可调，重出报告即可）。

相对路径一律相对 `targets.json` 所在目录解析，整个目录可以直接搬到别的机器上。

---

## 四、命令一览

| 命令 | 用途 |
|---|---|
| `init` | 生成配置模板 |
| `agent-opts <service>` | 打印启动时应注入的 `-javaagent` 参数串 |
| `status [service]` | 目标连通性与最新覆盖率 |
| `dump <service>` | 拉一次快照并出报告（累加，不清零） |
| `predeploy <service> [--version V]` | 发版/重启前结算：`dump --reset` + 归档 |
| `report <service>` | 用已有 exec 重新出报告（改了 `reportExcludes` 后用） |
| `watch [--interval N]` | 守护进程，定时轮询全部目标 |
| `serve [--port N]` | HTTP 托管看板 |

只依赖 Python 3 标准库和 `java`，无第三方包。

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
    versions/<版本>/             发版结算归档（报告 + exec + manifest）
    classes/                    按 reportExcludes 过滤后的 class 副本
    state.json                  历史统计，看板趋势曲线的数据源
```

---

## 六、注意事项

- **tcpserver 端口没有认证。** 任何能连上的人都能拉数据、并通过 `--reset` 清空计数器。生产/共享环境务必用防火墙或安全组限制来源，不要暴露到公网。
- **性能开销通常在个位数百分比**，可用于测试环境常驻，但不建议长期挂在生产上。
- **exec 是不可再生资产**，尤其是手工测试采集的数据。归档时务必连同对应的 class 产物一起保存，否则日后无法重新出报告。
- **覆盖率不是质量指标。** 它只说明代码被执行过，不说明断言是否有效。分支覆盖率通常比指令覆盖率更有参考价值。
