<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import { api, type Overview, type Project, type ServiceRow } from "../api";
import ProjectDialog from "../components/ProjectDialog.vue";
import ServiceTable from "../components/ServiceTable.vue";
import type { Nav } from "../App.vue";
import { METRIC_COLORS } from "../ui/colors";

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
  <div class="topbar">
    <router-link class="plain" to="/">← 项目</router-link>
    <h1>{{ isPool ? "未分组" : (project?.title || name) }}</h1>
    <span class="sub">
      <template v-if="isPool">已登记但还没归入任何项目的服务</template>
      <template v-else><span class="mono">{{ name }}</span><template v-if="project?.description"> · {{ project.description }}</template>
        <template v-if="counts"> · {{ counts.services }} 个服务，在线 {{ counts.online }}，离线 {{ counts.offline }}<template v-if="counts.stale">，采集停了 {{ counts.stale }}</template></template>
      </template>
    </span>
    <span class="spacer"></span>
    <template v-if="!isPool">
      <el-button size="small" type="primary" plain @click="addOpen = true">添加服务</el-button>
      <el-button size="small" @click="editOpen = true">编辑</el-button>
      <el-button size="small" type="danger" plain @click="removeProject">删除项目</el-button>
    </template>
    <el-button size="small" @click="load">刷新</el-button>
  </div>

  <div class="card" style="padding: 8px 12px">
    <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeTotal }"></i>运行时 · 总</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.runtimeInc }"></i>运行时 · 新增代码</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.unitTotal }"></i>单测 · 总</span>
    <span class="legend"><i :style="{ background: METRIC_COLORS.unitInc }"></i>单测 · 新增代码</span>
    <span class="legend muted">覆盖率数字不按阈值着色；颜色只表示运维状态</span>
  </div>

  <div v-if="loading && !data" class="muted">加载中…</div>
  <div v-else class="card">
    <div v-if="!rows.length" class="muted">
      <template v-if="isPool">所有服务都已归入项目。</template>
      <template v-else>这个项目下还没有服务。点右上角「添加服务」从未分组里挑，或登记时用 <code>covhub service add … --project {{ name }}</code>。</template>
    </div>
    <template v-else-if="isPool">
      <ServiceTable :rows="rows" mode="unassigned" />
      <div style="margin-top: 10px">
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

  <el-dialog v-model="addOpen" title="本项目包含的服务" width="520px">
    <p class="muted" style="margin-top: 0">勾上就归入本项目，取消就移出，立即生效。要登记新服务用 <code>covhub service add</code>。</p>
    <div v-if="!allServices.length" class="muted">还没有登记任何服务。</div>
    <div v-for="r in allServices" :key="r.name" style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px">
      <el-checkbox :model-value="r.project === name" :disabled="busy.has(r.name)" @change="(v: boolean | string | number) => toggle(r, !!v)">
        <span class="mono">{{ r.name }}</span>
      </el-checkbox>
      <span class="muted" style="font-size: 12px">{{ r.channel }} · {{ r.endpoint }}</span>
      <span class="spacer" style="flex: 1"></span>
      <el-tag v-if="r.project && r.project !== name" size="small" type="info" effect="plain">当前在 {{ r.project }}</el-tag>
      <el-tag v-else-if="!r.project" size="small" type="info" effect="plain">未分组</el-tag>
    </div>
    <template #footer>
      <el-button @click="addOpen = false">关闭</el-button>
    </template>
  </el-dialog>

  <ProjectDialog v-model="editOpen" :editing="project" :on-error="onError" @saved="load" />
</template>
