<script setup lang="ts">
import { onMounted, provide, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api, gotoWithToken, type Project } from "./api";
import ProjectDialog from "./components/ProjectDialog.vue";

// 全站顶栏：项目下拉在这里。层级是项目 → 服务，所以无论在哪一页，顶部都能切项目。
const route = useRoute();
const router = useRouter();
const projects = ref<Project[]>([]);
const unassignedCount = ref(0);

// 页面把「当前在哪个项目下」写进来（服务详情页要靠它把下拉定到所属项目）
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
onMounted(loadProjects);
// 建 / 删项目、改归属都会引起路由变化，跟着刷一次列表就够了
watch(() => route.fullPath, loadProjects);

function onSelect(value: string) {
  if (value === HOME) router.push("/");
  else if (value === UNASSIGNED) router.push("/unassigned");
  else router.push(`/projects/${encodeURIComponent(value)}`);
}

// 任何页面拿到 401 都走这里：弹令牌输入，跳 /?token= 让 hub 种 Cookie
const needToken = ref(false);
const token = ref("");
const message = ref("");
const createOpen = ref(false);

function onError(err: unknown): boolean {
  if (err instanceof ApiError && err.status === 401) {
    needToken.value = true;
    return true;
  }
  message.value = err instanceof Error ? err.message : String(err);
  return false;
}
function onCreated(name: string) { router.push(`/projects/${encodeURIComponent(name)}`); }
</script>

<template>
  <header class="nav">
    <router-link class="brand" to="/">covhub</router-link>
    <el-select :model-value="nav.project || HOME" size="default" style="width: 260px" placeholder="选择项目"
               @change="onSelect">
      <el-option :value="HOME" label="全部项目" />
      <el-option v-for="p in projects" :key="p.name" :value="p.name" :label="p.title || p.name">
        <span>{{ p.title || p.name }}</span>
        <span class="muted" style="float: right; font-size: 12px">{{ p.services.length }} 个服务</span>
      </el-option>
      <el-option v-if="unassignedCount" :value="UNASSIGNED" :label="`未分组（${unassignedCount}）`" />
    </el-select>
    <el-button size="default" plain @click="createOpen = true">新建项目</el-button>
    <span class="spacer"></span>
    <span class="muted" style="font-size: 12px">JaCoCo 运行期 / 单测覆盖率</span>
  </header>

  <div class="page">
    <router-view v-slot="{ Component }">
      <component :is="Component" :on-error="onError" />
    </router-view>

    <ProjectDialog v-model="createOpen" :editing="null" :on-error="onError" @saved="onCreated" />

    <el-dialog v-model="needToken" title="需要访问令牌" width="420px" :close-on-click-modal="false">
      <p class="muted">这个 hub 配了 serve.token。填一次，之后靠 Cookie 放行。</p>
      <el-input v-model="token" placeholder="serve.token" show-password @keyup.enter="gotoWithToken(token)" />
      <template #footer>
        <el-button type="primary" :disabled="!token" @click="gotoWithToken(token)">进入</el-button>
      </template>
    </el-dialog>

    <el-alert v-if="message" :title="message" type="error" show-icon closable @close="message = ''" />
  </div>
</template>

<style>
.nav { display: flex; align-items: center; gap: 12px; padding: 10px 20px; background: #fff; border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 10; }
.nav .brand { font-weight: 700; font-size: 16px; color: var(--ink); text-decoration: none; margin-right: 8px; }
.nav .spacer { flex: 1; }
</style>
