import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

// 产物出到 web/dist，是独立交付物：解包给 nginx，或用 serve.webDir 让 hub 自己托管
// （2.3 之前是直接写进 Python 包的 covhub/webui）。产物仍进版本库 —— hub 机器不装 node。
// hash 路由 + 相对 base，挂在反代子路径下也能跑。关掉 sourcemap：里面的绝对路径会让
// Windows / Linux 构建出不同的 hash，dist 进版本库就会脏。
export default defineConfig({
  base: "./",
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  build: {
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,   // Element Plus + ECharts 一个包 ~300 KB gz，内网可接受
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8900",
      "/docs": "http://127.0.0.1:8900",
      "/swagger": "http://127.0.0.1:8900",
      // 报告页与 jacoco.xml 是 hub 直接从 dataDir 服务的静态路径
      "^/[^/]+/(current|versions|unit)/": "http://127.0.0.1:8900",
    },
  },
});
