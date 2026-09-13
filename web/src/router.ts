import { createRouter, createWebHashHistory } from "vue-router";
import Projects from "./pages/Projects.vue";
import ProjectDashboard from "./pages/ProjectDashboard.vue";
import ServiceDetail from "./pages/ServiceDetail.vue";

// 层级：项目 → 服务。首页只有项目，进项目才看服务面板，再进服务看详情。
// hash 模式：hub 的 URL 根下还有 /<服务>/current/... 这些静态报告路径，history 模式会撞上
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: Projects },
    { path: "/projects/:name", component: ProjectDashboard, props: true },
    { path: "/unassigned", component: ProjectDashboard, props: { name: "__unassigned" } },
    { path: "/services/:name", component: ServiceDetail, props: true },
  ],
});
