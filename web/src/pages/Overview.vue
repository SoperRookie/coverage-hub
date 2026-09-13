<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { api, type Overview, type ServiceRow } from "../api";
import StatusTag from "../components/StatusTag.vue";
import { METRIC_COLORS, ago, pct } from "../ui/colors";

const props = defineProps<{ onError: (err: unknown) => boolean }>();
const data = ref<Overview | null>(null);
const loading = ref(true);
let timer: number | undefined;

async function load() {
  try {
    data.value = await api.overview();
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
onMounted(() => { load(); timer = window.setInterval(load, 60_000); });
onBeforeUnmount(() => window.clearInterval(timer));

function groups(o: Overview) {
  const out = o.projects.map((p) => ({ key: p.name, title: p.title || p.name, sub: p.name, rows: p.services, counts: p.counts }));
  if (o.unassigned.length) out.push({ key: "__unassigned", title: "未分组", sub: "没有归属项目的服务", rows: o.unassigned, counts: null as any });
  return out;
}

function incText(r: ServiceRow, which: "runtime" | "unit") {
  const b = r[which];
  if (!b) return "—";
  if (!b.incremental) return "—";
  return b.incremental.total === 0 ? "无新增" : pct(b.incremental.pct);
}
</script>

<template>
  <div class="topbar">
    <h1>覆盖率看板</h1>
    <span v-if="data" class="sub">{{ data.counts.services }} 个服务 · 在线 {{ data.counts.online }} · 离线 {{ data.counts.offline }}
      <template v-if="data.counts.attention"> · 需关注 {{ data.counts.attention }}</template>
      · 更新于 {{ data.generatedAt.replace("T", " ") }}</span>
    <span class="spacer"></span>
    <el-button size="small" @click="load">刷新</el-button>
  </div>

  <div class="card" style="padding-bottom: 6px">
    <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeTotal }"></i>运行时 · 总</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeInc }"></i>运行时 · 新增代码</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.unitTotal }"></i>单测 · 总</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.unitInc }"></i>单测 · 新增代码</span>
    <span class="legend muted">覆盖率数字不按阈值着色；颜色只表示运维状态</span>
  </div>

  <div v-if="loading" class="muted">加载中…</div>
  <div v-else-if="data && !data.counts.services" class="card muted">
    还没有登记任何服务。用 <code>covhub service add &lt;name&gt; …</code> 或 <code>covhub import</code> 登记。
  </div>

  <div v-for="g in data ? groups(data) : []" :key="g.key" class="card">
    <h2>
      {{ g.title }}
      <span class="hint">{{ g.sub }}
        <template v-if="g.counts"> · {{ g.counts.services }} 个服务，离线 {{ g.counts.offline }}<template v-if="g.counts.stale">，采集停了 {{ g.counts.stale }}</template></template>
      </span>
    </h2>
    <el-table :data="g.rows" size="small" stripe>
      <el-table-column label="服务" min-width="180">
        <template #default="{ row }">
          <router-link class="plain" :to="`/services/${encodeURIComponent(row.name)}`">{{ row.name }}</router-link>
          <div class="muted mono">{{ row.channel }} · {{ row.endpoint }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="200">
        <template #default="{ row }"><StatusTag :row="row" /></template>
      </el-table-column>
      <el-table-column label="版本" width="110">
        <template #default="{ row }"><span class="mono">{{ row.version || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="运行时 总" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeTotal }">{{ row.runtime ? pct(row.runtime.instruction) : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="运行时 新增" width="110" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeInc }">{{ incText(row, "runtime") }}</span></template>
      </el-table-column>
      <el-table-column label="单测 总" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitTotal }">{{ row.unit ? pct(row.unit.instruction) : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="单测 新增" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitInc }">{{ incText(row, "unit") }}</span></template>
      </el-table-column>
      <el-table-column label="触达类" width="100" align="right">
        <template #default="{ row }"><span class="num">{{ row.runtime ? `${row.runtime.classesHit} / ${row.runtime.classesTotal}` : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="最后采集" width="110">
        <template #default="{ row }"><span class="muted">{{ ago(row.ageSeconds) }}</span></template>
      </el-table-column>
    </el-table>
  </div>
</template>
