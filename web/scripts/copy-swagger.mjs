// 把 swagger-ui-dist 里用得到的几个文件拷进产物目录（covhub/webui/swagger/）。
// hub 自己托管 Swagger UI（/docs），不从 CDN 拉 —— hub 常在内网。vite 的 emptyOutDir 会清空
// 产物目录，所以这一步放在 vite build 之后跑（package.json 的 build 脚本）。
import { copyFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const src = dirname(require.resolve("swagger-ui-dist/package.json"));
const dest = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "covhub", "webui", "swagger");
mkdirSync(dest, { recursive: true });
for (const f of ["swagger-ui.css", "swagger-ui-bundle.js", "swagger-ui-standalone-preset.js", "favicon-32x32.png"]) {
  copyFileSync(join(src, f), join(dest, f));
}
console.log("swagger-ui-dist → " + dest);
