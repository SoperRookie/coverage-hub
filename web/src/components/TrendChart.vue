<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { Brief } from "../api";
import { SERIES } from "../ui/colors";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

// 两条线：总覆盖（蓝、实线）、新增代码（橙、虚线），每条固定色，不用 visualMap（那是按值着色）。
// 新增覆盖在 diff 到达前是 null，折线断开（connectNulls: false）—— 不回填历史，快照描述的是当时。
// 网格线、坐标轴退后（浅灰细线），数据线 2px，标记点 ≥ 8px 只在悬停时显示；十字线 + tooltip。
const props = defineProps<{ history: Brief[] }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const xs = props.history.map((h) => h.at.replace("T", " "));
  chart.setOption({
    animation: false,
    grid: { left: 48, right: 20, top: 16, bottom: 30 },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross", lineStyle: { color: "#c9ccd1" }, label: { backgroundColor: "#4b5059" } },
      backgroundColor: "#fff", borderColor: "#e4e6ea", textStyle: { color: "#111318", fontSize: 12 },
      valueFormatter: (v: unknown) => (v === null || v === undefined ? "—" : `${v}%`),
    },
    legend: { show: false },   // 图例在卡片头上，这里不重复
    xAxis: { type: "category", data: xs, axisLine: { lineStyle: { color: "#e4e6ea" } }, axisTick: { show: false },
             axisLabel: { fontSize: 10, color: "#7a8089" } },
    yAxis: { type: "value", min: 0, max: 100, axisLabel: { formatter: "{value}%", color: "#7a8089", fontSize: 11 },
             splitLine: { lineStyle: { color: "#eef0f2" } } },
    series: [
      { name: "总覆盖", type: "line", data: props.history.map((h) => h.instruction), color: SERIES.total,
        lineStyle: { width: 2 }, symbol: "circle", symbolSize: 8, showSymbol: false, emphasis: { scale: 1.2 } },
      { name: "新增代码", type: "line", data: props.history.map((h) => h.incremental?.pct ?? null), color: SERIES.inc,
        lineStyle: { width: 2, type: "dashed" }, symbol: "circle", symbolSize: 8, showSymbol: false, connectNulls: false },
    ],
  }, true);
}

const onResize = () => chart?.resize();
onMounted(() => { render(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => props.history, render, { deep: true });
</script>

<template>
  <div ref="el" style="height: 240px; width: 100%"></div>
</template>
