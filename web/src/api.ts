// hub 的 JSON 接口。鉴权靠 Cookie（?token= 带对一次后 hub 种下），这里只负责在 401 时
// 把「需要令牌」这件事抛给页面。

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init?.body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method: init?.method || "GET",
    credentials: "same-origin",
    headers,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    /* 非 JSON 响应，下面按状态码处理 */
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, (body && (body.error || body.log)) || `HTTP ${resp.status}`, body);
  }
  return body as T;
}

/** 采集类命令的返回：非 2xx 时 hub 也会把这次执行的日志放在 log 里，409 是「业务上失败」不是坏请求。 */
export interface CommandResult {
  ok: boolean;
  service: string;
  log: string;
  latest?: Brief | null;
}

async function command(path: string): Promise<CommandResult> {
  try {
    return await request<CommandResult>(path, { method: "POST" });
  } catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.body && typeof err.body.log === "string") {
      return err.body as CommandResult;
    }
    throw err;
  }
}

export interface Incremental {
  covered: number;
  total: number;
  pct: number | null;
}

export interface Brief {
  at: string;
  version: string | null;
  instruction: number;
  branch: number;
  covered: number;
  total: number;
  classesHit: number;
  classesTotal: number;
  line?: number;
  linesCovered?: number;
  linesTotal?: number;
  incremental: Incremental | null;
  kind?: string;
}

export interface ServiceRow {
  name: string;
  project: string | null;
  channel: "pull" | "push";
  endpoint: string;
  version: string | null;
  online: boolean | null;
  unknown: boolean;
  onlineAt: string | null;
  instances: number | null;
  ageSeconds: number | null;
  stale: boolean;
  pushMixed: boolean;
  runtime: Brief | null;
  unit: Brief | null;
  diff: { version: string; base: string; addedLines: number; files: number; at: string } | null;
  breaks: number;
  hasReport: boolean;
}

export interface Counts {
  services: number;
  online: number;
  offline: number;
  unknown: number;
  stale: number;
  attention: number;
}

export interface Overview {
  generatedAt: string;
  staleAfterSeconds: number;
  projects: { name: string; title: string | null; description: string | null; services: ServiceRow[]; counts: Counts }[];
  unassigned: ServiceRow[];
  counts: Counts;
}

export interface IncFile {
  path: string;
  covered: number;
  total: number;
  pct: number | null;
  missed: number[];
  group: string | null;
}

export interface IncView {
  covered: number;
  total: number;
  pct: number | null;
  version: string | null;
  files: IncFile[];
  unmatched: string[];
  ambiguous: { file: string; paths: string[] }[];
  skipped: number;
}

export interface SourceLine {
  nr: number | null;
  text: string | null;
  status: "covered" | "missed" | "nocode" | "context" | "gap";
}

export interface SourceView {
  path: string;
  reportFile: string;
  sourceFound: boolean;
  sourcePath: string | null;
  covered: number;
  total: number;
  added: number;
  lines: SourceLine[];
  reportUrl: string | null;
}

export interface VersionRow extends Brief {
  dir: string;
  sealedAt: string;
  sealedBy: string;
  matchRate: number | null;
  reportUrl: string;
  xmlUrl: string;
}

export interface BreakRow {
  at: string;
  from?: string;
  to?: string;
  sealedAs?: string;
  reason?: string;
  instances?: number;
}

export type StatusFields = Omit<ServiceRow, "runtime" | "unit">;

export interface ArchiveRef {
  version: string;
  dir: string;
  sealedAt: string;
  sealedBy: string;
  at: string;
}

/** detail?version= 时带回来的那个归档：结算快照 + 归档元数据 */
export interface ViewingVersion extends ArchiveRef {
  execCount: number | null;
  healthVerdict: string | null;
  matchRate?: number | null;
  kind: string;
}

export interface Detail extends StatusFields {
  viewingVersion: ViewingVersion | null;
  config: Record<string, unknown>;
  runtime: {
    latest: Brief | null;
    history: Brief[];
    versions: VersionRow[];
    archives: ArchiveRef[];
    breaks: BreakRow[];
    instances: { peer: string; since: string; last: string | null }[];
    incremental: IncView | null;
    reportUrl: string | null;
    xmlUrl: string;
  };
  unit: {
    latest: Brief | null;
    history: Brief[];
    incremental: IncView | null;
    xmlUrl: string | null;
  };
}

export interface Project {
  id: number;
  name: string;
  title: string | null;
  description: string | null;
  services: string[];
}

export interface ReportVersion extends Brief {
  dir: string;
  sealedAt: string;
  matchRate: number | null;
  reportUrl: string;
  xmlUrl: string;
}

export interface ReportService {
  name: string;
  channel: "pull" | "push";
  version: string | null;
  online: boolean | null;
  unknown: boolean;
  stale: boolean;
  breaks: number;
  ageSeconds: number | null;
  runtime: Brief | null;
  unit: Brief | null;
  versions: ReportVersion[];
  unitReports: Brief[];
}

export interface ProjectReport {
  project: string;
  title: string;
  days: number;
  since: string | null;
  generatedAt: string;
  services: ReportService[];
  counts: Counts;
}

const enc = encodeURIComponent;

export const api = {
  overview: () => request<Overview>("api/overview"),
  detail: (name: string, version?: string | null) =>
    request<Detail>(`api/services/${enc(name)}/detail${version ? `?version=${enc(version)}` : ""}`),
  health: () => request<{ ok: boolean; version: string }>("api/health"),
  source: (name: string, kind: "runtime" | "unit", file: string, version?: string | null) =>
    request<SourceView>(`api/services/${enc(name)}/source?kind=${kind}&file=${enc(file)}${version ? `&version=${enc(version)}` : ""}`),
  /** 手动触发：拉一次快照并出报告（累加，不清零） */
  dump: (name: string) => command(`api/dump?service=${enc(name)}`),
  /** 手动触发：结算当前周期并归档（dump --reset + 归档 + 终版报告） */
  predeploy: (name: string, version?: string) =>
    command(`api/predeploy?service=${enc(name)}${version ? `&version=${enc(version)}` : ""}`),
  /** 手动触发：用已有 exec 重出报告 */
  report: (name: string) => command(`api/report?service=${enc(name)}`),
  projectReport: (name: string, days: number) => request<ProjectReport>(`api/projects/${enc(name)}/report?days=${days}`),
  projects: () => request<{ projects: Project[] }>("api/projects"),
  createProject: (body: { name: string; title?: string | null; description?: string | null }) =>
    request<{ project: Project }>("api/projects", { method: "POST", body }),
  updateProject: (name: string, body: { title?: string | null; description?: string | null }) =>
    request<{ project: Project }>(`api/projects/${enc(name)}`, { method: "PATCH", body }),
  deleteProject: (name: string) => request<{ ok: boolean }>(`api/projects/${enc(name)}`, { method: "DELETE" }),
  /** 把服务归到某个项目；project 传 null 表示解绑 */
  assignService: (service: string, project: string | null) =>
    request<{ service: unknown }>(`api/services/${enc(service)}`, { method: "PATCH", body: { project } }),
};

/** 401 时跳到带令牌的地址，hub 种 Cookie 后 302 回来（hash 路由的深链会保留）。 */
export function gotoWithToken(token: string) {
  const url = new URL(window.location.href);
  url.search = "?token=" + encodeURIComponent(token);
  window.location.href = url.toString();
}
