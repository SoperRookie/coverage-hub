# 更新日志

## v2.6.1（2026-10-08）

- 修复：push 服务一有在线实例，看板详情页就 500。详情接口把收集端的连接记录原样放进返回体，
  里面握着 socket 对象，JSON 序列化失败。`collector_instances()` 现在只给 peer / since / last 等
  能序列化的字段，总览与 `status` 不受影响（它们只数个数）。2.6 之前 push 实例常年连不上才没暴露。
- Windows 的 covhub-agent 构建脚本由 cmd 批处理 `agent\build.cmd` 换成 PowerShell `agent\build.ps1`
  （`powershell -ExecutionPolicy Bypass -File agent\build.ps1`；带 UTF-8 BOM，注释可以是中文）。两个脚本仍然等价。
- 文档按当前实现逐项对账：README 的命令一览 / 接口表补齐漏掉的参数，ONBOARDING 里过时的返回体示例、
  迁移号、版本号、`classfiles` 必填与否、`Jenkinsfile.deploy` 的实际阶段等改正；integration 下各模板的注释同步。
- 对账顺手修掉的几处：CLI 不再要求 `jacocoAgent` 在 hub 本机存在（它是被测端路径，按文档改成容器内路径后
  `serve` / `dump` 会拒绝启动）；`diff` 子命令的 `version` 改为必填（原来省掉它时文件名会被当成版本）；
  Jenkins 库 `fetchReport` 带上令牌（hub 配了 `serve.token` 时原来必 401）；`Jenkinsfile.deploy` 的 `IMAGE`
  不带 tag 时由流水线补 `:NEW_VERSION`（参数默认值里的 `${NEW_VERSION}` 不会被展开）；k8s 方式 `rollout pause`
  后再改镜像与环境变量，合成一次滚动；`diffs.origin` 在 models 里补 `server_default`，与迁移 0003 一致。

## v2.6.0（2026-10-06）

**push 通道换上自带的薄 agent `covhub-agent.jar`：hub 不在时被测服务照常启动，hub 重启后自己重连。**
JaCoCo 自带的 `output=tcpclient` 有两个改不了的行为 —— 启动时连不上收集端，agent 初始化抛异常，
**被测 JVM 直接起不来**（hub 停机维护期间谁都发不了版）；连接断了之后**不再重连**，hub 一重启，
所有 push 实例此后的覆盖率都取不到，直到被测服务各自重启。

- 新增 `lib/covhub-agent.jar`（源码 `agent/`，一个类、零依赖、Java 8 字节码，`agent/build.sh` 构建（Windows 脚本见「未发布」），产物进
  版本库）。它与 `jacocoagent.jar` **并列挂在被测 JVM 上**：JaCoCo 改用 `output=none` 只插桩，`covhub-agent`
  在 daemon 线程里连 hub 的收集端，数据经 JaCoCo 的公开入口 `org.jacoco.agent.rt.RT` 取（只用反射，不绑定
  JaCoCo 版本）。线上仍是 JaCoCo 的 remote control 协议 —— **收集端、exec 格式、JaCoCo 的 jar 都没改**。
- 连不上就后台退避重试（1 秒起、30 秒封顶，只在第一次失败时打一行日志）；断了自己连回来；`idle` 秒没收到
  hub 的指令也重连，兜住收不到 FIN 的断线（hub 掉电、NAT 回收空闲连接）。任何失败都只打 `[covhub-agent]`
  前缀的日志，不影响被测应用。
- 新配置项 `covhubAgent`（和 `jacocoAgent` 一样填**被测端**路径，`init` 的模板默认带）。配了它，push 服务的
  `agent-opts` 就是**空格隔开的两个 `-javaagent`**，`excludes` 末尾自动追加 `covhub.agent.*` 与
  `org.jacoco.agent.rt.*`（实测不排除的话这两个类会被插桩、混进 exec）。`idle` 按三轮 `watch.intervalSeconds`
  给，不低于 180 秒。pull 通道不受影响。
- 新增 `GET /api/covhub-agent.jar`、`covhub-client.sh fetch-covhub-agent [目标路径]`、groovy
  `covhub.fetchCovhubAgent(dest:)`；`Jenkinsfile.deploy` 在参数串有两个 `-javaagent` 时把两个 jar 下到同一个目录。
  K8s initContainer 片段改成下两个 jar。
