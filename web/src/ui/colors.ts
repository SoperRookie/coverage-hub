// 颜色只有两套，规则落成代码：
//   METRIC_COLORS —— 指标的固定色，与数值无关。覆盖率数字**不按阈值着色**：运行期 13%
//                    不等于「差」，按阈值标红只会训练人无视颜色。
//   STATUS_COLORS —— 语义色只给运维状态（在线 / 离线 / 采集停了 / 断代 / 混版本）。
// 模板里禁止出现 `pct < 阈值 ? … : …` 这种按值选色。

export const METRIC_COLORS = {
  runtimeTotal: "#2f6fed",   // 运行时 · 总
  runtimeInc: "#7c4dff",     // 运行时 · 新增代码
  unitTotal: "#1b9e77",      // 单测 · 总
  unitInc: "#66b2a8",        // 单测 · 新增代码
  branch: "#8c8c8c",
} as const;

export type StatusKind = "online" | "offline" | "unknown" | "stale" | "break" | "mixed";

export const STATUS: Record<StatusKind, { label: string; type: "success" | "danger" | "info" | "warning" }> = {
  online: { label: "在线", type: "success" },
  offline: { label: "离线", type: "danger" },
  unknown: { label: "未知", type: "info" },
  stale: { label: "采集停了", type: "warning" },
  break: { label: "有断代", type: "warning" },
  mixed: { label: "混版本", type: "warning" },
};

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits) + "%";
}

export function ago(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "从未";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function when(iso: string | null | undefined): string {
  return iso ? iso.replace("T", " ") : "—";
}
