<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import { api, type Overview, type Project, type ServiceRow } from "../api";
import ProjectDialog from "../components/ProjectDialog.vue";
import StatusTag from "../components/StatusTag.vue";
import { METRIC_COLORS, ago, pct } from "../ui/colors";

const props = defineProps<{ onError: (err: unknown) => boolean }>();
const route = useRoute();
const router = useRouter();

const data = ref<Overview | null>(null);
const projects = ref<Project[]>([]);
const loading = ref(true);
let timer: number | undefined;

// 当前看哪个项目：__all（全部）/ __unassigned（未分组）/ 项目名。放在 URL 的 query 里，刷新和分享链接都还在
const ALL = "__all";
const UNASSIGNED = "__unassigned";
const current = computed<string>({
  get: () => (route.query.project as string) || ALL,
  set: (v) => router.replace({ query: v === ALL ? {} : { project: v } }),
});

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

// 当前视图下的分组：全部 → 每个项目一组 + 未分组；单个项目 → 只有它
interface Group { key: string; title: string; sub: string; rows: ServiceRow[]; project: Project | null; counts: Overview["counts"] | null }
const groups = computed<Group[]>(() => {
  const o = data.value;
  if (!o) return [];
  const byName = new Map(projects.value.map((p) => [p.name, p]));
  const all: Group[] = o.projects.map((p) => ({
    key: p.name, title: p.title || p.name, sub: p.description || p.name, rows: p.services,
    project: byName.get(p.name) ?? null, counts: p.counts,
  }));
  // 库里有、但 overview 里还没服务的空项目也要显示，否则刚建的项目"消失"了
  for (const p of projects.value) {
    if (!all.some((g) => g.key === p.name)) {
      all.push({ key: p.name, title: p.title || p.name, sub: p.description || p.name, rows: [], project: p, counts: null });
    }
  }
  all.push({ key: UNASSIGNED, title: "未分组", sub: "没有归属项目的服务", rows: o.unassigned, project: null, counts: null });
  if (current.value === ALL) return all.filter((g) => g.key !== UNASSIGNED || g.rows.length);
  return all.filter((g) => g.key === current.value);
});

watch([projects, () => route.query.project], () => {
  // 当前项目被删了就回到全部
  const v = current.value;
  if (v !== ALL && v !== UNASSIGNED && projects.value.length && !projects.value.some((p) => p.name === v)) current.value = ALL;
});

// ---- 项目的增删改 ----
const dialogOpen = ref(false);
const editing = ref<Project | null>(null);
function openCreate() { editing.value = null; dialogOpen.value = true; }
function openEdit(p: Project) { editing.value = p; dialogOpen.value = true; }
async function onSaved(name: string) { await load(); current.value = name; }