- **升级不强制**：没配 `covhubAgent` 的老配置原样生成 `output=tcpclient`，已经在跑的实例不受影响；hub 启动时
  会提醒一行。要换上：`covhub.yaml` 加一行 `covhubAgent: <被测端路径>`，被测端 `fetch-covhub-agent`，
  重新取 `agent-opts`，下次重启被测服务时生效。
- 没做的：进程退出时主动推最后一段数据（仍靠 `predeploy` 在停服前结算）；class / 版本由 agent 自报。

## v2.5.0（2026-10-05）

**diff 由 hub 比对两版源码生成，流水线不再在构建节点上算 git diff。** 原来构建节点要有
上一版 commit 的历史：Jenkins `cleanWs` 过的工作区、浅克隆、只拉一个 tag，`rev-parse` 就找不到
基线，diff 静默变空、新增代码覆盖率永远算不出来。hub 手里按版本存着 `upload-sources` 传来的
源码，两棵树一比就是 diff，和历史深度、工作区死活都没关系。

- `POST /api/upload-sources` 存好源码后默认就生成这一版的 diff（`git diff --no-index -M`，识别重命名，
  输出与流水线那条命令同形，剥掉路径里的版本目录、丢掉 `.roots.json`）；加 `diff=skip` 只存源码，
  `base=<版本>` 指定基线。返回体多 `diff` / `diffReason`。生成不了（第一次接入没有基线、hub 没装 git）
  不算上传失败，`diffReason` 说原因。**hub 机器要装 git**。
- `POST /api/diff` 加 `from=sources`（不读正文）：显式让 hub 比对、指定基线或重做；`base` 改为可选
  （上传 git diff 时仍必填，缺了 400）。返回体多 `baseReason`。
- 基线自动定：服务当前 `version`（线上跑着的那版）→ 最近结算的版本 → 最近上传过源码的版本，取第一个
  传过源码的。
- `diffs` 表加 `origin`（`upload` / `sources`，迁移 `0003`）：**自动生成的不覆盖流水线上传的**，显式
  `from=sources` 才覆盖。DBA 建表 SQL 同步。
- CLI：`diff <svc> [ver] --from-sources [--base V]`、`upload-sources ... [--base V] [--no-diff]`；
  `covhub-client.sh` 同样；groovy `covhub.pushDiff(fromSources: true, base:)`、
  `covhub.uploadSources(base:, diff:)`。`Jenkinsfile.build` 去掉整段 git diff，只剩 `uploadSources`。
  `DIFF_BASE` 的含义从 commit 变成版本号。
- `lastVersion` / `last-version` / `GET /api/services/{name}/versions` 保留，给仍自己算 git diff 的项目定基线。

## v2.4.0（2026-09-28）

**源码按版本上传。** hub 独立部署后本机没有源码，原来只能在 hub 上 checkout 仓库、把
`sourcefiles` 指过去 —— 要 git 权限，每次发版有人去切版本，且一个目录只能对一个版本
（旧版本的新增代码因此看不到源码）。

- 新增 `POST /api/upload-sources?service&version`、`covhub-client.sh upload-sources <svc> <ver> [包]`、
  groovy `covhub.uploadSources`、CLI `covhub upload-sources <svc> [ver] <包或目录>`。客户端不给包时在当前
  git 仓库里现打受版本控制的 `.java/.kt/.groovy/.scala`（去掉 `src/test/`，路径相对仓库根、与 git diff 一致）。
  `Jenkinsfile.build` 的 `Push to covhub` 阶段已带上这一步。
- hub 存到 `data/<svc>/sources/<版本>/`：**只收源码扩展名**（配置文件混进包里也不落盘），不剥顶层目录
  （单模块仓库的模块目录是 diff 路径的一部分），按每个文件的 `package` 声明识别源码根 —— 多模块、非标准
  目录、sources.jar 平铺都不用配。同版本重传整份替换，解到临时目录再换上，传坏了旧的还在。
- 出报告按服务当前 `version` 取源码根传给 `--sourcefiles`：JaCoCo 类页面有逐行红绿标记，HTML 生成时内嵌
  源码，归档报告不再依赖它。归档 `manifest.json` 多记一项 `sourcefiles`（实际用的源码根）。
