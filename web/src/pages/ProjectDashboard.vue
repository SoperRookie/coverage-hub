<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import { api, type Overview, type Project, type ServiceRow } from "../api";
import ProjectDialog from "../components/ProjectDialog.vue";
import ServiceTable from "../components/ServiceTable.vue";
import PageHeader from "../components/PageHeader.vue";
import Kpi from "../components/Kpi.vue";
import type { Nav } from "../App.vue";
import { SERIES } from "../ui/colors";

// 一个项目的面板：它下面的服务，以及往里加 / 移出服务。name 为 __unassigned 时是未分组池。
const props = defineProps<{ name: string; onError: (err: unknown) => boolean }>();
const router = useRouter();
const UNASSIGNED = "__unassigned";
const nav = inject<Nav>("nav")!;
watch(() => props.name, (n) => { nav.project = n; }, { immediate: true });

const data = ref<Overview | null>(null);
const project = ref<Project | null>(null);
const loading = ref(true);
let timer: number | undefined;

async function load() {
  try {
    const [o, p] = await Promise.all([api.overview(), api.projects()]);
    data.value = o;
    project.value = p.projects.find((x) => x.name === props.name) ?? null;
    if (props.name !== UNASSIGNED && !project.value) {
      ElMessage.warning(`没有名为 ${props.name} 的项目`);
      router.replace("/");
    }
  } catch (err) {
    props.onError(err);
  } finally {
    loading.value = false;
  }
}
onMounted(() => { load(); timer = window.setInterval(load, 60_000); });
onBeforeUnmount(() => window.clearInterval(timer));
watch(() => props.name, load);

const isPool = computed(() => props.name === UNASSIGNED);
const rows = computed<ServiceRow[]>(() => {
  if (!data.value) return [];
  if (isPool.value) return data.value.unassigned;
  return data.value.projects.find((p) => p.name === props.name)?.services ?? [];
});
const counts = computed(() => data.value?.projects.find((p) => p.name === props.name)?.counts ?? null);
const unassigned = computed(() => data.value?.unassigned ?? []);
const allProjects = ref<Project[]>([]);
watch(data, async () => { if (isPool.value) allProjects.value = (await api.projects()).projects; });

// ---- 添加服务：列出全部服务，勾上就归进来、取消就移出，立即生效，不用再点确认 ----
const addOpen = ref(false);
const allServices = computed<ServiceRow[]>(() => {
  const o = data.value;
  if (!o) return [];
  return [...o.projects.flatMap((p) => p.services), ...o.unassigned].sort((a, b) => a.name.localeCompare(b.name));
});
const busy = ref<Set<string>>(new Set());
async function toggle(row: ServiceRow, checked: boolean) {
  busy.value.add(row.name);
  try {
    await api.assignService(row.name, checked ? props.name : null);
    ElMessage.success(checked ? `${row.name} 已归入本项目` : `${row.name} 已移出`);
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value.delete(row.name);
  }
}

