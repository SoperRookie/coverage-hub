<script setup lang="ts">
import { computed, onMounted, provide, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api, gotoWithToken, type Project } from "./api";
import { theme } from "./ui/theme";

// 壳：左侧栏（品牌、项目选择、导航）+ 内容区。层级是项目 → 服务，所以项目选择固定在侧栏。
const route = useRoute();
const router = useRouter();
const projects = ref<Project[]>([]);
const unassignedCount = ref(0);
const version = ref("");

export interface Nav { project: string }
const nav = reactive<Nav>({ project: "" });
provide("nav", nav);

const HOME = "__home";
const UNASSIGNED = "__unassigned";

async function loadProjects() {
  try {
    const [p, o] = await Promise.all([api.projects(), api.overview()]);
    projects.value = p.projects;
    unassignedCount.value = o.unassigned.length;
  } catch (err) {
    onError(err);
  }
}
onMounted(async () => {
  loadProjects();
  try { version.value = (await api.health()).version; } catch { /* 顶栏的版本号可有可无 */ }
});
watch(() => route.fullPath, loadProjects);

const current = computed(() => projects.value.find((p) => p.name === nav.project) ?? null);
function onSelect(value: string) {
  if (value === HOME) router.push("/");
  else if (value === UNASSIGNED) router.push("/unassigned");
  else router.push(`/projects/${encodeURIComponent(value)}`);
}

const needToken = ref(false);
const token = ref("");
const message = ref("");

function onError(err: unknown): boolean {
  if (err instanceof ApiError && err.status === 401) {
    needToken.value = true;
    return true;
  }
  message.value = err instanceof Error ? err.message : String(err);
  return false;
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <router-link class="brand" to="/">
        <span class="logo">C</span>
        <span>covhub<small>JaCoCo 覆盖率看板</small></span>
      </router-link>

      <div class="section">项目</div>
      <div class="picker">
        <el-select :model-value="nav.project || HOME" size="default" style="width: 100%" placeholder="选择项目" @change="onSelect">
          <el-option :value="HOME" label="全部项目" />
          <el-option v-for="p in projects" :key="p.name" :value="p.name" :label="p.title || p.name">
            <span>{{ p.title || p.name }}</span>
            <span class="muted" style="float: right; font-size: 12px">{{ p.services.length }}</span>
          </el-option>
          <el-option v-if="unassignedCount" :value="UNASSIGNED" :label="`未分组（${unassignedCount}）`" />
        </el-select>
      </div>

      <nav>
        <router-link to="/" :class="{ active: route.path === '/' }">项目总览<span class="count">{{ projects.length }}</span></router-link>
        <router-link v-if="current" :to="`/projects/${encodeURIComponent(current.name)}`"
                     :class="{ active: (route.path.startsWith('/projects/') && !route.path.endsWith('/report')) || route.path.startsWith('/services/') }">
          {{ current.title || current.name }} 的服务<span class="count">{{ current.services.length }}</span>
        </router-link>
        <router-link v-if="current" :to="`/projects/${encodeURIComponent(current.name)}/report`"
                     :class="{ active: route.path.endsWith('/report') }">项目报表</router-link>
        <router-link v-if="unassignedCount" to="/unassigned" :class="{ active: route.path === '/unassigned' }">未分组服务<span class="count">{{ unassignedCount }}</span></router-link>
      </nav>

      <div class="foot">
        <div class="theme-row">
          <span>主题</span>
          <el-radio-group v-model="theme" size="small">
            <el-radio-button value="light">浅</el-radio-button>
            <el-radio-button value="dark">深</el-radio-button>
            <el-radio-button value="system">自动</el-radio-button>
          </el-radio-group>
        </div>
        <div>covhub {{ version || "" }}</div>
        <div><a href="docs" target="_blank">接口文档 (Swagger)</a></div>
        <div>覆盖率不按阈值着色，颜色只表示运维状态</div>
      </div>
    </aside>

    <main class="content">
      <div class="page">
        <router-view v-slot="{ Component }">
          <component :is="Component" :on-error="onError" />
        </router-view>
      </div>
    </main>
  </div>

  <el-dialog v-model="needToken" title="需要访问令牌" width="420px" :close-on-click-modal="false">
    <p class="muted">这个 hub 配了 serve.token。填一次，之后靠 Cookie 放行。</p>
    <el-input v-model="token" placeholder="serve.token" show-password @keyup.enter="gotoWithToken(token)" />
    <template #footer>
      <el-button type="primary" :disabled="!token" @click="gotoWithToken(token)">进入</el-button>
    </template>
  </el-dialog>

  <el-alert v-if="message" :title="message" type="error" show-icon closable style="position: fixed; right: 20px; bottom: 20px; width: 420px; z-index: 20" @close="message = ''" />
</template>
