<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api, type ArchiveRef, type Compare, type CompareFile } from "../api";
import { SERIES, pct, when } from "../ui/colors";

// 两个版本的覆盖率对比：基准 A → 对比 B。默认 A = 最近一次归档、B = 当前周期，
// 正好回答「这一版比上一版跑到的多了还是少了」。差值用箭头 + 文字色，不按正负标红绿。
const props = defineProps<{ service: string; archives: ArchiveRef[]; onError: (err: unknown) => boolean }>();
const CURRENT = "current";
const a = ref<string>(props.archives[0]?.dir ?? CURRENT);
const b = ref<string>(CURRENT);
const data = ref<Compare | null>(null);
const loading = ref(false);
const onlyChanged = ref(true);

const options = computed(() => [
  { value: CURRENT, label: "当前周期" },
  ...props.archives.map((x) => ({ value: x.dir, label: `${x.version}${x.dir !== x.version ? ` (${x.dir})` : ""} · ${when(x.sealedAt)}` })),
]);

async function load() {
  loading.value = true;
  try {
    data.value = await api.compare(props.service, a.value, b.value);
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
watch([a, b], load, { immediate: true });
function swap() { [a.value, b.value] = [b.value, a.value]; }

const rows = computed<CompareFile[]>(() => {
  const all = data.value?.files ?? [];
  return onlyChanged.value ? all.filter((f) => f.status !== "same") : all;
});
const asFile = (r: unknown) => r as CompareFile;

// 差值文案：+3.2 / −1.5 / ±0；null 表示有一侧没数据
function d(v: number | null | undefined, unit = ""): string {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "±0" + unit;
  return (v > 0 ? "+" : "−") + Math.abs(v) + unit;
}
const arrow = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "▲" : "▼");
const STATUS_TEXT: Record<string, string> = { changed: "有变化", same: "无变化", added: "仅 B 有", removed: "仅 A 有" };

interface Metric { label: string; color: string; a: string; b: string; delta: number | null | undefined; unit: string; sub: string }
const metrics = computed<Metric[]>(() => {
  const c = data.value;
  if (!c) return [];
  const sa = c.a.summary, sb = c.b.summary;
  const inc = (s: typeof sa) => (!s || !s.incremental ? "—" : s.incremental.total === 0 ? "无新增" : pct(s.incremental.pct));
  return [
    { label: "指令覆盖", color: SERIES.total, a: pct(sa?.instruction ?? null), b: pct(sb?.instruction ?? null), delta: c.delta.instruction, unit: " pp",
      sub: `${sa?.covered ?? "—"} / ${sa?.total ?? "—"} → ${sb?.covered ?? "—"} / ${sb?.total ?? "—"}` },
    { label: "分支覆盖", color: SERIES.total, a: pct(sa?.branch ?? null), b: pct(sb?.branch ?? null), delta: c.delta.branch, unit: " pp", sub: "" },
    { label: "触达类", color: SERIES.total, a: sa ? `${sa.classesHit} / ${sa.classesTotal}` : "—", b: sb ? `${sb.classesHit} / ${sb.classesTotal}` : "—",
      delta: c.delta.classesHit, unit: " 个", sub: c.delta.classesTotal ? `类总数 ${d(c.delta.classesTotal)}` : "" },
    { label: "新增代码", color: SERIES.inc, a: inc(sa), b: inc(sb), delta: c.delta.incremental, unit: " pp",
      sub: "各自版本 diff 里新增行的覆盖，两边的分母不同" },
  ];
});
</script>

<template>
  <div class="cmp-bar">
    <span class="muted">基准 A</span>
    <el-select v-model="a" size="small" style="width: 240px">
      <el-option v-for="o in options" :key="o.value" :value="o.value" :label="o.label" />
    </el-select>
    <el-button size="small" text @click="swap" title="交换两侧">⇄</el-button>
    <span class="muted">对比 B</span>
    <el-select v-model="b" size="small" style="width: 240px">
      <el-option v-for="o in options" :key="o.value" :value="o.value" :label="o.label" />
    </el-select>
    <span class="spacer"></span>
    <el-checkbox v-model="onlyChanged" size="small">只看有变化的文件</el-checkbox>
  </div>

  <div v-if="loading && !data" class="muted">加载中…</div>
  <template v-if="data">
    <div v-if="!data.a.hasReport || !data.b.hasReport" class="notice">
      {{ !data.a.hasReport ? "A" : "B" }} 侧没有 jacoco.xml（还没采集过，或归档不完整），按文件的对比只有另一侧的数字。
    </div>
    <div class="cmp-grid">
      <div v-for="m in metrics" :key="m.label" class="cmp-card">
        <div class="label"><i :style="{ background: m.color }"></i>{{ m.label }}</div>
        <div class="pair num">
          <span class="side">{{ m.a }}</span>
          <span class="arrow">→</span>
          <span class="side">{{ m.b }}</span>
        </div>
        <div class="delta num" :class="{ dim: m.delta === null || m.delta === undefined }">{{ arrow(m.delta) }} {{ d(m.delta, m.unit) }}</div>
        <div v-if="m.sub" class="sub">{{ m.sub }}</div>
      </div>
    </div>

    <p class="muted" style="margin: 0 0 8px; font-size: 12px">
      A：{{ data.a.label }}<template v-if="data.a.at">（{{ when(data.a.at) }}）</template> ·
      B：{{ data.b.label }}<template v-if="data.b.at">（{{ when(data.b.at) }}）</template> ·
      {{ data.counts.changed }} 个文件有变化，{{ data.counts.same }} 个无变化<template v-if="data.counts.added">，{{ data.counts.added }} 个仅 B 有</template><template v-if="data.counts.removed">，{{ data.counts.removed }} 个仅 A 有</template>。
      按源码文件的指令覆盖，变化最大的排前面。
    </p>
    <el-table :data="rows" size="small" stripe row-key="path" max-height="560">
      <el-table-column prop="path" label="文件" min-width="300" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.path }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><span class="muted">{{ STATUS_TEXT[asFile(row).status] }}</span></template>
      </el-table-column>
      <el-table-column label="A 指令覆盖" width="150" align="right">
        <template #default="{ row }"><span class="num" v-if="row.a">{{ pct(row.a.pct) }} <span class="muted">({{ row.a.covered }}/{{ row.a.total }})</span></span><span v-else class="muted">—</span></template>
      </el-table-column>
      <el-table-column label="B 指令覆盖" width="150" align="right">
        <template #default="{ row }"><span class="num" v-if="row.b">{{ pct(row.b.pct) }} <span class="muted">({{ row.b.covered }}/{{ row.b.total }})</span></span><span v-else class="muted">—</span></template>
      </el-table-column>
      <el-table-column label="变化" width="100" align="right" prop="delta" sortable>
        <template #default="{ row }"><span class="num">{{ arrow(row.delta) }} {{ d(row.delta, " pp") }}</span></template>
      </el-table-column>
      <el-table-column label="行（A → B）" width="140" align="right">
        <template #default="{ row }"><span class="num muted">{{ row.a ? `${row.a.linesCovered}/${row.a.linesTotal}` : "—" }} → {{ row.b ? `${row.b.linesCovered}/${row.b.linesTotal}` : "—" }}</span></template>
      </el-table-column>
    </el-table>
    <div v-if="!rows.length" class="empty">{{ onlyChanged ? "两个版本按文件的指令覆盖完全一样" : "两侧都没有按文件的数据" }}</div>
  </template>
</template>

<style scoped>
.cmp-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.cmp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-bottom: 14px; }
.cmp-card { border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px 10px; background: var(--surface-2); }
.cmp-card .label { font-size: 12px; color: var(--ink-2); display: flex; align-items: center; gap: 6px; }
.cmp-card .label i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.cmp-card .pair { display: flex; align-items: baseline; gap: 8px; margin: 6px 0 2px; font-size: 20px; font-weight: 650; }
.cmp-card .pair .arrow { color: var(--ink-3); font-size: 14px; font-weight: 400; }
.cmp-card .delta { font-size: 13px; font-weight: 600; }
.cmp-card .delta.dim { color: var(--ink-3); font-weight: 400; }
.cmp-card .sub { font-size: 11px; color: var(--ink-3); margin-top: 2px; }
</style>
