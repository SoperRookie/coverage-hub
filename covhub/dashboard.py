"""看板渲染。"""

from datetime import datetime
from html import escape as html_escape
import os
import urllib.parse

from . import __version__
from .agent import endpoint_label, reachable, service_channel
from .collector import get_collector, collector_instances
from .layout import svc_dir
from .db import repo

# --------------------------------------------------------------------------
# 看板
# --------------------------------------------------------------------------

def _esc(value):
    """转义成可安全插进 HTML 文本或属性的字符串。

    看板上的服务名、版本号、断代记录都不是本地常量 —— version 经
    /api/retarget 从流水线传进来，落进 state.json，再被渲染进 index.html。
    不转义就等于把写接口变成了看板的脚本注入入口，而 index.html 是全组在看的。
    """
    return html_escape("" if value is None else str(value), quote=True)


def _url(value):
    """编码成 URL 的一个路径段，再按 HTML 属性转义。

    报告链接由服务名 / 版本号拼成，这两者都可能带 / # ? —— 只做 HTML 转义
    拦不住它们改变链接指向，得先做百分号编码。
    """
    return _esc(urllib.parse.quote("" if value is None else str(value), safe=""))


def dashboard_rows(cfg):
    """把每个服务整理成看板要用的一行。

    比 status 多算两件运维上真正关心的事：
      · stale —— 最后一次采集离现在太久，说明采集停了（看板上不显示时间差的话，
        没人会去心算「18:16 是多久以前」）
      · breaks —— 有过没结算的重启，那段覆盖率已经丢了，必须显眼
    """
    interval = int((cfg.get("watch") or {}).get("intervalSeconds", 300))
    stale_after = max(interval * 3, 900)
    now = datetime.now()

    rows = []
    for svc in cfg.get("services", []):
        latest = repo.latest(svc["name"])
        history = repo.history(svc["name"], 40)

        age = None
        if latest:
            try:
                age = (now - datetime.fromisoformat(latest["at"])).total_seconds()
            except (ValueError, TypeError):
                age = None

        # 与上一次采集比，看趋势是涨是跌
        delta = None
        pts = [h["instruction"] for h in history if "instruction" in h]
        if len(pts) >= 2:
            delta = pts[-1] - pts[-2]

        channel = service_channel(svc)
        insts = collector_instances(svc["name"]) if channel == "push" else []
        rows.append({
            "name": svc["name"],
            "channel": channel,
            "endpoint": endpoint_label(svc),
            "online": reachable(svc, timeout=1.0),
            "unknown": channel == "push" and get_collector() is None,
            "instances": len(insts),
            "latest": latest,
            "age": age,
            "stale": age is not None and age > stale_after,
            "delta": delta,
            "history": history,
            "versions": repo.versions(svc["name"], 10),
            "breaks": repo.breaks(svc["name"], 3),
            "hasReport": os.path.isfile(
                os.path.join(svc_dir(cfg, svc), "current", "html", "index.html")),
        })
    return rows


