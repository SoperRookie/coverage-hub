<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { PieChart } from "echarts/charts";
import { CanvasRenderer } from "echarts/renderers";

import { chartTokens, isDark } from "../ui/theme";

echarts.use([PieChart, CanvasRenderer]);

// 环形图指标块：一个百分比 = 已覆盖那一弧（系列色）+ 剩余那一弧（浅灰）。
// 数字仍用文字色写在环中间；没有数据（ratio 为 null）时整环浅灰、中间显示占位文字，不画成 0%。
const props = defineProps<{ label: string; value: string; sub?: string; color: string; ratio: number | null; dim?: boolean }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const rest = chartTokens().line;
  const r = props.ratio === null ? null : Math.max(0, Math.min(100, props.ratio));
  chart.setOption({
    animation: false,
    series: [{
      type: "pie", radius: ["72%", "92%"], center: ["50%", "50%"],
      silent: true, label: { show: false }, labelLine: { show: false },
      startAngle: 90, clockwise: true, padAngle: r === null || r === 0 || r === 100 ? 0 : 2,
      itemStyle: { borderRadius: 3 },
      data: r === null
        ? [{ value: 1, itemStyle: { color: rest } }]
        : [{ value: r, itemStyle: { color: props.color } }, { value: 100 - r, itemStyle: { color: rest } }],
    }],
  }, true);
}

const onResize = () => chart?.resize();
onMounted(() => { render(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => [props.ratio, props.color], render);
watch(isDark, render);
</script>

<template>
  <div class="kpi donut">
    <div class="label"><i :style="{ background: color }"></i>{{ label }}</div>
    <div class="ring">
      <div ref="el" class="chart"></div>
      <div class="center num" :class="{ dim }">{{ value }}</div>
    </div>
    <div v-if="sub" class="sub">{{ sub }}</div>
  </div>
</template>

<style scoped>
.donut { display: flex; flex-direction: column; align-items: center; text-align: center; }
.donut .label { align-self: flex-start; }
.ring { position: relative; width: 132px; height: 132px; margin: 8px 0 2px; }
.ring .chart { position: absolute; inset: 0; }
.ring .center { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: 650; letter-spacing: -.3px; }
.ring .center.dim { color: var(--ink-3); font-size: 18px; }
</style>
