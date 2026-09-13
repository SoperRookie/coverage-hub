<script setup lang="ts">
import type { ServiceRow } from "../api";
import CovCell from "./CovCell.vue";
import StatusTag from "./StatusTag.vue";
import { SERIES, ago } from "../ui/colors";

// 一个项目（或未分组池）下的服务表。列分两组：运行时（总 / 新增）、单测（总 / 新增），
// 数字用文字色、色条用系列色。行上的动作由父页面决定。
defineProps<{ rows: ServiceRow[]; mode: "project" | "unassigned" }>();
const emit = defineEmits<{ (e: "remove", row: ServiceRow): void }>();
const asRow = (r: unknown) => r as ServiceRow;
</script>

<template>
  <el-table :data="rows" size="default" :border="false" :header-cell-style="{ background: 'var(--surface-2)' }" style="width: 100%">
    <el-table-column label="服务" min-width="150">
      <template #default="{ row }">
        <router-link :to="`/services/${encodeURIComponent(row.name)}`" style="font-weight: 600">{{ row.name }}</router-link>
        <div class="muted mono" style="font-size: 11px">{{ row.channel }} · {{ row.endpoint }}</div>
      </template>
    </el-table-column>
    <el-table-column label="状态" min-width="110">
      <template #default="{ row }"><StatusTag :row="asRow(row)" /></template>
    </el-table-column>
    <el-table-column label="版本" width="84">
      <template #default="{ row }"><span class="mono">{{ row.version || "—" }}</span></template>
    </el-table-column>
    <el-table-column label="运行时" align="center">
      <el-table-column label="总覆盖" width="108" align="right">
        <template #default="{ row }"><CovCell :value="row.runtime?.instruction ?? null" :color="SERIES.total" /></template>
      </el-table-column>
      <el-table-column label="新增代码" width="108" align="right">
        <template #default="{ row }">
          <CovCell :value="row.runtime?.incremental ? row.runtime.incremental.pct : null" :color="SERIES.inc"
                   :no-inc="!!row.runtime?.incremental && row.runtime.incremental.total === 0" />
        </template>
      </el-table-column>
    </el-table-column>
    <el-table-column label="单测" align="center">
      <el-table-column label="总覆盖" width="108" align="right">
        <template #default="{ row }"><CovCell :value="row.unit?.instruction ?? null" :color="SERIES.total" /></template>
      </el-table-column>
      <el-table-column label="新增代码" width="108" align="right">
        <template #default="{ row }">
          <CovCell :value="row.unit?.incremental ? row.unit.incremental.pct : null" :color="SERIES.inc"
                   :no-inc="!!row.unit?.incremental && row.unit.incremental.total === 0" />
        </template>
      </el-table-column>
    </el-table-column>
    <el-table-column label="触达类" width="86" align="right">
      <template #default="{ row }"><span class="num">{{ row.runtime ? `${row.runtime.classesHit} / ${row.runtime.classesTotal}` : "—" }}</span></template>
    </el-table-column>
    <el-table-column label="最后采集" width="92">
      <template #default="{ row }"><span class="muted">{{ ago(row.ageSeconds) }}</span></template>
    </el-table-column>
    <el-table-column v-if="mode === 'project'" label="" width="72" align="right">
      <template #default="{ row }"><el-button size="small" text @click="emit('remove', asRow(row))">移出</el-button></template>
    </el-table-column>
  </el-table>
</template>
