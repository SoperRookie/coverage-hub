# SonarQube 两 project 并列

单元测试覆盖率和运行期覆盖率各占一个 project：

| project key | 数据来源 | 谁推 | 频率 |
|---|---|---|---|
| `my-service` | 构建期聚合报告 `jacoco-aggregate/jacoco.xml` | `Jenkinsfile.build` | 每次构建 |
| `my-service-runtime` | 运行期归档 `data/<svc>/versions/<ver>/jacoco.xml` | `Jenkinsfile.deploy` 第 6 步 | 每次发版 |

## 为什么不合并成一个

`sonar.coverage.jacoco.xmlReportPaths` 支持逗号分隔多份报告，Sonar 会把它们合并（某行在任一报告中被覆盖即算覆盖）。技术上可行，但对这个场景是错的 —— **合并之后正好把最有价值的信息抹掉了**：

- 有单测覆盖、但线上从没执行过 → 可能是死代码
- 线上频繁执行、却没有单测保护 → 补测试的最高优先级

这两类只有在两个数分开时才看得出来。合成一个"overall coverage"之后，你只知道总数涨了，不知道涨在哪一侧。

另外现代 SonarQube（6.2 之后，含 SonarCloud）**只有一个 `coverage` 指标**，早期的 unit / integration / overall 三个指标已被合并删除。所以同一个 project 里没有位置同时展示两个数 —— 这是硬限制，不是选择问题。

---

## 建立 runtime project

### 1. 创建

Sonar 上手工创建一个 project，key 用 `<主 project key>-runtime`，名字建议带上括号说明，比如 `order-service (runtime coverage)`，避免同事在列表里误以为是重复项目。

### 2. 关掉质量门禁

**这一步很重要。** 给 runtime project 挂一个空的质量门禁（Sonar 里建一个不含任何条件的 Quality Gate，命名如 `No Gate`，指给该 project）。

原因：运行期覆盖率天然低且**滞后于代码提交** —— 新代码提交时线上还没有这个版本，覆盖率必然是 0。套用单测的门禁（比如"新代码覆盖率 ≥ 80%"）会让每次发版都红，最后大家一起忽略告警，门禁就失效了。

runtime project 的定位是**观测**，不是**卡控**。

### 3. New Code 定义

设成 `Previous version`。配合发版时推送的版本号，Sonar 上就能按版本对比 —— 这正好对应运行期覆盖率"按版本切段"的采集方式。

### 4. 关掉不相关的分析

runtime project 只关心覆盖率，代码异味/重复率/安全热点这些在主 project 已经分析过了，重复一遍没有意义还拖慢扫描。可以在项目级别把不需要的规则集停掉，或直接用下面的 properties 模板（只扫覆盖率相关）。

---

## 推送方式

发版流水线第 6 步会自动推。手工推用 `push-runtime.sh`：

```bash
# 报告和 class 产物都在 hub 上，脚本会自己取回来；
# 本机只要有 curl、tar 和 sonar-scanner，不必留历史产物
export COVHUB_URL=http://covhub.internal:8900
export COVHUB_TOKEN=<hub 上配的 serve.token>
./push-runtime.sh order-service 1.4.2
```

或直接调 sonar-scanner：

```bash
sonar-scanner \
  -Dsonar.projectKey=order-service-runtime \
  -Dsonar.projectName='order-service (runtime coverage)' \
  -Dsonar.projectVersion=1.4.2 \
  -Dsonar.sources=/opt/src/order-service/src/main/java \
  -Dsonar.java.binaries=./classes-1.4.2 \
  -Dsonar.coverage.jacoco.xmlReportPaths=jacoco-runtime.xml

# 报告和 class 先从 hub 取回来：
curl -sSf -o jacoco-runtime.xml \
  "http://covhub.internal:8900/order-service/versions/1.4.2/jacoco.xml"
covhub-client.sh fetch-classes order-service 1.4.2 ./classes-1.4.2
```

### 两个前置条件

不满足这两条，Sonar 上会显示 0% 或者覆盖标记打在错误的行上：

1. **`sonar.java.binaries` 必须是采集时运行的那份 class。** JaCoCo 按 CRC64 class id 匹配，对不上等于没数据。这就是构建流水线要归档 class 产物、并在发版时 `upload-classes` 传给 hub 的原因 —— hub 上留着，日后用 `fetch-classes` 取回即可。
2. **`sonar.sources` 要 checkout 到对应版本。** Sonar 按文件 + 行号映射，源码版本不一致会把覆盖标记打到错行。

`push-runtime.sh` 会在推之前校验这两个路径是否存在，缺一个就报错退出，避免推上去一份看似成功实则错位的数据。

---

## 日常怎么看

**主 project** 照常用：新代码覆盖率门禁、代码异味、重复率。

**runtime project** 关注两件事：

- 单个版本的绝对值 —— 这一版上线后，有多少代码真的被执行过
- 版本间趋势 —— 回归测试范围有没有退化

**交叉分析**（最有价值的部分）Sonar 帮不上忙，两个 project 之间它不会自动比对。真要做死代码识别，直接拿两份 `jacoco.xml` 做差集更准也更快。需要的话可以给 covhub 加个 `crossref` 子命令，输出「无单测且线上未执行」和「线上高频执行但无单测」两张清单。

---

## 文件

| 文件 | 用途 |
|---|---|
| `sonar-project.runtime.properties` | runtime project 的扫描配置模板 |
| `push-runtime.sh` | 手工推送脚本，带前置校验 |
