# 架构图

covhub **v2.1.0 当前实现**的 PlantUML 图，八张图各回答一个问题。图不是示意，是照着代码画的。

| 文件 | 回答的问题 | 图类型 |
|---|---|---|
| `01-部署拓扑.puml` | 谁连谁、数据往哪个方向流；构建 / 发版 / 浏览器 / 被测端各自怎么碰到 hub | 部署图 |
| `02-一次采集.puml` | 一次采集内部发生了什么：探活、dump、断代检测、出报告、算新增覆盖、入库 | 时序图 |
| `03-发版流程.puml` | 发版六个阶段各自动了哪些数据，为什么 predeploy 必须在停服之前 | 时序图 |
| `04-周期状态.puml` | 一个采集周期怎么流转、什么时候数据会作废、归档里有什么 | 状态图 |
| `05-push通道.puml` | push 通道怎么认领连接、怎么向多副本取数、混版本怎么发现 | 时序图 |
| `06-构建期数据.puml` | 单测 jacoco.xml 与 git diff 怎么进来，「新增代码覆盖率」按什么口径算 | 时序图 |
| `07-数据模型.puml` | 八张表各存什么、和 dataDir 下的目录怎么对应 | 类图 |
| `08-看板导航.puml` | 看板的页面层级、每页背后的接口、历史版本 / 对比 / 报表从哪进 | 组件图 |

看图顺序建议 01 → 02 → 03 → 04：先看方向，再看单次采集，再看跨机器的发版全程，最后看周期与失效条件。
06 是接构建流水线时看的；07 改表结构前看；08 改前端前看；05 只在用到 push 通道时才需要。

## 与代码的对应关系

改这些地方时对应的图要跟着更新：

| 代码 | 影响的图 |
|---|---|
| `agent.agent_opts()` / `reachable()` / `jacoco.do_dump()` | 01、02 |
| `watch.watch_once()` / `cycle.snapshot()` / `make_report()` / `cycle.record()` | 02 |
| `collector.PushCollector` / `exec_format.remote_dump()` | 01、05 |
| `cycle.check_data_health()` / `diagnose` | 03 |
| `ops.predeploy()` / `artifacts.store_classes()` / `pack_classes()` / `ops.retarget()` | 03、04 |
| `build.store_diff()` / `store_unit_report()` / `recompute()` / `incremental.compute()` | 06 |
| `db/models.py` / Alembic 迁移 / `layout.ensure_dirs()` | 07 |
| `views.py` / `api/routes_view.py` / `web/src/router.ts` 与各页面 | 08 |
| `api/routes_*.py` 的路由 | 01、03、06 |
| `integration/covhub-client.sh`、`Jenkinsfile.deploy` / `Jenkinsfile.build` 的阶段划分 | 03、06 |

## 渲染

**IntelliJ IDEA**：装 PlantUML Integration 插件，打开 `.puml` 右侧即预览。

**命令行**（文件名是中文，记得带 `-charset UTF-8`）：

```bash
# 需要 plantuml.jar（从 https://plantuml.com/download 取）
java -Dfile.encoding=UTF-8 -jar plantuml.jar -charset UTF-8 -tsvg docs/diagrams/*.puml     # 出 SVG
java -Dfile.encoding=UTF-8 -jar plantuml.jar -charset UTF-8 -tpng docs/diagrams/*.puml     # 出 PNG
java -Dfile.encoding=UTF-8 -jar plantuml.jar -charset UTF-8 -checkonly docs/diagrams/*.puml # 只查语法
```

部署图、状态图、类图默认要 Graphviz（`dot`）。没装的话加 `-Playout=smetana` 用内置布局引擎：

```bash
java -Dfile.encoding=UTF-8 -jar plantuml.jar -charset UTF-8 -Playout=smetana -tpng docs/diagrams/*.puml
```

（本目录八张图都用 PlantUML 1.2025.4 + smetana 渲染验证过。）

**中文显示成方框**时，在图里加一行指定字体：

```
skinparam defaultFontName "Microsoft YaHei"
```

## 编辑时的两个坑

**① `--` 是删除线语法。** PlantUML 的 creole 里 `--文字--` 会渲染成删除线，所以 `dump --address --port` 会变成一串带删除线的字。命令行参数一律用 `""` 包成等宽原文：

```
S -> CLI : dump ""--address --port""
```

**② 时序图的参与者类型是另一套。** 部署图里能用的 `node`、`folder` 在时序图里不合法，时序图只认 `participant` / `actor` / `database` / `entity` / `boundary` / `control` / `collections` / `queue`。写错不会报错，会被当成语法错误图。
