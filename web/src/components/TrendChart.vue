<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { Brief } from "../api";
import { METRIC_COLORS } from "../ui/colors";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

// 两条线各一个固定色，不用 visualMap（那是按值着色）。新增覆盖在 diff 到达前是 null，
// 折线断开（connectNulls: false）—— 不回填历史，快照描述的是当时。
const props = defineProps<{ history: Brief[]; totalLabel: string; incLabel: string; totalColor: string; incColor: string }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const xs = props.history.map((h) => h.at.replace("T", " "));
  chart.setOption({
    animation: false,
    grid: { left: 44, right: 16, top: 30, bottom: 28 },
    tooltip: { trigger: "axis", valueFormatter: (v: unknown) => (v === null || v === undefined ? "—" : `${v}%`) },
    legend: { top: 0, left: 0 },
    xAxis: { type: "category", data: xs, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", min: 0, max: 100, axisLabel: { formatter: "{value}%" } },
    series: [
      { name: props.totalLabel, type: "line", data: props.history.map((h) => h.instruction), color: props.totalColor, showSymbol: false, smooth: false },
      { name: props.incLabel, type: "line", data: props.history.map((h) => h.incremental?.pct ?? null), color: props.incColor, showSymbol: true, symbolSize: 5, connectNulls: false, lineStyle: { type: "dashed" } },
    ],
  }, true);
}

const onResize = () => chart?.resize();
onMounted(() => { render(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => props.history, render, { deep: true });
defineExpose({ METRIC_COLORS });
</script>

<template>
  <div ref="el" style="height: 220px; width: 100%"></div>
</template>
