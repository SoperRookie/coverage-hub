<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import { api, type Brief, type CommandResult, type Detail } from "../api";
import type { Nav } from "../App.vue";
import CompareView from "../components/CompareView.vue";
import CovCell from "../components/CovCell.vue";
import IncrementalTable from "../components/IncrementalTable.vue";
import Donut from "../components/Donut.vue";
import PageHeader from "../components/PageHeader.vue";
import StatusTag from "../components/StatusTag.vue";
import TrendChart from "../components/TrendChart.vue";
import { SERIES, ago, num, pct, when } from "../ui/colors";

const props = defineProps<{ name: string; onError: (err: unknown) => boolean }>();
const route = useRoute();
const router = useRouter();
const d = ref<Detail | null>(null);
const loading = ref(true);
type Tab = "overview" | "inc" | "versions" | "compare" | "unit" | "config";
const TABS: Tab[] = ["overview", "inc", "versions", "compare", "unit", "config"];
// 页签也进 URL（?tab=compare），对比页能直接贴给别人
const tab = ref<Tab>(TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : "overview");
watch(tab, (t) => router.replace({ query: { ...route.query, tab: t === "overview" ? undefined : t } }));
const incTab = ref<"runtime" | "unit">("runtime");
const nav = inject<Nav>("nav")!;

// 看哪个版本：query 里的 v 是归档目录名（如 1.4.2 / 1.4.2-2），没有就是当前周期。
// 放在 URL 里而不是组件状态里，历史版本的页面才能被收藏、被贴给别人。
const version = computed<string | null>(() => (typeof route.query.v === "string" && route.query.v) || null);
const viewingHistory = computed(() => !!d.value?.viewingVersion);

async function load() {
  loading.value = true;
  try {
    d.value = await api.detail(props.name, version.value);
    nav.project = d.value.project ?? "__unassigned";
  } catch (err) {
    if (!props.onError(err) && version.value) {
      // 归档不存在（被清理了？）就退回当前周期，别卡在一个空页面上
      ElMessage.warning(err instanceof Error ? err.message : String(err));
      router.replace({ query: {} });
    }
  } finally {
    loading.value = false;
  }
}
onMounted(load);
watch(() => [props.name, version.value], load);

// el-select 把空串当「没选」显示占位符，所以「当前周期」用一个哨兵值
const CURRENT = "__current";
function switchVersion(v: string) {
  router.push({ query: v && v !== CURRENT ? { v } : {} });
}

// ---- 手动触发：跑完用例点一下就把这一刻的覆盖率拉下来，不用等下一轮轮询 ----
const running = ref<"" | "dump" | "predeploy" | "report">("");
const result = reactive<{ open: boolean; title: string; res: CommandResult | null }>({ open: false, title: "", res: null });

async function run(kind: "dump" | "predeploy" | "report", fn: () => Promise<CommandResult>, title: string) {
  running.value = kind;
  try {
    const res = await fn();
    result.title = title;
    result.res = res;
    result.open = true;
    if (res.ok) ElMessage.success(`${title}完成`);
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    running.value = "";
  }
}
const dumpNow = () => run("dump", () => api.dump(props.name), "立即采集");
const reportNow = () => run("report", () => api.report(props.name), "重出报告");
async function sealNow() {
  let v: string;
  try {
    const r = await ElMessageBox.prompt(
      "会先拉一次数据并清零计数器，再把本周期的全部 exec 归档、出终版报告。发版前必须做，且要在停服之前。",
      "结算归档", {
        inputValue: d.value?.version ?? "",
        inputPlaceholder: "版本标识，如 1.4.2；留空用配置里的 version",
        confirmButtonText: "结算", cancelButtonText: "取消", type: "warning",
      });
    v = (r.value || "").trim();
  } catch { return; }
  await run("predeploy", () => api.predeploy(props.name, v || undefined), "结算归档");
}
function onMore(c: string) {
  if (c === "seal") sealNow();
  else if (c === "report") reportNow();
  else load();
}

