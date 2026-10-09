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
// tooltip 的显示 / 隐藏自己管（triggerOn: "none"）。轴触发的 tooltip 总是吸到最近的点：空档里
// （服务停了、采集停了）悬停也会把几小时前那个点的数字端出来，像是那会儿还有覆盖率。
// 所以鼠标移动时自己算离最近点的时间差，在正常采集间隔内才 showTip 到那个点，否则收起。
// 不能在 formatter 里判断：ECharts 自己的 mousemove 先于我们挂的监听跑，formatter 看到的是上一次
// 鼠标位置；而 params.axisValue 在 snap 关掉时给的仍是最近点的时间，不是鼠标的时间。
let times: number[] = [];          // 每个快照的时间戳，与 history 对齐
let gapLimit = 0;
let shown = -1;                      // 正显示着 tooltip 的 history 下标
// 空档处往 series 里插一个 null 点把线断开（connectNulls: false），所以 series 的下标和 history 的
// 下标不一样：dataIdx[history 下标] = series 下标，histIdx[series 下标] = history 下标（null 点是 -1）
let dataIdx: number[] = [];
let histIdx: number[] = [];

const KIND: Record<string, string> = { watch: "轮询采集", dump: "手动采集", predeploy: "结算归档", report: "重出报告", seal: "重启封存" };

// 库里的时刻是 hub 本地时间、不带时区的 ISO 串；ECharts 对不带时区的串也按本地时间解析，两边一致
const ts = (iso: string) => iso.replace(" ", "T");

function render() {
  if (!el.value) return;
  chart = chart || echarts.init(el.value);
  const t = chartTokens();
  const h = props.history;
  const multiDay = !!props.from && !!props.to && props.from !== props.to;
  times = h.map((b) => Date.parse(ts(b.at)));
  // 「正常间隔」取相邻采集间隔的中位数（抽稀后会变大，正好跟着放宽），空档阈值是它的 1.5 倍、至少 5 分钟
  const gaps = times.slice(1).map((t, i) => t - times[i]).sort((a, b) => a - b);
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
  gapLimit = Math.max(median * 1.5, 5 * 60e3);
  shown = -1;
  // 采集停过的那段不该画成一条横线（看着像一直有覆盖率）：空档中间补一个 null 让线断开
  const xs: (string | number)[] = [];
  dataIdx = []; histIdx = [];
  for (let i = 0; i < h.length; i++) {
    dataIdx.push(xs.length); histIdx.push(i); xs.push(ts(h[i].at));
    if (i + 1 < h.length && times[i + 1] - times[i] > gapLimit) { histIdx.push(-1); xs.push((times[i] + times[i + 1]) / 2); }
  }
  const col = (pick: (b: Brief) => number | null) => histIdx.map((i, k) => [xs[k], i < 0 ? null : pick(h[i])]);
  chart.setOption({
    animation: false,
    grid: { left: 48, right: 20, top: 16, bottom: multiDay ? 40 : 30 },
    tooltip: {
      trigger: "axis", triggerOn: "none",
      axisPointer: { type: "cross", lineStyle: { color: t.lineStrong }, label: { backgroundColor: t.ink2 } },
      backgroundColor: t.surface, borderColor: t.line, textStyle: { color: t.ink, fontSize: 12 },
      formatter: (params: unknown) => {
        const ps = params as { dataIndex: number; seriesName: string; color: string; value: [string, number | null] }[];
        if (!ps.length) return "";
        const b = h[histIdx[ps[0].dataIndex] ?? -1];
        if (!b) return "";
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
      { name: "总覆盖", type: "line", data: col((b) => b.instruction), color: SERIES.total,
        lineStyle: { width: 2 }, symbol: "circle", symbolSize: 8, showSymbol: h.length === 1, emphasis: { scale: 1.2 }, connectNulls: false },
      { name: "新增代码", type: "line", data: col((b) => b.incremental?.pct ?? null), color: SERIES.inc,
        lineStyle: { width: 2, type: "dashed" }, symbol: "circle", symbolSize: 8, showSymbol: h.length === 1, connectNulls: false },
    ],
  }, true);
}

const onResize = () => chart?.resize();
function hideTip() {
  if (shown === -1) return;
  shown = -1;
  chart?.dispatchAction({ type: "hideTip" });
  // triggerOn: "none" 时十字线不会跟着 hideTip 消失（它靠 ECharts 全局监听的 leave 事件），得自己发一次
  chart?.dispatchAction({ type: "updateAxisPointer", currTrigger: "leave" });
}
function onMouseMove(e: { offsetX: number; offsetY: number }) {
  if (!chart || !times.length || !chart.containPixel("grid", [e.offsetX, e.offsetY])) return hideTip();
  const t = chart.convertFromPixel({ xAxisIndex: 0 }, e.offsetX);
  if (typeof t !== "number" || Number.isNaN(t)) return hideTip();
  let best = -1, bestD = Infinity;
  for (let i = 0; i < times.length; i++) {
    const d = Math.abs(times[i] - t);
    if (d < bestD) { bestD = d; best = i; }
  }
  if (best < 0 || bestD > gapLimit) return hideTip();
  if (best !== shown) { shown = best; chart.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: dataIdx[best] }); }
}
onMounted(() => {
  render();
  chart?.getZr().on("mousemove", onMouseMove);
  chart?.getZr().on("globalout", hideTip);
  window.addEventListener("resize", onResize);
});
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); chart?.dispose(); chart = null; });
watch(() => [props.history, props.from, props.to], render, { deep: true });
watch(isDark, render);
</script>

<template>
  <div ref="el" style="height: 240px; width: 100%"></div>
</template>
