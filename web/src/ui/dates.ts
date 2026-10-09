// 日期区间选择器共用的几样：接口上的日期是 YYYY-MM-DD、按 hub 本地日历切，前端也按浏览器本地日历算，
// 两边同一台内网机器的话正好对上；别用 toISOString()，那是 UTC 日期，晚上八点以后会跳到明天。

export function localDay(dt: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
}

export function daysAgo(n: number): string {
  const dt = new Date();
  dt.setDate(dt.getDate() - n);
  return localDay(dt);
}

export const isDay = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);

/** el-date-picker 的 shortcuts：区间两端都含，所以「最近 7 天」是 6 天前到今天 */
export const DAY_SHORTCUTS = [
  { text: "今天", value: () => [daysAgo(0), daysAgo(0)] },
  { text: "昨天", value: () => [daysAgo(1), daysAgo(1)] },
  { text: "最近 7 天", value: () => [daysAgo(6), daysAgo(0)] },
  { text: "最近 30 天", value: () => [daysAgo(29), daysAgo(0)] },
  { text: "最近 90 天", value: () => [daysAgo(89), daysAgo(0)] },
];

/** 不能选未来：库里不会有明天的数据 */
export const noFuture = (dt: Date) => dt.getTime() > Date.now();

/** 「2026-10-01 ～ 2026-10-09」，同一天只写一次 */
export const spanText = (from: string, to: string) => (from === to ? from : `${from} ～ ${to}`);