// 四个指标块：运行时 / 单测 × 总 / 新增。新增没有 diff 时 "—"；有 diff 但没可覆盖行 "无新增"。
interface Metric { label: string; value: string; sub: string; color: string; ratio: number | null; dim: boolean }
function metric(label: string, b: Brief | null, kind: "total" | "inc", color: string): Metric {
  if (!b) return { label, value: "—", sub: "还没有数据", color, ratio: null, dim: true };
  if (kind === "total") {
    return { label, value: pct(b.instruction), sub: `指令 ${num(b.covered)} / ${num(b.total)} · 分支 ${pct(b.branch)}`, color, ratio: b.instruction, dim: false };
  }
  const i = b.incremental;
  if (!i) return { label, value: "—", sub: "没有这一版的 diff", color, ratio: null, dim: true };
  if (i.total === 0) return { label, value: "无新增", sub: "diff 里没有可覆盖的新增行", color, ratio: null, dim: true };
  return { label, value: pct(i.pct), sub: `${i.covered} / ${i.total} 行，按 JaCoCo 有探针的行计`, color, ratio: i.pct, dim: false };
}
const metrics = computed<Metric[]>(() => d.value ? [
  metric(viewingHistory.value ? "运行时 · 总覆盖（结算时）" : "运行时 · 总覆盖", d.value.runtime.latest, "total", SERIES.total),
  metric(viewingHistory.value ? "运行时 · 新增代码（结算时）" : "运行时 · 新增代码", d.value.runtime.latest, "inc", SERIES.inc),
  metric("单测 · 总覆盖", d.value.unit.latest, "total", SERIES.total),
  metric("单测 · 新增代码", d.value.unit.latest, "inc", SERIES.inc),
] : []);
const archiveLabel = (a: { version: string; dir: string; sealedAt: string; sealedBy: string }) =>
  `${a.version}${a.dir !== a.version ? ` (${a.dir})` : ""} · ${when(a.sealedAt)}${a.sealedBy !== "predeploy" ? " · 重启封存" : ""}`;
const crumbs = computed(() => [
  { label: "项目总览", to: "/" },
  d.value?.project ? { label: d.value.project, to: `/projects/${encodeURIComponent(d.value.project)}` } : { label: "未分组", to: "/unassigned" },
  { label: props.name },
]);
</script>

