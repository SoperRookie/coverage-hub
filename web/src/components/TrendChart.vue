<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { Brief } from "../api";
import { SERIES, pct } from "../ui/colors";
import { chartTokens, isDark } from "../ui/theme";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

// 两条线：总覆盖（蓝、实线）、新增代码（橙、虚线），每条固定色，不用 visualMap（那是按值着色）。
// 新增覆盖在 diff 到达前是 null，折线断开（connectNulls: false）—— 不回填历史，快照描述的是当时。
// 横轴是**时间轴**不是类目轴：按天 / 按区间看时，采集停过的那段要在图上留出空白，
// 等距排开会把三小时的停摆画成和五分钟一样宽。from / to（YYYY-MM-DD）给了就把横轴撑满
// 整段日期 —— 一天只采了两次也看得出是上午还是下午。
// 网格线、坐标轴退后（浅灰细线），数据线 2px，标记点 ≥ 8px 只在悬停时显示；十字线 + tooltip。
const props = defineProps<{ history: Brief[]; from?: string; to?: string }>();
const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

const KIND: Record<string, string> = { watch: "轮询采集", dump: "手动采集", predeploy: "结算归档", report: "重出报告", seal: "重启封存" };

// 库里的时刻是 hub 本地时间、不带时区的 ISO 串；ECharts 对不带时区的串也按本地时间解析，两边一致
const ts = (iso: string) => iso.replace(" ", "T");

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const t = chartTokens();
  const h = props.history;
  const multiDay = !!props.from && !!props.to && props.from !== props.to;
  chart.setOption({
    animation: false,
    grid: { left: 48, right: 20, top: 16, bottom: multiDay ? 40 : 30 },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross", lineStyle: { color: t.lineStrong }, label: { backgroundColor: t.ink2 } },
      backgroundColor: t.surface, borderColor: t.line, textStyle: { color: t.ink, fontSize: 12 },
      formatter: (params: unknown) => {
        const ps = params as { dataIndex: number; seriesName: string; color: string; value: [string, number | null] }[];
        if (!ps.length) return "";
        const b = h[ps[0].dataIndex];
        const head = `${b.at.replace("T", " ")}${b.version ? ` · 版本 ${b.version}` : ""}${b.kind && KIND[b.kind] ? ` · ${KIND[b.kind]}` : ""}`;
        const lines = ps.map((p) => `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${p.color};margin-right:6px"></span>${p.seriesName}　<b>${pct(p.value[1])}</b>`);
        return [head, ...lines].join("<br/>");
      },
    },
    legend: { show: false },   // 图例在卡片头上，这里不重复
    xAxis: {
      type: "time",
      min: props.from ? ts(`${props.from}T00:00:00`) : undefined,
      max: props.to ? ts(`${props.to}T23:59:59`) : undefined,
      axisLine: { lineStyle: { color: t.line } }, axisTick: { show: false },
      axisLabel: { fontSize: 10, color: t.ink3, hideOverlap: true,
                   formatter: multiDay ? { year: "{yyyy}-{MM}-{dd}", month: "{MM}-{dd}", day: "{MM}-{dd}", hour: "{HH}:{mm}", minute: "{HH}:{mm}", second: "{HH}:{mm}:{ss}", millisecond: "{HH}:{mm}" }
                                       : "{HH}:{mm}" },
      splitLine: { show: multiDay, lineStyle: { color: t.grid } },
    },
    yAxis: { type: "value", min: 0, max: 100, axisLabel: { formatter: "{value}%", color: t.ink3, fontSize: 11 },
             splitLine: { lineStyle: { color: t.grid } } },
    series: [
      { name: "总覆盖", type: "line", data: h.map((b) => [ts(b.at), b.instruction]), color: SERIES.total,
        lineStyle: { width: 2 }, symbol: "circle", symbolSize: 8, showSymbol: h.length === 1, emphasis: { scale: 1.2 } },
      { name: "新增代码", type: "line", data: h.map((b) => [ts(b.at), b.incremental?.pct ?? null]), color: SERIES.inc,
        lineStyle: { width: 2, type: "dashed" }, symbol: "circle", symbolSize: 8, showSymbol: h.length === 1, connectNulls: false },
    ],
  }, true);
}

const onResize = () => chart?.resize();
onMounted(() => { render(); window.addEventListener("resize", onResize); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => [props.history, props.from, props.to], render, { deep: true });
watch(isDark, render);
</script>

<template>
  <div ref="el" style="height: 240px; width: 100%"></div>
</template>
