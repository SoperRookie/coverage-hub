// hub 的 JSON 接口。鉴权靠 Cookie（?token= 带对一次后 hub 种下），这里只负责在 401 时
// 把「需要令牌」这件事抛给页面。

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string): Promise<T> {
  const resp = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    /* 非 JSON 响应，下面按状态码处理 */
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, (body && body.error) || `HTTP ${resp.status}`);
  }
  return body as T;
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

export interface Detail extends StatusFields {
  config: Record<string, unknown>;
  runtime: {
    latest: Brief | null;
    history: Brief[];
    versions: VersionRow[];
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

export const api = {
  overview: () => request<Overview>("api/overview"),
  detail: (name: string) => request<Detail>(`api/services/${encodeURIComponent(name)}/detail`),
  health: () => request<{ ok: boolean; version: string }>("api/health"),
};

/** 401 时跳到带令牌的地址，hub 种 Cookie 后 302 回来（hash 路由的深链会保留）。 */
export function gotoWithToken(token: string) {
  const url = new URL(window.location.href);
  url.search = "?token=" + encodeURIComponent(token);
  window.location.href = url.toString();
}