async function remove(row: ServiceRow) {
  try {
    await api.assignService(row.name, null);
    ElMessage.success(`${row.name} 已移出项目（配置与采集数据不动）`);
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

// 未分组池：把某个服务归到项目
async function assignTo(row: ServiceRow, target: string) {
  if (!target) return;
  try {
    await api.assignService(row.name, target);
    ElMessage.success(`${row.name} 已归入 ${target}`);
    await load();
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

// ---- 项目本身 ----
const editOpen = ref(false);
async function removeProject() {
  const p = project.value;
  if (!p) return;
  try {
    await ElMessageBox.confirm(
      `删除项目 ${p.title || p.name}？项目下的 ${rows.value.length} 个服务会回到未分组，配置与采集数据都不会删。`,
      "删除项目", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  } catch { return; }
  try {
    await api.deleteProject(p.name);
    ElMessage.success(`已删除项目 ${p.name}`);
    router.replace("/");
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <PageHeader :title="isPool ? '未分组服务' : (project?.title || name)"
              :crumbs="[{ label: '项目总览', to: '/' }, { label: isPool ? '未分组' : (project?.title || name) }]">
    <template #meta>
      <template v-if="isPool"><span>已登记但还没归入任何项目的服务</span></template>
      <template v-else>
        <span class="mono">{{ name }}</span>
        <span v-if="project?.description">{{ project.description }}</span>
      </template>
    </template>
    <template #actions>
      <template v-if="!isPool">
        <el-button size="small" type="primary" plain @click="addOpen = true">添加服务</el-button>
        <el-button size="small" @click="editOpen = true">编辑</el-button>
        <el-button size="small" type="danger" plain @click="removeProject">删除项目</el-button>
      </template>
      <el-button size="small" @click="load">刷新</el-button>
    </template>
  </PageHeader>

  <div v-if="counts" class="kpis" style="margin-bottom: 18px">
    <Kpi label="服务" :value="String(counts.services)" :sub="`在线 ${counts.online} · 离线 ${counts.offline}${counts.unknown ? ' · 未知 ' + counts.unknown : ''}`" />
    <Kpi label="采集停了" :value="String(counts.stale)" sub="超过 3 个轮询周期没有新数据" :dim="!counts.stale" />
    <Kpi label="需关注" :value="String(counts.attention)" sub="采集停了 / 有断代 / 混版本" :dim="!counts.attention" />
  </div>

  <div class="card">
    <div class="card-head">
      <h2>服务</h2>
      <span class="hint">
        <span class="legend"><i :style="{ background: SERIES.total }"></i>总覆盖</span>
        <span class="legend"><i :style="{ background: SERIES.inc }"></i>本版本新增代码</span>
      </span>
      <span class="spacer"></span>
      <span class="hint">覆盖率数字不按阈值着色；颜色只表示运维状态</span>
    </div>
    <div class="card-body flush">
      <div v-if="loading && !data" class="empty">加载中…</div>
      <div v-else-if="!rows.length" class="empty">
        <template v-if="isPool"><b>所有服务都已归入项目</b></template>
        <template v-else><b>这个项目下还没有服务</b>点右上角「添加服务」从未分组里挑，或登记时用 <code>covhub service add … --project {{ name }}</code>。</template>
      </div>
      <template v-else-if="isPool">
        <ServiceTable :rows="rows" mode="unassigned" />
        <div style="padding: 12px 16px; border-top: 1px solid var(--line)">
          <span class="muted" style="margin-right: 8px">把服务归入项目：</span>
          <span v-for="r in rows" :key="r.name" style="display: inline-flex; align-items: center; gap: 6px; margin: 0 14px 6px 0">
            <span class="mono">{{ r.name }}</span>
            <el-select size="small" placeholder="选项目" style="width: 160px" @change="(v: string) => assignTo(r, v)">
              <el-option v-for="p in allProjects" :key="p.name" :value="p.name" :label="p.title || p.name" />
            </el-select>
          </span>
        </div>
      </template>
      <ServiceTable v-else :rows="rows" mode="project" @remove="remove" />
    </div>
  </div>

  <el-dialog v-model="addOpen" title="本项目包含的服务" width="520px">
    <p class="muted" style="margin-top: 0">勾上就归入本项目，取消就移出，立即生效。已在别的项目里的服务要先从那边移出才能勾选。要登记新服务用 <code>covhub service add</code>。</p>
    <div v-if="!allServices.length" class="muted">还没有登记任何服务。</div>
    <div v-for="r in allServices" :key="r.name" style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px">
      <el-checkbox :model-value="r.project === name" :disabled="busy.has(r.name) || (!!r.project && r.project !== name)"
                   @change="(v: boolean | string | number) => toggle(r, !!v)">
        <span class="mono">{{ r.name }}</span>
      </el-checkbox>
      <span class="muted" style="font-size: 12px">{{ r.channel }} · {{ r.endpoint }}</span>
      <span class="spacer"></span>
      <span v-if="r.project && r.project !== name" class="muted" style="font-size: 12px">在 {{ r.project }} 里，先从那边移出</span>
      <span v-else-if="!r.project" class="muted" style="font-size: 12px">未分组</span>
    </div>
    <template #footer><el-button @click="addOpen = false">关闭</el-button></template>
  </el-dialog>

  <ProjectDialog v-model="editOpen" :editing="project" :on-error="onError" @saved="load" />
</template>
