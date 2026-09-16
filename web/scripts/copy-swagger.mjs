// 把 swagger-ui-dist 里用得到的几个文件拷进后端的资源目录（covhub/static/swagger/）。
// hub 自己托管 Swagger UI（/docs），不从 CDN 拉 —— hub 常在内网。
//
// 接口文档是后端自己的能力，资源跟着后端走：这个脚本**不在** npm run build 里，
// 只有升级 swagger-ui-dist 时手动跑一次 `npm run swagger`，产物一起提交。
import { copyFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const src = dirname(require.resolve("swagger-ui-dist/package.json"));
const dest = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "covhub", "static", "swagger");
mkdirSync(dest, { recursive: true });
for (const f of ["swagger-ui.css", "swagger-ui-bundle.js", "swagger-ui-standalone-preset.js", "favicon-32x32.png"]) {
  copyFileSync(join(src, f), join(dest, f));
}
console.log("swagger-ui-dist → " + dest);
