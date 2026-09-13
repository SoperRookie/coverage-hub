<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import { api, type Overview, type Project } from "../api";
import type { Nav } from "../App.vue";
import { METRIC_COLORS, pct } from "../ui/colors";

// 首页：只有项目。项目是最上层的东西，服务在项目里面；点卡片进项目面板。
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
  name: string; title: string; description: string | null; services: number;
  online: number; offline: number; unknown: number; attention: number;
  // 项目卡片不算平均覆盖率（误导）；只列每个服务的运行时总覆盖，让人一眼看到分布
  bars: { name: string; runtime: number | null; inc: number | null }[];
}
const cards = computed<Card[]>(() => {
  const o = data.value;
  const byName = new Map((o?.projects ?? []).map((p) => [p.name, p]));
  return projects.value.map((p) => {
    const ov = byName.get(p.name);
    const rows = ov?.services ?? [];
    return {
      name: p.name, title: p.title || p.name, description: p.description,
      services: rows.length,
      online: rows.filter((r) => r.online).length,
      offline: rows.filter((r) => r.online === false).length,
      unknown: rows.filter((r) => r.unknown).length,
      attention: rows.filter((r) => r.stale || r.breaks || r.pushMixed).length,
      bars: rows.map((r) => ({ name: r.name, runtime: r.runtime?.instruction ?? null,
                               inc: r.runtime?.incremental?.total ? r.runtime.incremental.pct : null })),
    };
  });
});
</script>

<template>
  <div class="topbar">
    <h1>项目</h1>
    <span v-if="data" class="sub">{{ projects.length }} 个项目 · {{ data.counts.services }} 个服务 · 在线 {{ data.counts.online }} · 离线 {{ data.counts.offline }}
      <template v-if="data.counts.attention"> · 需关注 {{ data.counts.attention }}</template></span>
    <span class="spacer"></span>
    <el-button size="small" @click="load">刷新</el-button>
  </div>

  <div v-if="loading" class="muted">加载中…</div>
  <div v-else-if="!projects.length" class="card muted">
    还没有项目。先用顶部的「新建项目」，再把服务加进去（服务用 <code>covhub service add</code> 登记，或从「未分组」里添加）。
  </div>

  <div class="cards">
    <router-link v-for="c in cards" :key="c.name" class="pcard" :to="`/projects/${encodeURIComponent(c.name)}`">
      <div class="pcard-head">
        <span class="pcard-title">{{ c.title }}</span>
        <span class="muted mono">{{ c.name }}</span>
      </div>
      <div v-if="c.description" class="muted pcard-desc">{{ c.description }}</div>
      <div class="pcard-counts">
        <span>{{ c.services }} 个服务</span>
        <el-tag v-if="c.online" type="success" size="small" effect="light">在线 {{ c.online }}</el-tag>
        <el-tag v-if="c.offline" type="danger" size="small" effect="light">离线 {{ c.offline }}</el-tag>
        <el-tag v-if="c.unknown" type="info" size="small" effect="light">未知 {{ c.unknown }}</el-tag>
        <el-tag v-if="c.attention" type="warning" size="small" effect="light">需关注 {{ c.attention }}</el-tag>
      </div>
      <div class="pcard-bars">
        <div v-for="b in c.bars.slice(0, 6)" :key="b.name" class="pbar">
          <span class="pbar-name mono">{{ b.name }}</span>
          <span class="pbar-track"><i :style="{ width: (b.runtime ?? 0) + '%', background: METRIC_COLORS.runtimeTotal }"></i></span>
          <span class="pbar-val num" :style="{ color: METRIC_COLORS.runtimeTotal }">{{ pct(b.runtime) }}</span>
          <span class="pbar-val num" :style="{ color: METRIC_COLORS.runtimeInc }" title="本版本新增代码">{{ b.inc === null ? "—" : pct(b.inc) }}</span>
        </div>
        <div v-if="c.bars.length > 6" class="muted" style="font-size: 12px">… 还有 {{ c.bars.length - 6 }} 个</div>
        <div v-if="!c.bars.length" class="muted" style="font-size: 12px">还没有服务</div>
      </div>
    </router-link>

    <router-link v-if="data && data.unassigned.length" class="pcard pcard-dashed" to="/unassigned">
      <div class="pcard-head"><span class="pcard-title">未分组</span></div>
      <div class="muted pcard-desc">已登记但还没归入任何项目的服务，进去把它们分配到项目里。</div>
      <div class="pcard-counts"><span>{{ data.unassigned.length }} 个服务</span></div>
    </router-link>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.pcard { display: block; background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; color: inherit; text-decoration: none; }
.pcard:hover { border-color: #2f6fed; }
.pcard-dashed { border-style: dashed; }
.pcard-head { display: flex; align-items: baseline; gap: 10px; }
.pcard-title { font-size: 16px; font-weight: 600; }
.pcard-desc { font-size: 12px; margin-top: 4px; }
.pcard-counts { display: flex; gap: 6px; align-items: center; margin: 10px 0 8px; font-size: 12px; color: var(--muted); }
.pbar { display: grid; grid-template-columns: 1fr 120px 52px 52px; gap: 8px; align-items: center; font-size: 12px; margin-top: 4px; }
.pbar-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pbar-track { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
.pbar-track > i { display: block; height: 100%; }
.pbar-val { text-align: right; }
</style>
