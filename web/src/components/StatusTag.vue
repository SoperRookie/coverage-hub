<script setup lang="ts">
import { computed } from "vue";
import type { StatusFields } from "../api";
import { STATUS, type StatusKind } from "../ui/colors";

const props = defineProps<{ row: StatusFields }>();

// 语义色只给运维状态。多个状态同时成立时全部显示，顺序按严重程度。
const kinds = computed<StatusKind[]>(() => {
  const out: StatusKind[] = [];
  if (props.row.unknown) out.push("unknown");
  else out.push(props.row.online ? "online" : "offline");
  if (props.row.stale) out.push("stale");
  if (props.row.pushMixed) out.push("mixed");
  if (props.row.breaks > 0) out.push("break");
  return out;
});
</script>

<template>
  <span>
    <el-tag v-for="k in kinds" :key="k" :type="STATUS[k].type" size="small" effect="light" style="margin-right: 4px">
      {{ STATUS[k].label }}<template v-if="k === 'online' && row.channel === 'push' && row.instances !== null">（{{ row.instances }} 实例）</template>
    </el-tag>
  </span>
</template>
