<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import "element-plus/es/components/message/style/css";
import { api, type ProjectReport, type ReportService, type StatusFields } from "../api";
import type { Nav } from "../App.vue";
import CovCell from "../components/CovCell.vue";
import Kpi from "../components/Kpi.vue";
import PageHeader from "../components/PageHeader.vue";
import ServiceBars from "../components/ServiceBars.vue";
import StatusTag from "../components/StatusTag.vue";
import { SERIES, ago, pct, when } from "../ui/colors";
import { exportPdf } from "../ui/pdf";

// 项目报表：一页看完项目下所有服务的现状，以及某段时间内结算过的版本和收到的单测报告。
// 不算项目平均覆盖率 —— 各服务的百分比平均起来只会误导，这里给的是逐服务、逐版本的原始数字。
// 时间范围放在 URL 的 query 里（?days=30），链接贴出去别人看到的是同一份。
const props = defineProps<{ name: string; onError: (err: unknown) => boolean }>();
const route = useRoute();
const router = useRouter();
const nav = inject<Nav>("nav")!;
watch(() => props.name, (n) => { nav.project = n; }, { immediate: true });

const DAYS = [{ v: 7, l: "近 7 天" }, { v: 30, l: "近 30 天" }, { v: 90, l: "近 90 天" }, { v: 0, l: "全部" }];
const days = computed(() => {
  const q = Number(route.query.days);
  return DAYS.some((d) => d.v === q) ? q : 30;
});
const rep = ref<ProjectReport | null>(null);
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    rep.value = await api.projectReport(props.name, days.value);
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
watch(() => [props.name, days.value], load);
const setDays = (v: number) => router.replace({ query: { ...route.query, days: String(v) } });

const isPool = computed(() => props.name === "__unassigned");
const title = computed(() => rep.value?.title || props.name);
const rangeText = computed(() => (days.value ? `${when(rep.value?.since ?? null)} 至今` : "全部历史"));

const bars = computed(() => (rep.value?.services ?? []).map((s) => ({
  name: s.name,
  total: s.runtime?.instruction ?? null,
  inc: s.runtime?.incremental && s.runtime.incremental.total > 0 ? s.runtime.incremental.pct : null,
})));
const sealed = computed(() => (rep.value?.services ?? []).flatMap((s) => s.versions.map((v) => ({ ...v, service: s.name })))
  .sort((a, b) => (a.sealedAt < b.sealedAt ? 1 : -1)));
const units = computed(() => (rep.value?.services ?? []).flatMap((s) => s.unitReports.map((u) => ({ ...u, service: s.name })))
  .sort((a, b) => (a.at < b.at ? 1 : -1)));
const asStatus = (r: unknown) => {
  const s = r as ReportService;
  return { ...s, project: null, endpoint: "", onlineAt: null, instances: null, pushMixed: false, diff: null, hasReport: false } as StatusFields;
};

