# 更新日志

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