- 看板「新增代码」点开可「展开全文」（`GET /api/services/{name}/source` 加 `full=1`，返回体加
  `sourceVersion` / `fullAvailable` / `full` / `totalLines`）；服务发了新版本之后旧版本的也照样对得上。
  详情页在这一版没传源码时给提示（`detail` 的 `runtime.sourcesUploaded`）。
- 服务配置里的 `sourcefiles` 降为兜底，且**只对服务当前 `version` 生效** —— 它是会跟着发版改掉的目录，
  拿它去对旧版本只会错位（原先的源码视图会这么做）。
- `/<svc>/sources/` 不经静态路径外发（按 resolve 后的路径判断，`//`、`..` 绕不过去），源码只通过报告与
  源码视图接口出去，都要令牌。nginx 模板本就不反代它。

## v2.3.2（2026-09-22）

- 新增 `covhub export [--out FILE] [--json]` 与 `GET /api/export`：把库里的项目与服务配置导成
  `import` 能吃的文件。服务配置在数据库里，换一个库（比如在 `covhub_dev` 上调完切回生产库）
  配置不会自己长出来，之前只能逐个 `service add` 重登记。导出的是入库原文：相对路径不展开、
  None 的标量不出现，导回去不会给 `bindAddress` 等填上默认值。
- `import` 同时认 `projects` 段；服务引用的项目在目标库不存在时按名字自动建出（旧
  `targets.yaml` 没有项目这一层，不该卡在「先 `project add`」上）。返回体多了 `projects` 计数。

## v2.3.1（2026-09-16）

- 启动日志多一行「看板由谁托管」：未配 `serve.webDir` 时说明本进程只发 API 与报告目录，
  配了就直接给出看板地址；配了却指向没有 `index.html` 的目录时告警 —— 那种坏法很隐蔽，
  API 一切正常、只有看板 404。「打开 8900 怎么是一页说明」是分离部署后最常见的困惑，
  与其让人翻文档，不如启动时说明白。
- ONBOARDING 第 2 章补上「部署看板前端」这一步（原来从起 hub 直接跳到验证，整章没有一步
  是部署前端的），验证拆成 hub / 看板 / 浏览器三段并配排障对照表。
- 修正文档里两处与实际不符的说法：`serve.token` 与 `serve.webDir` 改完**不用重启**
  （配置文件每个请求重读，实测改完旧令牌当场 401、新令牌立即可用），只有 `serve.port` /
  `watch` / `collect` / `database` 是启动时读一次的。

## v2.3.0（2026-09-16）

**前后端分离部署。** 看板不再随 Python 包分发，前端和 hub 是两个交付物。

- 前端产物从 `covhub/webui/` 挪到 **`web/dist`**（仍进版本库，hub / nginx 机器不装 node），
  不再进 wheel（`package-data` 只剩后端资源）。部署时解包给 nginx，模板见
  **`integration/nginx/covhub.conf`**：前端由 nginx 发，`/api/*`、`/docs`、`/swagger/`、
  `/<服务>/(current|versions|unit|artifacts|diff)/` 反代给 hub。
- **前端与 hub 必须同源**（模板就是这么配的）。鉴权认 Cookie，所以一个 CORS 头都不发 ——
  开跨域等于让任意页面替已登录的浏览器调写接口。
- **新增 `POST /api/login`**：令牌走 `X-Covhub-Token` 头换一个 `HttpOnly` Cookie，看板的登录入口。
  分离部署后首页由 nginx 发，hub 收不到 `/?token=`，老的种 Cookie 路径走不通了；令牌也不再进
  地址栏、浏览器历史和 Referer。静态目录的 `?token=` **保留**，直接分享出去的报告链接还靠它。
- **`serve.webDir` 升为正式配置项**：留空 = 分离部署，hub 只做 API + 报告目录，根路径给一页说明；
  指向产物目录则由 hub 一起托管，回到 2.2 及以前的一体形态（单机够用，不必装 nginx）。
  相对路径现在按配置文件所在目录解析（之前跟着进程 CWD 走）。
- **`/docs` 与前端构建解耦**：Swagger UI 的资源移到 Python 包里的 `covhub/static/swagger/`，
  由 `api/docs.py` 自己的白名单路由发出。纯后端部署下接口文档照常可用；
  换 swagger-ui 版本时跑 `cd web && npm run swagger`。
- 服务名保留字补上 `docs`、`swagger`（这两个路径已被后端路由占用）。

## v2.2.1（2026-09-15）

