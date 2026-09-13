// 颜色只有三套，规则落成代码：
//   SERIES —— 两个系列色：「总覆盖」蓝、「新增代码」橙（已用调色板校验器验过：色盲可分、对比 ≥ 3:1）。
//             运行时 / 单测靠分组、标题区分，不再各配一种色 —— 四种色里有两对色盲分不开。
//             数字本身用文字色，系列色只出现在旁边的色条 / 图例点上（文字不穿系列色）。
//             覆盖率数字**不按阈值着色**：运行期 13% 不等于「差」。
//   STATUS —— 运维状态色（在线 / 离线 / 采集停了 / 断代 / 混版本 / 未知），带圆点 + 文字，从不只靠颜色。
//   两者的色值都固定，不随主题变。

export const SERIES = {
  total: "#2a78d6",   // 总覆盖
  inc: "#eb6834",     // 本版本新增代码
} as const;

export type StatusKind = "online" | "offline" | "unknown" | "stale" | "break" | "mixed";

export const STATUS: Record<StatusKind, { label: string; color: string; tone: "good" | "critical" | "warning" | "muted" }> = {
  online:  { label: "在线",     color: "#0ca30c", tone: "good" },
  offline: { label: "离线",     color: "#d03b3b", tone: "critical" },
  unknown: { label: "未知",     color: "#8a8985", tone: "muted" },
  stale:   { label: "采集停了", color: "#ec835a", tone: "warning" },
  break:   { label: "有断代",   color: "#ec835a", tone: "warning" },
  mixed:   { label: "混版本",   color: "#ec835a", tone: "warning" },
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

export function num(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}
