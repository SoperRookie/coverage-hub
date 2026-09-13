<script setup lang="ts">
import type { ServiceRow } from "../api";
import StatusTag from "./StatusTag.vue";
import { METRIC_COLORS, ago, pct } from "../ui/colors";

// 一个项目（或未分组池）下的服务表。行上的动作由父页面决定：
//   project   模式：可把服务移出项目
//   unassigned模式：没有行动作（归入项目在项目面板的「添加服务」里做）
defineProps<{ rows: ServiceRow[]; mode: "project" | "unassigned" }>();
const emit = defineEmits<{ (e: "remove", row: ServiceRow): void }>();

const asRow = (r: unknown) => r as ServiceRow;

function incText(r: ServiceRow, which: "runtime" | "unit") {
  const b = r[which];
  if (!b || !b.incremental) return "—";
  return b.incremental.total === 0 ? "无新增" : pct(b.incremental.pct);
}
</script>

<template>
  <el-table :data="rows" size="small" stripe>
    <el-table-column label="服务" min-width="180">
      <template #default="{ row }">
        <router-link class="plain" :to="`/services/${encodeURIComponent(row.name)}`">{{ row.name }}</router-link>
        <div class="muted mono">{{ row.channel }} · {{ row.endpoint }}</div>
      </template>
    </el-table-column>
    <el-table-column label="状态" width="200">
      <template #default="{ row }"><StatusTag :row="asRow(row)" /></template>
    </el-table-column>
    <el-table-column label="版本" width="100">
      <template #default="{ row }"><span class="mono">{{ row.version || "—" }}</span></template>
    </el-table-column>
    <el-table-column label="运行时 总" width="100" align="right">
      <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeTotal }">{{ row.runtime ? pct(row.runtime.instruction) : "—" }}</span></template>
    </el-table-column>
    <el-table-column label="运行时 新增" width="110" align="right">
      <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeInc }">{{ incText(asRow(row), "runtime") }}</span></template>
    </el-table-column>
    <el-table-column label="单测 总" width="100" align="right">
      <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitTotal }">{{ row.unit ? pct(row.unit.instruction) : "—" }}</span></template>
    </el-table-column>
    <el-table-column label="单测 新增" width="100" align="right">
      <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitInc }">{{ incText(asRow(row), "unit") }}</span></template>
    </el-table-column>
    <el-table-column label="触达类" width="100" align="right">
      <template #default="{ row }"><span class="num">{{ row.runtime ? `${row.runtime.classesHit} / ${row.runtime.classesTotal}` : "—" }}</span></template>
    </el-table-column>
    <el-table-column label="最后采集" width="100">
      <template #default="{ row }"><span class="muted">{{ ago(row.ageSeconds) }}</span></template>
    </el-table-column>
    <el-table-column v-if="mode === 'project'" label="" width="80" align="right">
      <template #default="{ row }">
        <el-button size="small" text @click="emit('remove', asRow(row))">移出</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>
