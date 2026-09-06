# 数据流图

covhub **当前实现**的 PlantUML 图（跟随 v1.2 起的架构）。五张图各回答一个问题。

| 文件 | 回答的问题 | 图类型 |
|---|---|---|
| `01-topology.puml` | 谁连谁、数据往哪个方向流 | 部署/组件图 |
| `02-collect.puml` | 一次采集内部发生了什么 | 时序图 |
| `03-release.puml` | 发版六个阶段各自动了哪些数据 | 时序图 |
| `04-lifecycle.puml` | 一个采集周期怎么流转、什么时候数据会作废 | 状态图 |
| `05-push.puml` | push 通道怎么认领连接、怎么向多副本取数 | 时序图 |

看图的顺序建议 01 → 02 → 03 → 04：先看方向，再看单次采集，再看跨机器的发版全程，最后看周期与失效条件。05 只在用到 push 通道时才需要看。

## 与代码的对应关系

图不是示意，是照着代码画的。改这些地方时对应的图要跟着更新：

| 代码 | 影响的图 |
|---|---|
| `agent_opts()` / `reachable()` / `do_dump()` | 01、02 |
| `_snapshot()` / `make_report()` / `record()` | 02 |
| `PushCollector` / `remote_dump()` / `write_exec_file()` | 01、05 |
| `check_data_health()` / `diagnose()` | 03 |
| `dashboard_rows()` / `build_dashboard_html()` | 看板，图里没画 |
| `cmd_predeploy()` / `store_classes()` / `pack_classes()` / `cmd_retarget()` | 03、04 |
| `api_dispatch()` 的路由表 | 01、03 |
| `integration/covhub-client.sh`、`Jenkinsfile.deploy` 的阶段划分 | 03 |

## 渲染

**IntelliJ IDEA**：装 PlantUML Integration 插件，打开 `.puml` 右侧即预览。

**命令行**：

```bash
# 需要 plantuml.jar（从 https://plantuml.com/download 取）
java -jar plantuml.jar -tsvg docs/diagrams/*.puml     # 出 SVG
java -jar plantuml.jar -tpng docs/diagrams/*.puml     # 出 PNG
java -jar plantuml.jar -checkonly docs/diagrams/*.puml # 只查语法
```

组件图和状态图默认要 Graphviz（`dot`）。没装的话加 `-Playout=smetana` 用内置布局引擎：

```bash
java -jar plantuml.jar -Playout=smetana -tpng docs/diagrams/*.puml
```

（本目录五张图都用 smetana 渲染验证过。）

**中文显示成方框**时，在图里加一行指定字体：

```
skinparam defaultFontName "Microsoft YaHei"
```

## 编辑时的两个坑

**① `--` 是删除线语法。** PlantUML 的 creole 里 `--文字--` 会渲染成删除线，所以 `dump --address --port` 会变成一串带删除线的字。命令行参数一律用 `""` 包成等宽原文：

```
S -> CLI : dump ""--address --port""
```

**② 时序图的参与者类型是另一套。** 组件图里能用的 `file`、`cloud` 在时序图里不合法，时序图只认 `participant` / `actor` / `database` / `entity` / `boundary` / `control` / `collections` / `queue`。写错不会报错，会被当成语法错误图。