def render_dashboard(cfg):
    html = build_dashboard_html(dashboard_rows(cfg))
    os.makedirs(cfg["dataDir"], exist_ok=True)
    with open(os.path.join(cfg["dataDir"], "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def human_age(seconds):
    if seconds is None:
        return "从未采集"
    seconds = int(seconds)
    if seconds < 90:
        return "%d 秒前" % seconds
    if seconds < 5400:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 172800:
        return "%d 小时前" % (seconds // 3600)
    return "%d 天前" % (seconds // 86400)


def spark(history, key="instruction", w=240, h=44):
    """内联 SVG 趋势图：面积 + 折线 + 末点。不引入任何前端依赖。"""
    pts = [h[key] for h in history if key in h]
    if len(pts) < 2:
        return '<div class="spark-empty">趋势数据不足（至少要两次采集）</div>'

    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    pad = 4.0
    step = (w - 2) / (len(pts) - 1)

    def xy(i, v):
        return (1 + i * step, h - pad - (v - lo) / span * (h - 2 * pad))

    coords = [xy(i, v) for i, v in enumerate(pts)]
    line = " ".join("%.1f,%.1f" % c for c in coords)
    area = "%s %.1f,%.1f %.1f,%.1f" % (line, coords[-1][0], h, coords[0][0], h)
    last = coords[-1]
    return (
        '<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" '
        'role="img" aria-label="指令覆盖率趋势，最近 %d 次采集">'
        '<polygon class="spark-area" points="%s"/>'
        '<polyline class="spark-line" points="%s"/>'
        '<circle class="spark-dot" cx="%.1f" cy="%.1f" r="2.6"/>'
        "</svg>" % (w, h, len(pts), area, line, last[0], last[1]))


def _status_pill(r):
    """状态徽章。语义色只给运维状态用 —— 覆盖率高低不着色，见 CSS 注释。"""
    if r["unknown"]:
        return '<span class="pill pill-warn">状态未知</span>'
    if not r["online"]:
        return '<span class="pill pill-bad">离线</span>'
    if r["channel"] == "push":
        return '<span class="pill pill-ok">在线 · %d 个实例</span>' % r["instances"]
    return '<span class="pill pill-ok">在线</span>'


def _card(r):
    latest = r["latest"]
    head = (
        '<div class="card-head">'
        '<div class="card-title"><h2>%s</h2>'
        '<span class="chan chan-%s">%s</span></div>'
        "%s</div>"
        '<div class="endpoint">%s</div>'
        % (_esc(r["name"]), _esc(r["channel"]), _esc(r["channel"]),
           _status_pill(r), _esc(r["endpoint"])))

    if not latest:
        body = ('<div class="empty-body">尚未采集到数据<span>确认 agent 已注入，'
                "再跑一次 dump</span></div>")
    else:
        inst = latest["instruction"]
        delta = ""
        if r["delta"] is not None and abs(r["delta"]) >= 0.05:
            up = r["delta"] > 0
            delta = ('<span class="delta %s">%s%.1f</span>'
                     % ("up" if up else "down", "+" if up else "−", abs(r["delta"])))

        alerts = []
        if r["stale"]:
            alerts.append('<div class="alert alert-warn">最后一次采集在 %s，'
                          "采集可能已经停了 —— 确认 watch 还在跑</div>"
                          % human_age(r["age"]))
        for b in reversed(r["breaks"]):
            at = _esc(b.get("at", "").replace("T", " "))
            if b.get("sealedAs"):
                alerts.append('<div class="alert alert-warn">检测到未结算的重启，'
                              "已自动结算为 <b>%s</b>（%s）</div>"
                              % (_esc(b.get("sealedAs", "?")), at))
            else:
                alerts.append('<div class="alert alert-warn">在线实例跑着两份不同的 '
                              "class（%s）—— 滚动发版中途采到的数据对不上同一份 "
                              "class 产物，发版流程里补一次 predeploy</div>" % at)

        body = (
            '<div class="headline">'
            '<div class="big"><span class="num">%.1f</span><span class="pct">%%</span>%s</div>'
            '<div class="big-label">指令覆盖率</div>'
            "</div>"
            '<div class="meter" role="img" aria-label="指令覆盖率 %.1f%%">'
            '<i style="width:%.2f%%"></i></div>'
            '<dl class="figs">'
            "<div><dt>分支</dt><dd>%.1f<i>%%</i></dd></div>"
            "<div><dt>触达类</dt><dd>%d<i>/%d</i></dd></div>"
            "<div><dt>已执行指令</dt><dd>%s<i>/%s</i></dd></div>"
            "</dl>"
            '<div class="trend">%s</div>'
            '<div class="meta"><span title="%s">%s</span>'
            '<span class="ver">版本 %s</span></div>'
            "%s"
            % (inst, delta, inst, inst,
               latest["branch"], latest["classesHit"], latest["classesTotal"],
               "{:,}".format(latest["covered"]), "{:,}".format(latest["total"]),
               spark(r["history"]),
               _esc(latest["at"].replace("T", " ")), human_age(r["age"]),
               _esc(latest.get("version") or "—"),
               "".join(alerts)))

    links = []
    if r["hasReport"]:
        links.append('<a class="btn" href="%s/current/html/index.html">打开报告</a>'
                     % _url(r["name"]))
        links.append('<a class="btn btn-quiet" href="%s/current/jacoco.xml">jacoco.xml</a>'
                     % _url(r["name"]))
    if r["versions"]:
        vs = "".join('<a class="vtag" href="%s/versions/%s/html/index.html">%s</a>'
                     % (_url(r["name"]), _url(v.get("dir") or v["version"]), _esc(v["version"]))
                     for v in reversed(r["versions"]))
        links.append('<div class="vers"><span>已结算</span>%s</div>' % vs)

    cls = "card"
    if r["unknown"] or r["stale"] or r["breaks"]:
        cls += " card-attn"
    if not r["online"] and not r["unknown"]:
        cls += " card-down"
    return ('<article class="%s">%s%s<div class="links">%s</div></article>'
            % (cls, head, body, "".join(links)))


def build_dashboard_html(rows):
    # 需要注意的排前面：一屏之内先看见问题，而不是按配置顺序一个个找
    def weight(r):
        return (0 if (not r["online"] and not r["unknown"]) else
                1 if (r["unknown"] or r["stale"] or r["breaks"]) else 2, r["name"])

    ordered = sorted(rows, key=weight)
    online = sum(1 for r in rows if r["online"])
    attn = sum(1 for r in rows if r["stale"] or r["breaks"] or r["unknown"])
    down = sum(1 for r in rows if not r["online"] and not r["unknown"])

    tiles = (
        '<div class="tile"><span class="tv">%d</span><span class="tl">服务</span></div>'
        '<div class="tile"><span class="tv ok">%d</span><span class="tl">在线</span></div>'
        '<div class="tile"><span class="tv %s">%d</span><span class="tl">离线</span></div>'
        '<div class="tile"><span class="tv %s">%d</span><span class="tl">需要注意</span></div>'
        % (len(rows), online, "bad" if down else "", down, "warn" if attn else "", attn))

    cards = "\n".join(_card(r) for r in ordered) or (
        '<div class="blank"><h2>还没有配置任何服务</h2>'
        "<p>在配置文件的 services 里加一条，再跑 "
        "<code>covhub.py dump &lt;服务名&gt;</code>。</p></div>")

    values = {
        "{{GENERATED}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{VERSION}}": __version__,
        "{{TILES}}": tiles,
        "{{CARDS}}": cards,
    }
    html = DASHBOARD_TEMPLATE
    for token, value in values.items():
        html = html.replace(token, value)
    return html



DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>覆盖率看板 · coverage-hub</title>
<script>
  // 主题在刷新前先落地，避免每 60 秒自动刷新时闪一下白
  try {
    var t = localStorage.getItem("covhub-theme");
    if (t) { document.documentElement.setAttribute("data-theme", t); }
  } catch (e) {}
</script>
<style>
/* hub 可能部署在内网，不引任何外部字体和 CDN —— 系统字体栈 + 内联 SVG 就够了 */
:root {
  color-scheme: light;
  --bg:      #F4F7F7;
  --panel:   #FFFFFF;
  --sunk:    #EDF2F2;
  --ink:     #0E1A1C;
  --muted:   #5B6E71;
  --faint:   #8B9EA1;
  --line:    #DBE3E4;
  --accent:  #0C7A6C;
  --ok:      #2F8A5B;
  --warn:    #A8761A;
  --bad:     #B24234;
  --shadow:  0 1px 2px rgba(14,26,28,.06), 0 10px 24px -18px rgba(14,26,28,.4);
  --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
          "Microsoft YaHei", "Source Han Sans SC", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg:     #0C1213;
    --panel:  #141B1D;
    --sunk:   #101718;
    --ink:    #E7EEEF;
    --muted:  #90A3A6;
    --faint:  #6A7C7F;
    --line:   #222C2E;
    --accent: #46C4B1;
    --ok:     #58BE86;
    --warn:   #D9A63F;
    --bad:    #DE7365;
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 12px 28px -20px rgba(0,0,0,.9);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:     #0C1213;
  --panel:  #141B1D;
  --sunk:   #101718;
  --ink:    #E7EEEF;
  --muted:  #90A3A6;
  --faint:  #6A7C7F;
  --line:   #222C2E;
  --accent: #46C4B1;
  --ok:     #58BE86;
  --warn:   #D9A63F;
  --bad:    #DE7365;
  --shadow: 0 1px 2px rgba(0,0,0,.5), 0 12px 28px -20px rgba(0,0,0,.9);
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: var(--sans); font-size: 14px; line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1440px; margin: 0 auto; padding: 0 24px 64px; }

/* ---------- 顶栏 ---------- */
header.top {
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  padding: 26px 0 18px; border-bottom: 1px solid var(--line); margin-bottom: 22px;
}
header.top h1 { font-size: 19px; font-weight: 650; margin: 0; letter-spacing: -.01em; }
header.top .ver {
  font-family: var(--mono); font-size: 11px; color: var(--accent);
  border: 1px solid var(--line); border-radius: 3px; padding: 1px 6px;
}
header.top .gen { margin-left: auto; font-size: 12px; color: var(--faint); font-family: var(--mono); }
button.theme {
  background: none; border: 1px solid var(--line); color: var(--muted);
  border-radius: 5px; padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer;
}
button.theme:hover { color: var(--ink); border-color: var(--muted); }
button.theme:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* ---------- 概览 ---------- */
.tiles { display: flex; gap: 28px; flex-wrap: wrap; margin: 0 0 26px; }
.tile { display: flex; align-items: baseline; gap: 8px; }
.tv {
  font-size: 26px; font-weight: 650; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums;
}
.tv.ok { color: var(--ok); } .tv.warn { color: var(--warn); } .tv.bad { color: var(--bad); }
.tl { font-size: 12px; color: var(--muted); }

/* ---------- 卡片 ---------- */
/* min() 是必要的：窄屏上 340px 固定下限会让整页横向溢出 */
.grid { display: grid; gap: 16px;
        grid-template-columns: repeat(auto-fill, minmax(min(340px, 100%), 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 18px 14px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 12px;
}
/* 需要注意的用左边一道色条标出来，不整块染色 —— 免得一屏花掉 */
.card-attn { border-left: 3px solid var(--warn); }
.card-down { border-left: 3px solid var(--bad); }

.card-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.card-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.card-head h2 {
  font-size: 15px; font-weight: 650; margin: 0; letter-spacing: -.005em;
  overflow-wrap: anywhere;
}
.chan {
  font-family: var(--mono); font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--faint); border: 1px solid var(--line); border-radius: 3px; padding: 0 5px;
}
.pill {
  margin-left: auto; font-size: 11.5px; font-weight: 600; white-space: nowrap;
  border-radius: 999px; padding: 2px 10px; border: 1px solid;
}
.pill-ok   { color: var(--ok);   border-color: var(--ok);   background: color-mix(in srgb, var(--ok) 12%, transparent); }
.pill-warn { color: var(--warn); border-color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); }
.pill-bad  { color: var(--bad);  border-color: var(--bad);  background: color-mix(in srgb, var(--bad) 12%, transparent); }

.endpoint {
  font-family: var(--mono); font-size: 11.5px; color: var(--faint);
  margin-top: -8px; overflow-wrap: anywhere;
}

/* 主数字。覆盖率高低**不着色** —— 运行期 13% 不等于「差」，
   按阈值标红只会训练人无视颜色。语义色留给离线/过期/断代那些确定的坏事。 */
.headline { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.big { display: flex; align-items: baseline; gap: 4px; }
.big .num {
  font-size: 34px; font-weight: 660; letter-spacing: -.025em; line-height: 1;
  font-variant-numeric: tabular-nums;
}
.big .pct { font-size: 16px; color: var(--muted); font-weight: 600; }
.big-label { font-size: 12px; color: var(--muted); }
.delta {
  font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums;
  margin-left: 4px; padding: 1px 6px; border-radius: 3px;
}
.delta.up   { color: var(--ok);  background: color-mix(in srgb, var(--ok) 12%, transparent); }
.delta.down { color: var(--muted); background: var(--sunk); }

.meter { height: 6px; border-radius: 999px; background: var(--sunk); overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--accent); border-radius: 999px; }

.figs {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
  margin: 0; padding: 12px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
}
.figs dt { font-size: 11px; color: var(--faint); margin-bottom: 2px; }
.figs dd {
  margin: 0; font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.figs dd i { font-style: normal; font-size: 11.5px; color: var(--faint); font-weight: 500; }

.trend { min-height: 44px; }
.spark { display: block; width: 100%; height: 44px; }
.spark-area { fill: var(--accent); opacity: .1; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 1.6;
              stroke-linejoin: round; stroke-linecap: round;
              /* 图是拉伸铺满的，不加这句描边会跟着横向变形 */
              vector-effect: non-scaling-stroke; }
.spark-dot  { fill: var(--accent); vector-effect: non-scaling-stroke; }
.spark-empty { font-size: 11.5px; color: var(--faint); padding: 14px 0; }

.meta {
  display: flex; gap: 12px; justify-content: space-between; align-items: baseline;
  font-size: 11.5px; color: var(--muted); flex-wrap: wrap;
}
.meta .ver { font-family: var(--mono); color: var(--faint); }

.alert {
  font-size: 12px; border-radius: 6px; padding: 8px 10px; line-height: 1.5;
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  color: color-mix(in srgb, var(--warn) 80%, var(--ink));
  border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
}
.alert b { font-family: var(--mono); font-weight: 600; }

.empty-body {
  display: flex; flex-direction: column; gap: 2px; padding: 22px 0;
  color: var(--muted); font-size: 13px;
}
.empty-body span { font-size: 11.5px; color: var(--faint); }

.links { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: auto; padding-top: 4px; }
.btn {
  font-size: 12px; font-weight: 600; text-decoration: none; border-radius: 6px;
  padding: 5px 12px; background: var(--accent); color: var(--panel); border: 1px solid var(--accent);
}
.btn:hover { filter: brightness(1.08); }
.btn-quiet { background: none; color: var(--muted); border-color: var(--line); font-weight: 500; }
.btn-quiet:hover { color: var(--ink); border-color: var(--muted); filter: none; }
.btn:focus-visible, .vtag:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.vers { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; width: 100%; margin-top: 2px; }
.vers > span { font-size: 11px; color: var(--faint); }
.vtag {
  font-family: var(--mono); font-size: 11px; text-decoration: none;
  color: var(--muted); background: var(--sunk); border-radius: 3px; padding: 1px 6px;
}
.vtag:hover { color: var(--ink); }

.blank {
  grid-column: 1 / -1; background: var(--panel); border: 1px dashed var(--line);
  border-radius: 10px; padding: 40px 24px; text-align: center; color: var(--muted);
}
.blank h2 { font-size: 15px; margin: 0 0 6px; color: var(--ink); }
.blank code { font-family: var(--mono); font-size: 12px; background: var(--sunk); padding: 1px 5px; border-radius: 3px; }

footer.foot {
  margin-top: 30px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 11.5px; color: var(--faint); display: flex; gap: 14px; flex-wrap: wrap;
}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>覆盖率看板</h1>
  <span class="ver">covhub {{VERSION}}</span>
  <span class="gen">{{GENERATED}} · 每 60 秒自动刷新</span>
  <button class="theme" type="button" id="themeBtn">切换主题</button>
</header>

<section class="tiles">{{TILES}}</section>

<main class="grid">
{{CARDS}}
</main>

<footer class="foot">
  <span>数字是运行期真实执行到的代码，不代表测试是否有效</span>
  <span>报告随采集重新生成，exec 与 class 产物是不可再生资产</span>
</footer>

</div>
<script>
  document.getElementById("themeBtn").addEventListener("click", function () {
    var root = document.documentElement;
    var dark = root.getAttribute("data-theme") === "dark" ||
               (!root.getAttribute("data-theme") &&
                window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("covhub-theme", next); } catch (e) {}
  });
</script>
</body>
</html>
"""
