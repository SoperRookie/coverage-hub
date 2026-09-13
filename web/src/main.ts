import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import "element-plus/theme-chalk/dark/css-vars.css";
import "./ui/base.css";
import "./ui/theme";          // 挂上 html.dark，早于首屏渲染

createApp(App).use(router).mount("#app");