<template>
  <PageHeader :title="name" :crumbs="crumbs">
    <template #meta>
      <template v-if="d">
        <StatusTag :row="d" />
        <span>{{ d.channel }} · <span class="mono">{{ d.endpoint }}</span></span>
        <span>版本 <span class="mono">{{ d.version || "—" }}</span></span>
        <span>最后采集 {{ ago(d.ageSeconds) }}</span>
      </template>
    </template>
    <template #actions>
      <el-select v-if="d" :model-value="version ?? CURRENT" size="small" style="width: 250px" @change="switchVersion">
        <el-option :value="CURRENT" label="当前周期" />
        <el-option v-for="a in d.runtime.archives" :key="a.dir" :value="a.dir" :label="archiveLabel(a)" />
      </el-select>
      <a v-if="d?.runtime.reportUrl" :href="d.runtime.reportUrl" target="_blank"><el-button size="small">JaCoCo 报告</el-button></a>
      <el-button size="small" type="primary" :loading="running === 'dump'" :disabled="!!running" @click="dumpNow">立即采集</el-button>
      <el-dropdown trigger="click" @command="onMore">
        <el-button size="small" :disabled="!!running">更多 ▾</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="seal">结算归档（predeploy）</el-dropdown-item>
            <el-dropdown-item command="report">重出报告</el-dropdown-item>
            <el-dropdown-item command="refresh" divided>刷新</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </template>
  </PageHeader>

  <div v-if="loading && !d" class="muted">加载中…</div>
  <template v-if="d">
    <div v-if="d.viewingVersion" class="notice info">
      <span>正在查看历史版本 <b class="mono">{{ d.viewingVersion.version }}</b><span v-if="d.viewingVersion.dir !== d.viewingVersion.version" class="mono"> ({{ d.viewingVersion.dir }})</span>，
        {{ d.viewingVersion.sealedBy === "predeploy" ? "结算于" : "重启封存于" }} {{ when(d.viewingVersion.sealedAt) }}
        <template v-if="d.viewingVersion.execCount">，合并了 {{ d.viewingVersion.execCount }} 份 exec</template>
        <template v-if="d.viewingVersion.matchRate != null">，指纹匹配 {{ pct(d.viewingVersion.matchRate) }}</template>。
        运行时的数字与新增代码明细都是这一版结算时的；趋势图与单测历史不受影响。</span>
      <span class="spacer"></span>
      <a href="#" @click.prevent="switchVersion('')">回到当前周期</a>
    </div>
    <div v-if="d.stale" class="notice">最后一次采集在 {{ ago(d.ageSeconds) }}，采集可能已经停了 —— 确认 hub 的 --with-watch 还在跑。</div>
    <div v-for="b in d.runtime.breaks.slice(-3).reverse()" :key="b.at" class="notice">
      <template v-if="b.sealedAs">{{ when(b.at) }}：检测到未结算的重启，已自动结算为 <b>{{ b.sealedAs }}</b>（{{ b.from }} → {{ b.to }}）。重启前最后一个轮询周期的数据已丢失。</template>
      <template v-else>{{ when(b.at) }}：在线实例跑着两份不同的 class（{{ b.instances }} 个实例），多半是滚动发版正在进行 —— 发版流程里补一次 predeploy。</template>
    </div>

    <div class="kpis" style="margin-bottom: 18px">
      <Donut v-for="m in metrics" :key="m.label" :label="m.label" :value="m.value" :sub="m.sub" :color="m.color" :ratio="m.ratio" :dim="m.dim" />
    </div>

    <el-tabs v-model="tab" class="tabs">
      <el-tab-pane label="概览" name="overview" />
      <el-tab-pane label="新增代码" name="inc" />
      <el-tab-pane label="已结算版本" name="versions" />
      <el-tab-pane label="历史对比" name="compare" />
      <el-tab-pane label="单测覆盖率" name="unit" />
      <el-tab-pane label="配置" name="config" />
    </el-tabs>

    <!-- 概览 -->
    <template v-if="tab === 'overview'">
      <div class="card">
        <div class="card-head">
          <h2>运行时趋势</h2><span class="hint">最近 {{ d.runtime.history.length }} 次采集</span>
          <span class="spacer"></span>
          <span class="legend"><i :style="{ background: SERIES.total }"></i>总覆盖</span>
          <span class="legend"><i class="dash" :style="{ color: SERIES.inc }"></i>新增代码</span>
        </div>
        <div class="card-body">
          <TrendChart v-if="d.runtime.history.length" :history="d.runtime.history" />
          <div v-else class="empty">还没有采集记录</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>数据来源</h2></div>
        <div class="card-body sub" style="font-size: 12px; line-height: 1.8">
          <div>运行时数据来自被测进程的真实执行；单测数据来自构建流水线传上来的 jacoco.xml。</div>
          <div>「新增代码」指本版本 git diff 里新增的行，分母只算 JaCoCo 有探针的行（空行、注释、import 不计）。</div>
          <div v-if="d.diff">当前 diff：版本 <span class="mono">{{ d.diff.version }}</span>，基线 <span class="mono">{{ d.diff.base }}</span>，{{ d.diff.files }} 个源码文件 {{ d.diff.addedLines }} 行新增，{{ when(d.diff.at) }} 收到。</div>
          <div v-else>还没有收到这个服务的 git diff —— 构建流水线里加一步 <code>covhub-client.sh diff</code>。</div>
        </div>
      </div>
    </template>

    <!-- 新增代码 -->
    <div v-if="tab === 'inc'" class="card">
      <div class="card-head">
        <h2>新增代码明细</h2>
        <el-radio-group v-model="incTab" size="small">
          <el-radio-button value="runtime">运行时</el-radio-button>
          <el-radio-button value="unit">单测</el-radio-button>
        </el-radio-group>
        <span class="hint">点开一行看源码与逐行执行状态</span>
      </div>
      <div class="card-body">
        <IncrementalTable v-if="incTab === 'runtime'" :view="d.runtime.incremental" :service="name" kind="runtime" :version="version" :on-error="onError"
                          :empty="viewingHistory ? '这一版归档里没有新增代码的覆盖明细（结算时还没有它的 diff）。' : '当前周期没有新增代码的覆盖明细（没有这一版的 diff，或还没采集过）。'" />
        <IncrementalTable v-else :view="d.unit.incremental" :service="name" kind="unit" :version="version" :on-error="onError"
                          empty="没有单测的新增代码明细（还没收到这一版的单测报告或 diff）。" />
      </div>
    </div>

    <!-- 已结算版本 -->
    <div v-if="tab === 'versions'" class="card">
      <div class="card-head"><h2>已结算版本</h2><span class="hint">predeploy 结算的归档；同名归档退让成 -2 时链接指向真实目录</span></div>
      <div class="card-body flush">
        <el-table v-if="d.runtime.versions.length" :data="[...d.runtime.versions].reverse()" size="default">
          <el-table-column label="版本" width="150"><template #default="{ row }"><span class="mono">{{ row.version }}</span><span v-if="row.dir !== row.version" class="muted"> ({{ row.dir }})</span></template></el-table-column>
          <el-table-column label="结算时间" width="170"><template #default="{ row }">{{ when(row.sealedAt) }}</template></el-table-column>
          <el-table-column label="总覆盖" width="140" align="right"><template #default="{ row }"><CovCell :value="row.instruction" :color="SERIES.total" /></template></el-table-column>
          <el-table-column label="新增代码" width="140" align="right"><template #default="{ row }"><CovCell :value="row.incremental ? row.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.incremental && row.incremental.total === 0" /></template></el-table-column>
          <el-table-column label="触达类" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
          <el-table-column label="指纹匹配" width="100" align="right"><template #default="{ row }"><span class="num">{{ pct(row.matchRate) }}</span></template></el-table-column>
          <el-table-column label="报告"><template #default="{ row }"><a href="#" @click.prevent="switchVersion(row.dir)">在看板里看</a> · <a :href="row.reportUrl" target="_blank">HTML</a> · <a :href="row.xmlUrl" target="_blank">jacoco.xml</a></template></el-table-column>
        </el-table>
        <div v-else class="empty">还没有结算过的版本</div>
      </div>
    </div>

    <!-- 历史对比 -->
    <div v-if="tab === 'compare'" class="card">
      <div class="card-head"><h2>历史对比</h2><span class="hint">任意两个版本（当前周期或已归档）之间总量与按文件的指令覆盖差</span></div>
      <div class="card-body">
        <CompareView :key="name" :service="name" :archives="d.runtime.archives" :on-error="onError" />
      </div>
    </div>

    <!-- 单测 -->
    <div v-if="tab === 'unit'" class="card">
      <div class="card-head"><h2>单测覆盖率</h2><span class="hint">来自构建流水线，最近 {{ d.unit.history.length }} 个版本</span>
        <span class="spacer"></span><a v-if="d.unit.xmlUrl" :href="d.unit.xmlUrl" target="_blank" style="font-size: 12px">最新 jacoco.xml</a></div>
      <div class="card-body flush">
        <el-table v-if="d.unit.history.length" :data="[...d.unit.history].reverse()" size="default">
          <el-table-column label="版本" width="150"><template #default="{ row }"><span class="mono">{{ row.version }}</span></template></el-table-column>
          <el-table-column label="收到时间" width="170"><template #default="{ row }">{{ when(row.at) }}</template></el-table-column>
          <el-table-column label="指令" width="140" align="right"><template #default="{ row }"><CovCell :value="row.instruction" :color="SERIES.total" /></template></el-table-column>
          <el-table-column label="分支" width="90" align="right"><template #default="{ row }"><span class="num">{{ pct(row.branch) }}</span></template></el-table-column>
          <el-table-column label="行" width="90" align="right"><template #default="{ row }"><span class="num">{{ pct(row.line) }}</span></template></el-table-column>
          <el-table-column label="新增代码" width="140" align="right"><template #default="{ row }"><CovCell :value="row.incremental ? row.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.incremental && row.incremental.total === 0" /></template></el-table-column>
          <el-table-column label="类" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
        </el-table>
        <div v-else class="empty"><b>还没有收到单测报告</b>构建流水线里加一步 <code>covhub-client.sh unit-coverage</code>。</div>
      </div>
    </div>

    <!-- 配置 -->
    <div v-if="tab === 'config'" class="card">
      <div class="card-head"><h2>采集配置</h2><span class="hint">改配置用 <code>covhub service update</code></span></div>
      <div class="card-body">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="includes"><span class="mono">{{ (d.config.includes as string[]).join("  ") || "—" }}</span></el-descriptions-item>
          <el-descriptions-item label="excludes"><span class="mono">{{ (d.config.excludes as string[]).join("  ") || "—" }}</span></el-descriptions-item>
          <el-descriptions-item label="reportExcludes"><span class="mono">{{ (d.config.reportExcludes as string[]).join("  ") || "—" }}</span></el-descriptions-item>
          <el-descriptions-item label="classfiles"><span class="mono">{{ (d.config.classfiles as string[]).join("  ") || "—" }}</span></el-descriptions-item>
          <el-descriptions-item label="sourcefiles"><span class="mono">{{ (d.config.sourcefiles as string[]).join("  ") || "—" }}</span></el-descriptions-item>
          <el-descriptions-item label="classDumpDir"><span class="mono">{{ d.config.classDumpDir || "—" }}</span></el-descriptions-item>
        </el-descriptions>
        <p v-if="d.channel === 'push' && d.runtime.instances.length" class="muted" style="margin-bottom: 0">
          在线实例：<span v-for="i in d.runtime.instances" :key="i.peer" class="mono" style="margin-right: 10px">{{ i.peer }}（{{ when(i.since) }} 连入）</span>
        </p>
        <p class="muted" style="margin: 10px 0 0; font-size: 12px">
          报告：<a :href="d.runtime.xmlUrl" target="_blank">当前周期 jacoco.xml</a>
          <template v-if="d.runtime.reportUrl"> · <a :href="d.runtime.reportUrl" target="_blank">JaCoCo 原生报告</a></template>
        </p>
      </div>
    </div>
  </template>

  <!-- 手动触发的结果：hub 把这次执行的日志原样回来，失败原因也在里面（409 时 ok=false） -->
  <el-dialog v-model="result.open" :title="result.title" width="640px">
    <template v-if="result.res">
      <el-alert :type="result.res.ok ? 'success' : 'error'" :closable="false" show-icon style="margin-bottom: 10px"
                :title="result.res.ok ? `${result.title}成功${result.res.latest ? `，当前指令覆盖 ${pct(result.res.latest.instruction)}` : ''}` : `${result.title}失败，原因见日志`" />
      <pre class="log">{{ result.res.log || "（没有日志输出）" }}</pre>
    </template>
    <template #footer><el-button @click="result.open = false">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.tabs { margin-bottom: 4px; }
.log { margin: 0; max-height: 360px; overflow: auto; background: var(--surface-2); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
</style>
