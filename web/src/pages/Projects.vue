<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import { api, type Overview, type Project, type ServiceRow } from "../api";
import type { Nav } from "../App.vue";
import Kpi from "../components/Kpi.vue";
import PageHeader from "../components/PageHeader.vue";
import { SERIES, STATUS, ago, pct } from "../ui/colors";

// 首页：项目总览。项目是最上层的东西，服务在项目里面；点卡片进项目面板。
const props = defineProps<{ onError: (err: unknown) => boolean }>();
const nav = inject<Nav>("nav")!;
nav.project = "";
const data = ref<Overview | null>(null);
const projects = ref<Project[]>([]);
const loading = ref(true);
let timer: number | undefined;

async function load() {
  try {
    const [o, p] = await Promise.all([api.overview(), api.projects()]);
    data.value = o;
    projects.value = p.projects;
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
onMounted(() => { load(); timer = window.setInterval(load, 60_000); });
onBeforeUnmount(() => window.clearInterval(timer));

interface Card {
  name: string; title: string; description: string | null; rows: ServiceRow[];
  online: number; offline: number; unknown: number; attention: number; lastAge: number | null;
}
const cards = computed<Card[]>(() => {
  const byName = new Map((data.value?.projects ?? []).map((p) => [p.name, p]));
  return projects.value.map((p) => {
    const rows = byName.get(p.name)?.services ?? [];
    const ages = rows.map((r) => r.ageSeconds).filter((a): a is number => a !== null);
    return {
      name: p.name, title: p.title || p.name, description: p.description, rows,
      online: rows.filter((r) => r.online).length,
      offline: rows.filter((r) => r.online === false).length,
      unknown: rows.filter((r) => r.unknown).length,
      attention: rows.filter((r) => r.stale || r.breaks || r.pushMixed).length,
      lastAge: ages.length ? Math.min(...ages) : null,
    };
  });
});
</script>

<template>
  <PageHeader title="项目总览">
    <template #meta>
      <span v-if="data">{{ projects.length }} 个项目 · {{ data.counts.services }} 个服务 · 更新于 {{ data.generatedAt.replace("T", " ") }}</span>
    </template>
    <template #actions><el-button size="small" @click="load">刷新</el-button></template>
  </PageHeader>

  <div v-if="data" class="kpis" style="margin-bottom: 18px">
    <Kpi label="项目" :value="String(projects.length)" />
    <Kpi label="服务" :value="String(data.counts.services)" :sub="`在线 ${data.counts.online} · 离线 ${data.counts.offline}${data.counts.unknown ? ' · 未知 ' + data.counts.unknown : ''}`" />
    <Kpi label="需关注" :value="String(data.counts.attention)" sub="采集停了 / 有断代 / 混版本" :dim="!data.counts.attention" />
    <Kpi label="未分组服务" :value="String(data.unassigned.length)" sub="已登记但没归入项目" :dim="!data.unassigned.length" />
  </div>

  <div v-if="loading" class="muted">加载中…</div>
  <div v-else-if="!projects.length" class="card"><div class="empty"><b>还没有项目</b>先用左侧「新建项目」，再把服务加进去（服务用 <code>covhub service add</code> 登记）。</div></div>

  <div class="cards">
    <router-link v-for="c in cards" :key="c.name" class="pcard" :to="`/projects/${encodeURIComponent(c.name)}`">
      <div class="pcard-head">
        <div>
          <div class="pcard-title">{{ c.title }}</div>
          <div class="muted mono" style="font-size: 11px">{{ c.name }}<template v-if="c.description"> · <span style="font-family: inherit">{{ c.description }}</span></template></div>
        </div>
        <div class="pcard-status">
          <span v-if="c.online" class="status"><i :style="{ background: STATUS.online.color }"></i>{{ c.online }} 在线</span>
          <span v-if="c.offline" class="status"><i :style="{ background: STATUS.offline.color }"></i>{{ c.offline }} 离线</span>
          <span v-if="c.unknown" class="status"><i :style="{ background: STATUS.unknown.color }"></i>{{ c.unknown }} 未知</span>
          <span v-if="c.attention" class="status"><i :style="{ background: STATUS.stale.color }"></i>{{ c.attention }} 需关注</span>
        </div>
      </div>

      <table v-if="c.rows.length" class="mini">
        <thead><tr><th>服务</th><th class="r">运行时 总</th><th class="r">新增</th><th class="r">单测 总</th><th class="r">新增</th></tr></thead>
        <tbody>
          <tr v-for="r in c.rows.slice(0, 6)" :key="r.name">
            <td class="mono">{{ r.name }}</td>
            <td class="r num"><span class="cov"><span class="n">{{ r.runtime ? pct(r.runtime.instruction) : "—" }}</span><span class="t"><i :style="{ width: (r.runtime?.instruction ?? 0) + '%', background: SERIES.total }"></i></span></span></td>
            <td class="r num muted">{{ r.runtime?.incremental ? (r.runtime.incremental.total ? pct(r.runtime.incremental.pct) : "无新增") : "—" }}</td>
            <td class="r num">{{ r.unit ? pct(r.unit.instruction) : "—" }}</td>
            <td class="r num muted">{{ r.unit?.incremental ? (r.unit.incremental.total ? pct(r.unit.incremental.pct) : "无新增") : "—" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted" style="font-size: 12px; padding: 6px 0">还没有服务</div>
      <div class="pcard-foot muted">
        <span>{{ c.rows.length }} 个服务<template v-if="c.rows.length > 6">，显示前 6 个</template></span>
        <span v-if="c.lastAge !== null">最近采集 {{ ago(c.lastAge) }}</span>
      </div>
    </router-link>

    <router-link v-if="data && data.unassigned.length" class="pcard dashed" to="/unassigned">
      <div class="pcard-head"><div><div class="pcard-title">未分组</div><div class="muted" style="font-size: 12px">已登记但没归入任何项目的服务，进去分配。</div></div></div>
      <div class="pcard-foot muted"><span>{{ data.unassigned.length }} 个服务</span></div>
    </router-link>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 14px; }
.pcard { display: block; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 14px 16px 10px; color: inherit; }
.pcard:hover { border-color: var(--accent); text-decoration: none; }
.pcard.dashed { border-style: dashed; box-shadow: none; }
.pcard-head { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 8px; }
.pcard-title { font-size: 15px; font-weight: 650; }
.pcard-status { margin-left: auto; white-space: nowrap; }
.pcard-foot { display: flex; justify-content: space-between; font-size: 11px; padding-top: 8px; border-top: 1px solid var(--line); margin-top: 6px; }
table.mini { width: 100%; border-collapse: collapse; font-size: 12px; }
table.mini th { text-align: left; font-weight: 500; color: var(--ink-3); font-size: 11px; padding: 2px 0 4px; border-bottom: 1px solid var(--line); }
table.mini td { padding: 4px 0; border-bottom: 1px solid var(--surface-2); }
table.mini .r { text-align: right; }
table.mini .cov { grid-template-columns: 48px 44px; }
</style>