- 修复：一个版本周期攒下几百个 exec 后，`report` / `merge` / `execinfo` 把全部文件塞进一条命令行，
  Windows 上撞 CreateProcess 的 32767 字符上限（`WinError 206 文件名或扩展名太长`），采集与 dump 全部失败。
  现在超长时自动分批：execinfo 分批拼接输出，merge 滚动合并，report 先合并成临时 exec 再出报告（结果一致）。

## v2.2.0（2026-09-14）

- **移除 SonarQube 集成**：删掉 `integration/sonar/`（`push-runtime.sh`、runtime project 说明与属性文件）、
  Jenkins 共享库的 `covhub.pushSonar`、`Jenkinsfile.build` 的 SonarQube 阶段与 `SONAR_*` 变量、
  `Jenkinsfile.deploy` 的第 6 步「推旧版本覆盖率到 Sonar」；README / ONBOARDING / 部署片段里相关章节一并删除。
  `fetchReport` / `fetchClasses` / `fetch-classes` 保留（取回某版本的 jacoco.xml 与 class 产物仍有用）。
  覆盖率的展示与门禁以 hub 看板和报表为准。

## v2.1.1（2026-09-14）

- `covhub-client.sh`：统一请求函数；连接超时 10s / 单请求总超时 600s（`COVHUB_CONNECT_TIMEOUT` / `COVHUB_TIMEOUT`）；
  只读 GET 自动重试，`dump` / `predeploy` 不重试；401 / 404 / 409 分类提示，退出码 0 / 1 / 2；`fetch-classes` 拒绝清空
  `/`、`.`、`$HOME`；新增 `last-version <svc> --plain`（构建节点定 diff 基线不再需要 python3）与 `recompute`。
- `docs/sql/`：MySQL 8 建库建用户、建表脚本（与 Alembic 迁移逐项比对一致，DBA 不给 DDL 权限时用）。
- README 加效果图；看板左上角品牌改为 coverage-hub。
- `docs/diagrams/` 不再进版本库。

## v2.1.0（2026-09-13）

从单文件脚本升级为「包 + 数据库 + FastAPI + Vue 看板」的完整形态，新增项目维度、单测 / 新增代码覆盖率、历史版本与对比、报表导出、深色主题与内置接口文档。上一版 v1.2.2 之后的 37 个提交（含未单独打标签的 v1.3.0「配置支持 YAML」）全部并入本版。

### 架构

- `covhub.py` 拆成 `covhub/` 包（`config` / `db` / `incremental` / `build` / `cycle` / `views` / `ops` / `api` / `cli`），`covhub.py` 只剩入口壳，命令用法不变。
- **服务配置与覆盖率历史入库**：SQLAlchemy 2 + Alembic；`services`、`service_state`、`snapshots`、`archives`、`breaks`、`projects`、`unit_reports`、`diffs` 八张表。`targets.yaml` 的 `services` 段与 `data/*/state.json` 退役，`covhub import` 幂等导入。主验证库 MySQL 8（PyMySQL），SQLite / PostgreSQL 可跑；测试可用 `COVHUB_TEST_DATABASE_URL` 指向真库运行。
- **HTTP 层换成 FastAPI**：路径与返回体逐字段兼容旧脚本（缩进 JSON、400 而非 422、令牌三来源），新增 `/api/services`、`/api/projects`、`/api/import`。
- 示例配置改为 `covhub.example.yaml` + `service.example.yaml`，由代码里的模板导出；`lib/` 下两个 JaCoCo jar 进版本库，克隆即可跑。

### 数据能力

- **项目维度**：服务归属项目（同一时间只属一个），看板按项目分组。
- **单测覆盖率与新增代码覆盖率**：构建流水线推送 `jacoco.xml`（`POST /api/unit-coverage`）与 `git diff`（`POST /api/diff`），hub 按行级数据求交集算出「本版本新增代码」的运行时 / 单测覆盖率。分母只算 JaCoCo 有探针的新增行，`total = 0` 时百分比为空而不是 0 或 100；diff 里有而报告里没有的文件列进 `unmatched`，不静默吞掉。
- **历史版本**：结算归档时把新增行附近的源码片段存进 `incremental.json`，历史版本看源码不依赖源码目录仍在；旧格式归档用同目录的 `jacoco.xml` + diff 现算。
- 新增只读接口：`/api/overview`、`/api/services/{name}/detail[?version=]`、`/source`、`/compare?a=&b=`、`/versions`、`/api/projects/{name}/report?days=`。
- 集成脚本同步：`covhub-client.sh` 加 `unit-coverage` / `diff` 子命令，Jenkins 共享库加 `pushUnitCoverage` / `pushDiff`，`Jenkinsfile.build` 加「Push to covhub」阶段。