async function removeProject(p: Project) {
  try {
    await ElMessageBox.confirm(
      `删除项目 ${p.title || p.name}？项目下的 ${p.services.length} 个服务会变成未分组，配置与采集数据都不会删。`,
      "删除项目", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  } catch { return; }
  try {
    await api.deleteProject(p.name);
    ElMessage.success(`已删除项目 ${p.name}`);
    if (current.value === p.name) current.value = ALL;
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

// ---- 服务归属 ----
const NONE = "";
async function assign(row: ServiceRow, project: string) {
  try {
    await api.assignService(row.name, project === NONE ? null : project);
    ElMessage.success(project === NONE ? `${row.name} 已改为未分组` : `${row.name} 已归入 ${project}`);
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

// el-table 的插槽参数在模板里推不出泛型，显式收一下
const asRow = (r: unknown) => r as ServiceRow;

function incText(r: ServiceRow, which: "runtime" | "unit") {
  const b = r[which];
  if (!b || !b.incremental) return "—";
  return b.incremental.total === 0 ? "无新增" : pct(b.incremental.pct);
}
</script>

<template>
  <div class="topbar">
    <h1>覆盖率看板</h1>
    <span v-if="data" class="sub">{{ data.counts.services }} 个服务 · 在线 {{ data.counts.online }} · 离线 {{ data.counts.offline }}
      <template v-if="data.counts.attention"> · 需关注 {{ data.counts.attention }}</template>
      · 更新于 {{ data.generatedAt.replace("T", " ") }}</span>
    <span class="spacer"></span>
    <el-button size="small" type="primary" plain @click="openCreate">新建项目</el-button>
    <el-button size="small" @click="load">刷新</el-button>
  </div>

  <!-- 项目切换：全部 / 各项目 / 未分组 -->
  <div class="card" style="padding: 8px 12px">
    <el-radio-group v-model="current" size="small">
      <el-radio-button :value="ALL">全部</el-radio-button>
      <el-radio-button v-for="p in projects" :key="p.name" :value="p.name">
        {{ p.title || p.name }}<span class="muted" style="margin-left: 4px">{{ p.services.length }}</span>
      </el-radio-button>
      <el-radio-button v-if="data && data.unassigned.length" :value="UNASSIGNED">未分组<span class="muted" style="margin-left: 4px">{{ data.unassigned.length }}</span></el-radio-button>
    </el-radio-group>
    <span style="margin-left: 16px">
      <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeTotal }"></i>运行时 · 总</span>
      <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeInc }"></i>运行时 · 新增</span>
      <span class="legend"><i :style="{ background: METRIC_COLORS.unitTotal }"></i>单测 · 总</span>
      <span class="legend"><i :style="{ background: METRIC_COLORS.unitInc }"></i>单测 · 新增</span>
    </span>
  </div>

  <div v-if="loading" class="muted">加载中…</div>
  <div v-else-if="data && !data.counts.services && !projects.length" class="card muted">
    还没有登记任何服务。用 <code>covhub service add &lt;name&gt; …</code> 或 <code>covhub import</code> 登记，再在这里建项目把它们分组。
  </div>

  <div v-for="g in groups" :key="g.key" class="card">
    <h2 style="display: flex; align-items: center; gap: 8px">
      <span>{{ g.title }}</span>
      <span class="hint" style="flex: 1">{{ g.sub }}
        <template v-if="g.counts"> · {{ g.counts.services }} 个服务，离线 {{ g.counts.offline }}<template v-if="g.counts.stale">，采集停了 {{ g.counts.stale }}</template></template>
      </span>
      <template v-if="g.project">
        <el-button size="small" text @click="openEdit(g.project)">编辑</el-button>
        <el-button size="small" text type="danger" @click="removeProject(g.project)">删除</el-button>
      </template>
    </h2>
    <div v-if="!g.rows.length" class="muted">这个项目下还没有服务 —— 切到「全部」或「未分组」，在服务行的「项目」列里选它即可归入。</div>
    <el-table v-else :data="g.rows" size="small" stripe>
      <el-table-column label="服务" min-width="180">
        <template #default="{ row }">
          <router-link class="plain" :to="`/services/${encodeURIComponent(row.name)}`">{{ row.name }}</router-link>
          <div class="muted mono">{{ row.channel }} · {{ row.endpoint }}</div>
        </template>
      </el-table-column>
      <el-table-column label="项目" width="150">
        <template #default="{ row }">
          <el-select :model-value="row.project ?? NONE" size="small" placeholder="未分组" @change="(v: string) => assign(asRow(row), v)">
            <el-option :value="NONE" label="未分组" />
            <el-option v-for="p in projects" :key="p.name" :value="p.name" :label="p.title || p.name" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="200">
        <template #default="{ row }"><StatusTag :row="asRow(row)" /></template>
      </el-table-column>
      <el-table-column label="版本" width="100">
        <template #default="{ row }"><span class="mono">{{ row.version || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="运行时 总" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeTotal }">{{ row.runtime ? pct(row.runtime.instruction) : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="运行时 新增" width="110" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.runtimeInc }">{{ incText(asRow(row), "runtime") }}</span></template>
      </el-table-column>
      <el-table-column label="单测 总" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitTotal }">{{ row.unit ? pct(row.unit.instruction) : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="单测 新增" width="100" align="right">
        <template #default="{ row }"><span class="num" :style="{ color: METRIC_COLORS.unitInc }">{{ incText(asRow(row), "unit") }}</span></template>
      </el-table-column>
      <el-table-column label="触达类" width="100" align="right">
        <template #default="{ row }"><span class="num">{{ row.runtime ? `${row.runtime.classesHit} / ${row.runtime.classesTotal}` : "—" }}</span></template>
      </el-table-column>
      <el-table-column label="最后采集" width="110">
        <template #default="{ row }"><span class="muted">{{ ago(row.ageSeconds) }}</span></template>
      </el-table-column>
    </el-table>
  </div>

  <ProjectDialog v-model="dialogOpen" :editing="editing" :on-error="onError" @saved="onSaved" />
</template>
