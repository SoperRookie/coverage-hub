<script setup lang="ts">
// 指标块：数字用文字色，系列色只在标签前的小方块和底部细条上。
defineProps<{ label: string; value: string; sub?: string; color?: string; ratio?: number | null; dim?: boolean }>();
</script>

<template>
  <div class="kpi">
    <div class="label"><i v-if="color" :style="{ background: color }"></i>{{ label }}</div>
    <div class="value num" :class="{ dim }">{{ value }}</div>
    <div v-if="sub" class="sub">{{ sub }}</div>
    <div v-if="color" class="bar"><i :style="{ width: Math.max(0, Math.min(100, ratio ?? 0)) + '%', background: color }"></i></div>
  </div>
</template>
