<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { SERIES } from "../ui/colors";
import { chartTokens, isDark } from "../ui/theme";

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

// 报表里的横条图：每个服务两根条（总覆盖 / 新增代码），固定系列色，不按数值着色。
// 没有数据的条留空（null），不画成 0 —— 0% 和「没测」是两回事。
export interface BarRow { name: string; total: number | null; inc: number | null }
const props = defineProps<{ rows: BarRow[] }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const t = chartTokens();
  const names = props.rows.map((r) => r.name);
  chart.setOption({
    animation: false,
    grid: { left: 8, right: 48, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(16,24,40,0.04)" } },
      backgroundColor: t.surface, borderColor: t.line, textStyle: { color: t.ink, fontSize: 12 },
      valueFormatter: (v: unknown) => (typeof v === "number" ? `${v.toFixed(1)}%` : "—"),
    },
    xAxis: { type: "value", min: 0, max: 100, axisLabel: { formatter: "{value}%", color: t.ink3, fontSize: 11 },
             splitLine: { lineStyle: { color: t.grid } } },
    yAxis: { type: "category", data: names, inverse: true, axisLine: { show: false }, axisTick: { show: false },
             axisLabel: { color: t.ink2, fontSize: 12, fontFamily: "Consolas, Menlo, monospace" } },
    series: [
      { name: "总覆盖", type: "bar", data: props.rows.map((r) => r.total), color: SERIES.total, barWidth: 10,
        itemStyle: { borderRadius: [0, 4, 4, 0] }, label: { show: true, position: "right", color: t.ink2, fontSize: 11,
        formatter: (p: { value: unknown }) => (typeof p.value === "number" ? `${p.value.toFixed(1)}%` : "—") } },
      { name: "新增代码", type: "bar", data: props.rows.map((r) => r.inc), color: SERIES.inc, barWidth: 10, barGap: "20%",
        itemStyle: { borderRadius: [0, 4, 4, 0] }, label: { show: true, position: "right", color: t.ink2, fontSize: 11,
        formatter: (p: { value: unknown }) => (typeof p.value === "number" ? `${p.value.toFixed(1)}%` : "—") } },
    ],
  }, true);
}

const onResize = () => chart?.resize();
onMounted(() => { render(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => props.rows, render, { deep: true });
watch(isDark, render);
</script>

<template>
  <div ref="el" :style="{ height: Math.max(120, 28 + rows.length * 44) + 'px', width: '100%' }"></div>
</template>