// ---- 导出：CSV 给 Excel（带 BOM，中文不乱码），打印走浏览器（另存 PDF）----
function csvEscape(v: unknown): string {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function download(name: string, text: string) {
  const blob = new Blob(["\uFEFF" + text], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
function exportCsv() {
  const r = rep.value;
  if (!r) return;
  const rows: unknown[][] = [];
  rows.push(["项目", r.title, "范围", rangeText.value, "生成时间", r.generatedAt]);
  rows.push([]);
  rows.push(["服务现状"]);
  rows.push(["服务", "通道", "版本", "在线", "运行时总覆盖%", "运行时新增覆盖%", "新增覆盖行", "新增可覆盖行", "单测总覆盖%", "单测新增覆盖%", "触达类", "类总数", "最后采集"]);
  for (const s of r.services) {
    rows.push([s.name, s.channel, s.version ?? "", s.unknown ? "未知" : s.online ? "在线" : "离线",
               s.runtime?.instruction ?? "", s.runtime?.incremental?.pct ?? "", s.runtime?.incremental?.covered ?? "",
               s.runtime?.incremental?.total ?? "", s.unit?.instruction ?? "", s.unit?.incremental?.pct ?? "",
               s.runtime?.classesHit ?? "", s.runtime?.classesTotal ?? "", s.runtime?.at ?? ""]);
  }
  rows.push([]);
  rows.push(["已结算版本"]);
  rows.push(["服务", "版本", "归档目录", "结算时间", "总覆盖%", "指令覆盖", "指令总数", "新增覆盖%", "新增覆盖行", "新增可覆盖行", "触达类", "类总数", "指纹匹配%"]);
  for (const v of sealed.value) {
    rows.push([v.service, v.version, v.dir, v.sealedAt, v.instruction, v.covered, v.total,
               v.incremental?.pct ?? "", v.incremental?.covered ?? "", v.incremental?.total ?? "",
               v.classesHit, v.classesTotal, v.matchRate ?? ""]);
  }
  rows.push([]);
  rows.push(["单测报告"]);
  rows.push(["服务", "版本", "收到时间", "指令%", "分支%", "行%", "新增覆盖%", "新增覆盖行", "新增可覆盖行", "类", "类总数"]);
  for (const u of units.value) {
    rows.push([u.service, u.version, u.at, u.instruction, u.branch, u.line ?? "", u.incremental?.pct ?? "",
               u.incremental?.covered ?? "", u.incremental?.total ?? "", u.classesHit, u.classesTotal]);
  }
  download(`covhub-${r.project}-${days.value ? days.value + "d" : "all"}.csv`, rows.map((row) => row.map(csvEscape).join(",")).join("\r\n"));
  ElMessage.success("已导出 CSV");
}
const print = () => window.print();
const root = ref<HTMLElement | null>(null);
const exporting = ref(false);
async function exportPdfFile() {
  if (!root.value || !rep.value) return;
  exporting.value = true;
  try {
    await exportPdf(root.value, `covhub-${rep.value.project}-${days.value ? days.value + "d" : "all"}.pdf`);
    ElMessage.success("已导出 PDF");
  } catch (err) {
    ElMessage.error("导出失败：" + (err instanceof Error ? err.message : String(err)));
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <div ref="root">
  <PageHeader :title="`${title} · 报表`"
              :crumbs="[{ label: '项目总览', to: '/' }, { label: isPool ? '未分组' : title, to: isPool ? '/unassigned' : `/projects/${encodeURIComponent(name)}` }, { label: '报表' }]">
    <template #meta>
      <span v-if="rep">范围 {{ rangeText }}</span>
      <span v-if="rep">生成于 {{ when(rep.generatedAt) }}</span>
    </template>
    <template #actions>
      <el-radio-group :model-value="days" size="small" class="no-print" @change="(v: string | number | boolean | undefined) => setDays(Number(v))">
        <el-radio-button v-for="d in DAYS" :key="d.v" :value="d.v">{{ d.l }}</el-radio-button>
      </el-radio-group>
      <el-button size="small" class="no-print" :disabled="!rep" @click="exportCsv">导出 CSV</el-button>
      <el-button size="small" type="primary" plain class="no-print" :disabled="!rep" :loading="exporting" @click="exportPdfFile">导出 PDF</el-button>
      <el-button size="small" class="no-print" :disabled="!rep" @click="print">打印</el-button>
    </template>
  </PageHeader>

  <div v-if="loading && !rep" class="muted">加载中…</div>
  <template v-if="rep">
    <div class="kpis" style="margin-bottom: 18px">
      <Kpi label="服务" :value="String(rep.counts.services)" :sub="`在线 ${rep.counts.online} · 离线 ${rep.counts.offline}${rep.counts.unknown ? ' · 未知 ' + rep.counts.unknown : ''}`" />
      <Kpi label="期间结算版本" :value="String(sealed.length)" :sub="`${rep.services.filter((s) => s.versions.length).length} 个服务发过版`" :dim="!sealed.length" />
      <Kpi label="期间单测报告" :value="String(units.length)" :sub="`${rep.services.filter((s) => s.unitReports.length).length} 个服务有构建上报`" :dim="!units.length" />
      <Kpi label="需关注" :value="String(rep.counts.attention)" sub="采集停了 / 有断代 / 混版本" :dim="!rep.counts.attention" />
    </div>

    <div class="card">
      <div class="card-head">
        <h2>运行时覆盖率 · 各服务现状</h2>
        <span class="spacer"></span>
        <span class="legend"><i :style="{ background: SERIES.total }"></i>总覆盖</span>
        <span class="legend"><i :style="{ background: SERIES.inc }"></i>本版本新增代码</span>
      </div>
      <div class="card-body">
        <ServiceBars v-if="bars.length" :rows="bars" />
        <div v-else class="empty">这个项目下还没有服务</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>服务现状</h2><span class="hint">每个服务当前周期最近一次采集与最近一份单测报告</span></div>
      <div class="card-body flush">
        <el-table :data="rep.services" size="default" :border="false">
          <el-table-column label="服务" min-width="150">
            <template #default="{ row }"><router-link :to="`/services/${encodeURIComponent(row.name)}`" style="font-weight: 600">{{ row.name }}</router-link>
              <div class="muted mono" style="font-size: 11px">{{ row.channel }}</div></template>
          </el-table-column>
          <el-table-column label="状态" min-width="110"><template #default="{ row }"><StatusTag :row="asStatus(row)" /></template></el-table-column>
          <el-table-column label="版本" width="90"><template #default="{ row }"><span class="mono">{{ row.version || "—" }}</span></template></el-table-column>
          <el-table-column label="运行时" align="center">
            <el-table-column label="总覆盖" width="108" align="right"><template #default="{ row }"><CovCell :value="row.runtime?.instruction ?? null" :color="SERIES.total" /></template></el-table-column>
            <el-table-column label="新增代码" width="108" align="right">
              <template #default="{ row }"><CovCell :value="row.runtime?.incremental ? row.runtime.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.runtime?.incremental && row.runtime.incremental.total === 0" /></template>
            </el-table-column>
          </el-table-column>
          <el-table-column label="单测" align="center">
            <el-table-column label="总覆盖" width="108" align="right"><template #default="{ row }"><CovCell :value="row.unit?.instruction ?? null" :color="SERIES.total" /></template></el-table-column>
            <el-table-column label="新增代码" width="108" align="right">
              <template #default="{ row }"><CovCell :value="row.unit?.incremental ? row.unit.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.unit?.incremental && row.unit.incremental.total === 0" /></template>
            </el-table-column>
          </el-table-column>
          <el-table-column label="期间结算" width="80" align="right"><template #default="{ row }"><span class="num">{{ row.versions.length }}</span></template></el-table-column>
          <el-table-column label="触达类" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.runtime ? `${row.runtime.classesHit} / ${row.runtime.classesTotal}` : "—" }}</span></template></el-table-column>
          <el-table-column label="最后采集" width="92"><template #default="{ row }"><span class="muted">{{ ago(row.ageSeconds) }}</span></template></el-table-column>
        </el-table>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>期间已结算版本</h2><span class="hint">predeploy 结算的归档，按结算时间倒序；数字是结算那一刻的</span></div>
      <div class="card-body flush">
        <el-table v-if="sealed.length" :data="sealed" size="default" :border="false">
          <el-table-column label="服务" min-width="130"><template #default="{ row }"><router-link :to="{ path: `/services/${encodeURIComponent(row.service)}`, query: { v: row.dir } }">{{ row.service }}</router-link></template></el-table-column>
          <el-table-column label="版本" width="150"><template #default="{ row }"><span class="mono">{{ row.version }}</span><span v-if="row.dir !== row.version" class="muted"> ({{ row.dir }})</span></template></el-table-column>
          <el-table-column label="结算时间" width="160"><template #default="{ row }">{{ when(row.sealedAt) }}</template></el-table-column>
          <el-table-column label="总覆盖" width="130" align="right"><template #default="{ row }"><CovCell :value="row.instruction" :color="SERIES.total" /></template></el-table-column>
          <el-table-column label="新增代码" width="130" align="right"><template #default="{ row }"><CovCell :value="row.incremental ? row.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.incremental && row.incremental.total === 0" /></template></el-table-column>
          <el-table-column label="新增行" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.incremental ? `${row.incremental.covered} / ${row.incremental.total}` : "—" }}</span></template></el-table-column>
          <el-table-column label="触达类" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
          <el-table-column label="指纹匹配" width="90" align="right"><template #default="{ row }"><span class="num">{{ pct(row.matchRate) }}</span></template></el-table-column>
          <el-table-column label="报告" width="150" class-name="no-print"><template #default="{ row }"><a :href="row.reportUrl" target="_blank">HTML</a> · <a :href="row.xmlUrl" target="_blank">jacoco.xml</a></template></el-table-column>
        </el-table>
        <div v-else class="empty">{{ rangeText }}内没有结算过版本</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>期间单测报告</h2><span class="hint">构建流水线上报的 jacoco.xml，按收到时间倒序</span></div>
      <div class="card-body flush">
        <el-table v-if="units.length" :data="units" size="default" :border="false">
          <el-table-column label="服务" min-width="130"><template #default="{ row }"><router-link :to="`/services/${encodeURIComponent(row.service)}`">{{ row.service }}</router-link></template></el-table-column>
          <el-table-column label="版本" width="150"><template #default="{ row }"><span class="mono">{{ row.version }}</span></template></el-table-column>
          <el-table-column label="收到时间" width="160"><template #default="{ row }">{{ when(row.at) }}</template></el-table-column>
          <el-table-column label="指令" width="130" align="right"><template #default="{ row }"><CovCell :value="row.instruction" :color="SERIES.total" /></template></el-table-column>
          <el-table-column label="分支" width="80" align="right"><template #default="{ row }"><span class="num">{{ pct(row.branch) }}</span></template></el-table-column>
          <el-table-column label="行" width="80" align="right"><template #default="{ row }"><span class="num">{{ pct(row.line) }}</span></template></el-table-column>
          <el-table-column label="新增代码" width="130" align="right"><template #default="{ row }"><CovCell :value="row.incremental ? row.incremental.pct : null" :color="SERIES.inc" :no-inc="!!row.incremental && row.incremental.total === 0" /></template></el-table-column>
          <el-table-column label="新增行" width="100" align="right"><template #default="{ row }"><span class="num">{{ row.incremental ? `${row.incremental.covered} / ${row.incremental.total}` : "—" }}</span></template></el-table-column>
          <el-table-column label="类" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.classesHit }} / {{ row.classesTotal }}</span></template></el-table-column>
        </el-table>
        <div v-else class="empty">{{ rangeText }}内没有收到单测报告</div>
      </div>
    </div>

    <p class="muted" style="font-size: 12px">
      口径：总覆盖为 JaCoCo 指令覆盖率；新增代码覆盖率的分母只算 diff 新增行里 JaCoCo 有探针的行（空行、注释、import 不计），
      没有该版本 diff 时显示「—」。报表不给项目平均值 —— 各服务代码量差异很大，平均数没有意义。
    </p>
  </template>
  </div>
</template>
