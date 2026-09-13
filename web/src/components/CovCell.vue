<script setup lang="ts">
import { pct } from "../ui/colors";
// 表格里的覆盖率：数字（文字色）+ 细条（系列色）。value 为 null 显示「—」，noInc 显示「无新增」。
defineProps<{ value: number | null | undefined; color: string; noInc?: boolean }>();
</script>

<template>
  <span class="cov" :class="{ empty: value === null || value === undefined }">
    <span class="n">{{ noInc ? "无新增" : pct(value) }}</span>
    <span class="t"><i :style="{ width: (value ?? 0) + '%', background: color }"></i></span>
  </span>
</template>
