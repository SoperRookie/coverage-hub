import { createRouter, createWebHashHistory } from "vue-router";
import Overview from "./pages/Overview.vue";
import ServiceDetail from "./pages/ServiceDetail.vue";

// hash 模式：hub 的 URL 根下还有 /<服务>/current/... 这些静态报告路径，history 模式会撞上
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: Overview },
    { path: "/services/:name", component: ServiceDetail, props: true },
  ],
});
