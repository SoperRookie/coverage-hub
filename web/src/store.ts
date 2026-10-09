import { api, type Overview, type Project } from "./api";

// 总览与项目列表是侧栏和每个页面都要的东西。以前壳（App.vue）和页面各自请求一遍，
// 切一次路由就打两次 /api/overview —— 它是整站最重的接口（每个服务一行，带运行时 / 单测 /
// diff 的摘要）。这里合成一份：同一时刻只发一个请求，几秒内的结果直接复用；
// 页面上的「刷新」按钮、定时器和写操作之后传 force 绕过缓存。
export interface Snapshot {
  overview: Overview;
  projects: Project[];
}

const FRESH_MS = 5_000;
let cached: { at: number; data: Snapshot } | null = null;
let inflight: Promise<Snapshot> | null = null;

/** 写操作（建 / 删项目、归属变更）之后调：下一次 loadSnapshot() 必须重新拉。 */
export function invalidate(): void {
  cached = null;
}

export function loadSnapshot(force = false): Promise<Snapshot> {
  if (!force && cached && Date.now() - cached.at < FRESH_MS) return Promise.resolve(cached.data);
  if (inflight) return inflight;
  inflight = Promise.all([api.overview(), api.projects()])
    .then(([overview, p]) => {
      const data = { overview, projects: p.projects };
      cached = { at: Date.now(), data };
      return data;
    })
    .finally(() => { inflight = null; });
  return inflight;
}
