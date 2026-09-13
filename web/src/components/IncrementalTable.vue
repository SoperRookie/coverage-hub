<script setup lang="ts">
import type { IncView } from "../api";
import SourceView from "./SourceView.vue";
import { pct } from "../ui/colors";

// 新增代码按文件的覆盖明细。分母是 diff 新增行里 JaCoCo 有探针的行；unmatched 是
// diff 里有、报告里没有的源码文件 —— 中性提示，不是红色：多半是被 excludes 排掉了，
// 但也可能是配置错了，得让人看见。
// version 是归档目录名：看历史版本时传下去，源码从那个归档里存的片段取。
defineProps<{ view: IncView | null; empty: string; service: string; kind: "runtime" | "unit"; version?: string | null; onError: (err: unknown) => boolean }>();

function missedText(nums: number[]): string {
  if (!nums.length) return "";
  // 连续行号折叠成 a-b
  const parts: string[] = [];
  let start = nums[0], prev = nums[0];
  for (const n of nums.slice(1).concat([NaN])) {
    if (n === prev + 1) { prev = n; continue; }
    parts.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = prev = n;
  }
  return parts.join(", ");
}
</script>

<template>
  <div v-if="!view" class="muted">{{ empty }}</div>
  <div v-else>
    <p class="muted" style="margin: 0 0 8px">
      版本 {{ view.version }}：新增行里可覆盖 {{ view.total }} 行，覆盖 {{ view.covered }} 行，
      <b class="num">{{ pct(view.pct) }}</b>
      <span v-if="view.skipped"> · 忽略测试代码 {{ view.skipped }} 个文件</span>
    </p>
    <el-alert v-if="view.unmatched.length" type="info" :closable="false" show-icon style="margin-bottom: 8px"
              :title="`${view.unmatched.length} 个新增的源码文件在报告里找不到（被 excludes 排掉，或不在 classfiles 里）`">
      <div class="mono">{{ view.unmatched.join("　") }}</div>
    </el-alert>
    <el-alert v-if="view.ambiguous.length" type="info" :closable="false" show-icon style="margin-bottom: 8px"
              :title="`${view.ambiguous.length} 个报告文件对应了多条 diff 路径（多模块同名），行号已合并`" />
    <p class="muted" style="margin: 0 0 6px; font-size: 12px">点开一行看新增代码的源码与逐行覆盖状态。</p>
    <el-table :data="view.files" size="small" stripe :default-sort="{ prop: 'pct', order: 'ascending' }" row-key="path">
      <el-table-column type="expand" width="40">
        <template #default="{ row }">
          <div style="padding: 0 8px 0 40px">
            <SourceView :service="service" :kind="kind" :file="row.path" :version="version" :on-error="onError" />
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="path" label="文件" min-width="320" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.path }}</span><span v-if="row.group" class="muted"> · {{ row.group }}</span></template>
      </el-table-column>
      <el-table-column prop="pct" label="新增覆盖" width="100" sortable align="right">
        <template #default="{ row }"><span class="num">{{ pct(row.pct) }}</span></template>
      </el-table-column>
      <el-table-column label="覆盖 / 可覆盖" width="120" align="right">
        <template #default="{ row }"><span class="num">{{ row.covered }} / {{ row.total }}</span></template>
      </el-table-column>
      <el-table-column label="未覆盖行" min-width="200">
        <template #default="{ row }"><span class="mono">{{ missedText(row.missed) }}</span></template>
      </el-table-column>
    </el-table>
  </div>
</template>