### 看板（Vue 3）

- `web/` 独立前端（Vite + Vue 3 + TypeScript + Element Plus + ECharts），产物 `covhub/webui/` 随 Python 包分发、由 hub 静态托管，不引任何 CDN；旧的静态 `index.html` 退役。
- 层级 **项目总览 → 项目面板 → 服务详情**，侧栏式布局；建 / 编辑 / 删除项目，勾选即归入 / 移出服务；项目名允许中文。
- 服务详情：四个环形指标（运行时 / 单测 × 总覆盖 / 新增代码）、趋势图、新增代码明细可展开看**源码逐行执行状态**、已结算版本、单测历史、配置。
- **版本下拉切换历史版本**（URL 可收藏转发）；**历史对比**页签：任意两个版本的总量差与按文件的指令覆盖差。
- **手动触发**：立即采集（dump）、结算归档（predeploy）、重出报告，执行日志原样弹出。
- **项目报表**：时间范围 7 / 30 / 90 天 / 全部，各服务横条图 + 服务现状 / 已结算版本 / 单测报告三张表，导出 CSV、**导出 PDF**、打印；服务详情也可导出一份完整 PDF 报告（项目级、服务级各一份）。
- **深色主题**：浅 / 深 / 跟随系统，颜色全部走 token，图表随主题重画；`?theme=light|dark` 可强制。
- 视觉规则：只有两个系列色（总覆盖蓝、新增代码橙，经色盲可分性校验），覆盖率数字不按阈值着色，语义色只表示运维状态（在线 / 离线 / 采集停了 / 断代 / 混版本）。

### 文档与工具

- hub 自带 **Swagger UI**（`/docs`，`swagger-ui-dist` 打在包里，不引 CDN）；`/api/openapi.json` 不再单独暴露，文件版 `docs/openapi.json` 由 `covhub openapi` 导出、`--check` 校验。
- README、ONBOARDING 全面改写到 2.1：接入顺序、路径归属、网络放行、实操记录、Day-2 运维。
- `tools/seed_demo.py`：灌入「商城购物」「充值支付」两个项目、20 个微服务、各 3–5 个版本的完整演示数据；`tools/demo_services.py`：把这 20 个服务起成挂 JaCoCo agent 的真实 JVM，hub 可实时采集。
- 测试套件从零到 262 个用例：exec 协议字节级往返、HTTP 全套、增量计算、迁移守护、前端产物自包含。

### 升级说明

- 需要 Python 3.12：`pip install -e .`（新增依赖 FastAPI、uvicorn、SQLAlchemy、Alembic、pydantic、PyMySQL）。
- 首次启动先 `covhub db upgrade`（`serve` 默认自动做），再 `covhub import` 把旧 `targets.yaml` 的 `services` 与 `data/*/state.json` 导进库；`data/` 目录结构兼容，exec / 报告 / 归档原样可用。
- 配置文件只保留 hub 自身的项（`jacocoAgent` / `jacocoCli` / `dataDir` / `database` / `serve` / `watch` / `collect`），服务用 `covhub service add` 登记。
- 改前端要 `cd web && npm run build` 并连产物一起提交；hub 机器不需要 node。

## v1.2.2（2026-09-06）

结算前做数据体检（exec 与 class 指纹匹配率，只告警不阻断）；补 push 通道的接入文档与时序图。

## v1.2.1（2026-09-06）

看板重做。

## v1.2.0（2026-09-06）

push 通道：自己实现 JaCoCo remote control 协议与 exec 格式，agent 以 `output=tcpclient` 主动连到 hub。

## v1.1.1（2026-09-06）

Jenkins 共享库补 `diagnose`；构建期那条线降级为可选。

## v1.1.0（2026-09-06）

class 由 agent 的 `classdumpdir` 自己交出，版本切段由 hub 自己发现（断代检测），不需要被测项目的构建流水线配合。

## v1.0.0（2026-09-06）

单服务端架构：只部署一个 hub，发版节点只需 curl。
