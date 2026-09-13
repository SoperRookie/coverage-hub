<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import { api, type Brief, type Detail } from "../api";
import IncrementalTable from "../components/IncrementalTable.vue";
import StatusTag from "../components/StatusTag.vue";
import TrendChart from "../components/TrendChart.vue";
import type { Nav } from "../App.vue";
import { METRIC_COLORS, ago, pct, when } from "../ui/colors";

const props = defineProps<{ name: string; onError: (err: unknown) => boolean }>();
const d = ref<Detail | null>(null);
const loading = ref(true);
const incTab = ref<"runtime" | "unit">("runtime");
const nav = inject<Nav>("nav")!;

async function load() {
  loading.value = true;
  try {
    d.value = await api.detail(props.name);
    nav.project = d.value.project ?? "__unassigned";
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
watch(() => props.name, load);

// 四个大数字：总 / 新增 各一对。新增没有 diff 时是 null → "—"；有 diff 但没可覆盖行 → "无新增"
interface Metric { label: string; value: string; denom: string; color: string; ratio: number | null }
function metric(label: string, b: Brief | null, kind: "total" | "inc", color: string): Metric {
  if (!b) return { label, value: "—", denom: "还没有数据", color, ratio: null };
  if (kind === "total") {
    return { label, value: pct(b.instruction), denom: `指令 ${b.covered.toLocaleString()} / ${b.total.toLocaleString()} · 分支 ${pct(b.branch)}`, color, ratio: b.instruction };
  }
  const i = b.incremental;
  if (!i) return { label, value: "—", denom: "没有这一版的 diff", color, ratio: null };
  if (i.total === 0) return { label, value: "无新增", denom: "diff 里没有可覆盖的新增行", color, ratio: null };
  return { label, value: pct(i.pct), denom: `${i.covered} / ${i.total} 行（按 JaCoCo 有探针的行计）`, color, ratio: i.pct };
}
const metrics = computed<Metric[]>(() => d.value ? [
  metric("运行时 · 总", d.value.runtime.latest, "total", METRIC_COLORS.runtimeTotal),
  metric("运行时 · 新增代码", d.value.runtime.latest, "inc", METRIC_COLORS.runtimeInc),
  metric("单测 · 总", d.value.unit.latest, "total", METRIC_COLORS.unitTotal),
  metric("单测 · 新增代码", d.value.unit.latest, "inc", METRIC_COLORS.unitInc),
] : []);
</script>

<template>
  <div class="topbar">
    <router-link class="plain" :to="d?.project ? `/projects/${encodeURIComponent(d.project)}` : '/'">← {{ d?.project || "项目" }}</router-link>
    <h1>{{ name }}</h1>
    <span v-if="d" class="sub">
      <template v-if="d.project">项目 {{ d.project }} · </template>{{ d.channel }} · {{ d.endpoint }} · 版本 <span class="mono">{{ d.version || "—" }}</span>
      · 最后采集 {{ ago(d.ageSeconds) }}
    </span>
    <span class="spacer"></span>
    <StatusTag v-if="d" :row="d" />
    <el-button size="small" @click="load">刷新</el-button>
  </div>

  <div v-if="loading && !d" class="muted">加载中…</div>
  <template v-if="d">
    <div v-if="d.stale" class="alert">最后一次采集在 {{ ago(d.ageSeconds) }}，采集可能已经停了 —— 确认 hub 的 --with-watch 还在跑。</div>
    <div v-for="b in d.runtime.breaks.slice(-3).reverse()" :key="b.at" class="alert">
      <template v-if="b.sealedAs">{{ when(b.at) }}：检测到未结算的重启，已自动结算为 <b>{{ b.sealedAs }}</b>（{{ b.from }} → {{ b.to }}）。重启前最后一个轮询周期的数据已丢失。</template>
      <template v-else>{{ when(b.at) }}：在线实例跑着两份不同的 class（{{ b.instances }} 个实例），多半是滚动发版正在进行 —— 发版流程里补一次 predeploy。</template>
    </div>

    <div class="card">
      <div class="grid">
        <div v-for="m in metrics" :key="m.label" class="metric">
          <div class="label">{{ m.label }}</div>
          <div class="value num" :style="{ color: m.color }">{{ m.value }}</div>
          <div class="denom">{{ m.denom }}</div>
          <div class="bar"><i :style="{ width: (m.ratio ?? 0) + '%', background: m.color }"></i></div>
        </div>
      </div>
      <p class="muted" style="margin: 10px 0 0; font-size: 12px">
        运行时数据来自被测进程的真实执行；单测数据来自构建流水线传上来的 jacoco.xml。
        「新增代码」指本版本 git diff 里新增的行，分母只算 JaCoCo 有探针的行（空行、注释、import 不计）。
        <template v-if="d.diff">当前 diff：版本 {{ d.diff.version }}，基线 <span class="mono">{{ d.diff.base }}</span>，{{ d.diff.files }} 个源码文件 {{ d.diff.addedLines }} 行新增。</template>
        <template v-else>还没有收到这个服务的 git diff —— 流水线里加一步 <code>covhub-client.sh diff</code>。</template>
      </p>
    </div>

    <div class="card">
      <h2>运行时趋势<span class="hint">最近 {{ d.runtime.history.length }} 次采集</span></h2>
      <TrendChart v-if="d.runtime.history.length" :history="d.runtime.history" total-label="运行时 · 总" inc-label="运行时 · 新增代码"
                  :total-color="METRIC_COLORS.runtimeTotal" :inc-color="METRIC_COLORS.runtimeInc" />
      <div v-else class="muted">还没有采集记录。</div>
    </div>

    <div class="card">
      <h2>新增代码明细
        <span class="hint">
          <el-radio-group v-model="incTab" size="small">
            <el-radio-button value="runtime">运行时</el-radio-button>
            <el-radio-button value="unit">单测</el-radio-button>
          </el-radio-group>
        </span>
      </h2>
      <IncrementalTable v-if="incTab === 'runtime'" :view="d.runtime.incremental" empty="当前周期没有新增代码的覆盖明细（没有这一版的 diff，或还没采集过）。" />
      <IncrementalTable v-else :view="d.unit.incremental" empty="没有单测的新增代码明细（还没收到这一版的单测报告或 diff）。" />
    </div>

    <div class="card">
      <h2>已结算版本<span class="hint">predeploy 结算的归档；同名归档退让成 -2 时链接指向真实目录</span></h2>
      <el-table v-if="d.runtime.versions.length" :data="[...d.runtime.versions].reverse()" size="small" stripe>
        <el-table-column label="版本" width="140"><template #default="{ row }"><span class="mono">{{ row.version }}</span><span v-if="row.dir !== row.version" class="muted"> ({{ row.dir }})</span></template></el-table-column>
        <el-table-column label="结算时间" width="160"><template #default="{ row }">{{ when(row.sealedAt) }}</template></el-table-column>
        <el-table-column label="运行时 总" width="100" align="right"><template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeTotal }">{{ pct(row.instruction) }}</span></template></el-table-column>
        <el-table-column label="运行时 新增" width="110" align="right"><template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeInc }">{{ row.incremental ? (row.incremental.total ? pct(row.incremental.pct) : "无新增") : "—" }}</span></template></el-table-column>
        <el-table-column label="触达类" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
        <el-table-column label="指纹匹配" width="100" align="right"><template #default="{ row }"><span class="num">{{ pct(row.matchRate) }}</span></template></el-table-column>
        <el-table-column label="报告"><template #default="{ row }"><a class="plain" :href="row.reportUrl" target="_blank">HTML</a> · <a class="plain" :href="row.xmlUrl" target="_blank">jacoco.xml</a></template></el-table-column>
      </el-table>
      <div v-else class="muted">还没有结算过的版本。</div>
    </div>

    <div class="card">
      <h2>单测覆盖率<span class="hint">来自构建流水线，最近 {{ d.unit.history.length }} 个版本</span></h2>
      <el-table v-if="d.unit.history.length" :data="[...d.unit.history].reverse()" size="small" stripe>
        <el-table-column label="版本" width="140"><template #default="{ row }"><span class="mono">{{ row.version }}</span></template></el-table-column>
        <el-table-column label="收到时间" width="160"><template #default="{ row }">{{ when(row.at) }}</template></el-table-column>
        <el-table-column label="指令" width="90" align="right"><template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitTotal }">{{ pct(row.instruction) }}</span></template></el-table-column>
        <el-table-column label="分支" width="90" align="right"><template #default="{ row }"><span class="num">{{ pct(row.branch) }}</span></template></el-table-column>
        <el-table-column label="行" width="90" align="right"><template #default="{ row }"><span class="num">{{ pct(row.line) }}</span></template></el-table-column>
        <el-table-column label="新增代码" width="100" align="right"><template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitInc }">{{ row.incremental ? (row.incremental.total ? pct(row.incremental.pct) : "无新增") : "—" }}</span></template></el-table-column>
        <el-table-column label="类" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
      </el-table>
      <div v-else class="muted">还没有收到单测报告 —— 构建流水线里加一步 <code>covhub-client.sh unit-coverage</code>。</div>
    </div>

    <div class="card">
      <h2>报告与配置</h2>
      <p>
        <a v-if="d.runtime.reportUrl" class="plain" :href="d.runtime.reportUrl" target="_blank">当前周期的 JaCoCo 原生报告</a>
        <span v-else class="muted">当前周期还没有报告</span>
        · <a class="plain" :href="d.runtime.xmlUrl" target="_blank">jacoco.xml</a>
        <template v-if="d.unit.xmlUrl"> · <a class="plain" :href="d.unit.xmlUrl" target="_blank">单测 jacoco.xml</a></template>
      </p>
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="includes"><span class="mono">{{ (d.config.includes as string[]).join(" ") || "—" }}</span></el-descriptions-item>
        <el-descriptions-item label="excludes"><span class="mono">{{ (d.config.excludes as string[]).join(" ") || "—" }}</span></el-descriptions-item>
        <el-descriptions-item label="reportExcludes"><span class="mono">{{ (d.config.reportExcludes as string[]).join(" ") || "—" }}</span></el-descriptions-item>
        <el-descriptions-item label="classfiles"><span class="mono">{{ (d.config.classfiles as string[]).join(" ") || "—" }}</span></el-descriptions-item>
        <el-descriptions-item label="classDumpDir"><span class="mono">{{ d.config.classDumpDir || "—" }}</span></el-descriptions-item>
      </el-descriptions>
      <p v-if="d.channel === 'push' && d.runtime.instances.length" class="muted" style="margin-bottom: 0">
        在线实例：<span v-for="i in d.runtime.instances" :key="i.peer" class="mono" style="margin-right: 10px">{{ i.peer }}（{{ when(i.since) }} 连入）</span>
      </p>
    </div>
  </template>
</template>
