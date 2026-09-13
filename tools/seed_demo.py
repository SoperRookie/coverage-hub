"""往 hub 里灌一套演示数据：两个项目、各 10 个微服务、每个服务 3–5 个版本。

    python tools/seed_demo.py            # 用 covhub.yaml 指向的库与 dataDir
    python tools/seed_demo.py --reset    # 先删掉上次灌的演示项目 / 服务 / 数据目录再灌
    python tools/seed_demo.py --seed 7   # 换个随机种子，数字会变，结构不变

造出来的东西和真实采集落下的形态一致：库里有 snapshots / archives / unit_reports / diffs，
磁盘上每个版本有 jacoco.xml、html/、incremental.json（带源码片段）、manifest.json，
diff/ 与 unit/ 也齐全 —— 看板的历史版本、新增代码源码、历史对比、项目报表都能点开。
被测服务本身并不存在（127.0.0.1 上没人监听的端口），看板上是「离线」；
「立即采集 / 结算归档」按钮会因此报 409，这是预期的。
"""
import argparse
import json
import math
import os
import random
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from covhub import build, incremental as inc  # noqa: E402
from covhub.config import load_config, resolve_config_path  # noqa: E402
from covhub.db import repo  # noqa: E402
from covhub.db.engine import session_scope  # noqa: E402
from covhub.db.models import Diff, UnitReport  # noqa: E402
from covhub.layout import ensure_dirs, svc_dir  # noqa: E402
from covhub.runtime import prepare_database  # noqa: E402
from covhub.schemas import ServiceSpec  # noqa: E402

# ---- 业务：商城购物 + 充值支付，各 10 个服务。(服务名, 显示说明, 包名, 类名列表) ----

MALL = ("mall", "商城购物", "商品浏览 → 购物车 → 下单 → 库存 / 促销 → 配送 → 通知的主链路", [
    ("mall-gateway",      "网关", "com.mall.gateway",   ["AuthFilter", "RateLimitFilter", "RouteConfig", "GatewayExceptionHandler", "TraceFilter"]),
    ("user-service",      "用户", "com.mall.user",      ["UserController", "UserService", "UserRepository", "AddressService", "LoginService", "TokenManager", "UserAssembler"]),
    ("product-service",   "商品", "com.mall.product",   ["ProductController", "ProductService", "SkuService", "CategoryService", "ProductRepository", "PriceCalculator", "ProductCache"]),
    ("search-service",    "搜索", "com.mall.search",    ["SearchController", "SearchService", "IndexBuilder", "QueryParser", "RankScorer", "SuggestService"]),
    ("cart-service",      "购物车", "com.mall.cart",    ["CartController", "CartService", "CartItemMerger", "CartRepository", "CartPricing"]),
    ("order-service",     "订单", "com.mall.order",     ["OrderController", "OrderService", "OrderStateMachine", "OrderRepository", "OrderAssembler", "OrderTimeoutJob", "OrderQueryService", "SplitOrderService"]),
    ("inventory-service", "库存", "com.mall.inventory", ["StockController", "StockService", "StockLockService", "StockRepository", "StockSyncJob", "WarehouseRouter"]),
    ("promotion-service", "促销", "com.mall.promotion", ["CouponController", "CouponService", "DiscountEngine", "RuleLoader", "PromotionRepository", "SeckillService"]),
    ("delivery-service",  "配送", "com.mall.delivery",  ["ShipmentController", "ShipmentService", "CarrierAdapter", "TrackingSync", "DeliveryRepository", "FreightCalculator"]),
    ("notify-service",    "通知", "com.mall.notify",    ["NotifyController", "SmsSender", "EmailSender", "PushSender", "TemplateRenderer", "NotifyRetryJob"]),
])

PAY = ("payment", "充值支付", "充值 → 账户 → 支付核心 → 渠道 → 风控 → 清结算 / 退款 / 账务", [
    ("pay-gateway",        "支付网关", "com.pay.gateway",    ["SignVerifyFilter", "IdempotentFilter", "PayRouteConfig", "CallbackController", "ReplayGuard"]),
    ("account-service",    "账户", "com.pay.account",       ["AccountController", "AccountService", "BalanceService", "AccountRepository", "FreezeService", "AccountAssembler"]),
    ("recharge-service",   "充值", "com.pay.recharge",      ["RechargeController", "RechargeService", "RechargeOrderRepository", "RechargeCallbackHandler", "RechargeLimitChecker", "RechargeQueryService"]),
    ("wallet-service",     "钱包", "com.pay.wallet",        ["WalletController", "WalletService", "WalletTxRepository", "WalletPasswordService", "WalletLimitService"]),
    ("payment-core",       "支付核心", "com.pay.core",      ["PaymentController", "PaymentService", "PaymentStateMachine", "PaymentRepository", "RoutingService", "PaymentTimeoutJob", "PaymentNotifier", "CombinePayService"]),
    ("channel-adapter",    "渠道对接", "com.pay.channel",   ["AlipayClient", "WechatPayClient", "UnionPayClient", "ChannelRouter", "ChannelCallbackParser", "ChannelHealthChecker"]),
    ("risk-service",       "风控", "com.pay.risk",          ["RiskController", "RiskEngine", "RuleEvaluator", "DeviceFingerprint", "BlacklistService", "RiskDecisionLog"]),
    ("settlement-service", "清结算", "com.pay.settlement",  ["SettlementJob", "SettlementService", "SettleBatchRepository", "MerchantFeeCalculator", "SettleFileExporter"]),
    ("refund-service",     "退款", "com.pay.refund",        ["RefundController", "RefundService", "RefundStateMachine", "RefundRepository", "RefundCallbackHandler"]),
    ("ledger-service",     "账务对账", "com.pay.ledger",    ["LedgerService", "JournalRepository", "ReconcileJob", "ReconcileDiffHandler", "ChannelBillFetcher", "LedgerExporter"]),
])

# 每个服务的版本号序列（3–5 个），最后一个是当前在跑、还没结算的版本
VERSION_TRACKS = [
    ["1.0.0", "1.1.0", "1.2.0"],
    ["2.3.0", "2.3.1", "2.4.0", "2.5.0"],
    ["1.8.0", "1.9.0", "2.0.0", "2.0.1", "2.1.0"],
    ["3.0.0", "3.1.0", "3.1.1", "3.2.0"],
    ["0.9.0", "1.0.0", "1.0.1", "1.1.0", "1.2.0"],
]

# 假 Java 源码：按模板随机拼，够看，不求能编译
STMTS = [
    "log.info(\"{m} start, id={{}}\", id);", "if (req == null) {{ throw new IllegalArgumentException(\"req\"); }}",
    "var entity = repository.findById(id).orElseThrow();", "entity.setUpdatedAt(Instant.now());",
    "return assembler.toDto(entity);", "if (!validator.check(req)) {{ return Result.fail(\"INVALID\"); }}",
    "var result = client.call(req.toRemote());", "cache.put(key, result, Duration.ofMinutes(5));",
    "metrics.counter(\"{m}\").increment();", "return Result.ok(result);",
    "for (var item : items) {{ total = total.add(item.amount()); }}", "if (total.signum() <= 0) {{ return Result.fail(\"EMPTY\"); }}",
    "publisher.publish(new {c}Event(id, total));", "repository.save(entity);",
    "try {{ client.ping(); }} catch (RemoteException e) {{ log.warn(\"{m} remote failed\", e); return Result.fail(\"REMOTE\"); }}",
    "var lock = lockService.tryLock(key, 3, TimeUnit.SECONDS);", "if (lock == null) {{ return Result.fail(\"BUSY\"); }}",
    "state = machine.fire(state, Event.{M});", "audit.record(id, state);",
]
METHODS = ["create", "query", "update", "cancel", "confirm", "pay", "refund", "sync", "lock", "release", "settle", "notify", "route", "verify"]


def gen_source(pkg, cls, rnd):
    """生成一份假源码：返回 (行文本列表, 有探针的行号集合)。空行 / 注释 / 声明没探针。"""
    lines = ["package %s;" % pkg, "", "import java.time.Instant;", "import org.springframework.stereotype.Service;", "",
             "@Service", "public class %s {" % cls, "", "    private final %sRepository repository;" % cls.replace("Service", "").replace("Controller", ""),
             "    private final MetricRegistry metrics;", ""]
    probes = set()
    n_methods = rnd.randint(3, 7)
    for _ in range(n_methods):
        m = rnd.choice(METHODS) + rnd.choice(["", "Batch", "ById", "Async"])
        lines.append("    /** %s：%s */" % (m, rnd.choice(["幂等", "带重试", "同步渠道", "校验参数", "写审计"])))
        lines.append("    public Result<%sDto> %s(Long id, %sRequest req) {" % (cls, m, cls))
        for _ in range(rnd.randint(4, 12)):
            stmt = rnd.choice(STMTS).format(m=m, c=cls, M=m.upper())
            lines.append("        " + stmt)
            probes.add(len(lines))
        lines.append("    }")
        probes.add(len(lines))            # 方法末尾的隐式 return 也有探针
        lines.append("")
    lines.append("}")
    return lines, probes


def jacoco_xml(pkg_path, files, coverage):
    """files: {File.java: (lines, probes)}；coverage: {File.java: set(命中行号)}。写成 hub 出的那种 report > package 结构。"""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<report name="%s">' % pkg_path, '  <package name="%s">' % pkg_path]
    tot = {"INSTRUCTION": [0, 0], "BRANCH": [0, 0], "LINE": [0, 0], "CLASS": [0, 0], "METHOD": [0, 0]}
    for fname, (lines, probes) in files.items():
        hit = coverage[fname]
        cls_hit = any(nr in hit for nr in probes)
        out.append('    <class name="%s/%s" sourcefilename="%s">' % (pkg_path, fname[:-5], fname))
        out.append('      <counter type="CLASS" missed="%d" covered="%d"/>' % (0 if cls_hit else 1, 1 if cls_hit else 0))
        out.append('    </class>')
        out.append('    <sourcefile name="%s">' % fname)
        f_ins = [0, 0]
        f_br = [0, 0]
        f_ln = [0, 0]
        for nr in sorted(probes):
            weight = 1 + (nr * 7) % 6           # 每行 1–6 条指令，确定性的
            branchy = nr % 5 == 0
            if nr in hit:
                mi, ci = 0, weight
                mb, cb = (1, 1) if branchy else (0, 0)
                f_ln[1] += 1
            else:
                mi, ci = weight, 0
                mb, cb = (2, 0) if branchy else (0, 0)
                f_ln[0] += 1
            f_ins[0] += mi; f_ins[1] += ci; f_br[0] += mb; f_br[1] += cb
            out.append('      <line nr="%d" mi="%d" ci="%d" mb="%d" cb="%d"/>' % (nr, mi, ci, mb, cb))
        for kind, (m, c) in (("INSTRUCTION", f_ins), ("BRANCH", f_br), ("LINE", f_ln)):
            out.append('      <counter type="%s" missed="%d" covered="%d"/>' % (kind, m, c))
            tot[kind][0] += m; tot[kind][1] += c
        tot["CLASS"][1 if cls_hit else 0] += 1
        tot["METHOD"][1 if cls_hit else 0] += 3
        out.append('    </sourcefile>')
    for kind in ("INSTRUCTION", "BRANCH", "LINE", "METHOD", "CLASS"):
        out.append('    <counter type="%s" missed="%d" covered="%d"/>' % (kind, tot[kind][0], tot[kind][1]))
    out.append('  </package>')
    for kind in ("INSTRUCTION", "BRANCH", "LINE", "METHOD", "CLASS"):
        out.append('  <counter type="%s" missed="%d" covered="%d"/>' % (kind, tot[kind][0], tot[kind][1]))
    out.append('</report>')
    return "\n".join(out) + "\n", tot


def html_report(dest, title, pkg_path, files, coverage, tot):
    """占位用的 HTML 报告：首页 + 每个文件一页（看板里「在 JaCoCo 报告里看整个文件」链接指到这里）。"""
    os.makedirs(os.path.join(dest, pkg_path.replace("/", ".")), exist_ok=True)
    ins = tot["INSTRUCTION"]
    pct = ins[1] * 100.0 / (ins[0] + ins[1]) if ins[0] + ins[1] else 0
    rows = []
    for fname, (lines, probes) in files.items():
        hit = coverage[fname]
        c = sum(1 for nr in probes if nr in hit)
        rows.append('<tr><td><a href="%s/%s.html">%s</a></td><td>%d / %d</td></tr>' % (pkg_path.replace("/", "."), fname, fname, c, len(probes)))
        body = []
        for i, text in enumerate(lines, 1):
            cls = "fc" if i in hit else ("nc" if i in probes else "")
            body.append('<span class="%s" id="L%d">%4d  %s</span>' % (cls, i, i, text.replace("&", "&amp;").replace("<", "&lt;")))
        with open(os.path.join(dest, pkg_path.replace("/", "."), fname + ".html"), "w", encoding="utf-8") as f:
            f.write('<!doctype html><meta charset="utf-8"><title>%s</title><style>body{font:13px/1.5 Consolas,monospace;padding:16px}'
                    'span{display:block;white-space:pre}.fc{background:#c8e6c9}.nc{background:#ffcdd2}</style>'
                    '<h3>%s</h3><p><a href="../index.html">← %s</a>（演示数据）</p>%s' % (fname, fname, title, "\n".join(body)))
    with open(os.path.join(dest, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>%s</title><style>body{font:14px system-ui;padding:24px}td{padding:4px 12px}</style>'
                '<h2>%s</h2><p>演示数据 · 指令覆盖 %.1f%%（%d / %d）</p><table>%s</table>'
                % (title, title, pct, ins[1], ins[0] + ins[1], "".join(rows)))


def make_diff(pkg_path, changes):
    """changes: {File.java: (旧行数, [新增行号], 行文本)} → -U0 风格的 unified diff（新增行连成 hunk）。"""
    out = []
    for fname, (old_len, added, lines) in changes.items():
        rel = "src/main/java/%s/%s" % (pkg_path, fname)
        out.append("diff --git a/%s b/%s" % (rel, rel))
        out.append("--- a/%s" % rel)
        out.append("+++ b/%s" % rel)
        runs = []
        for nr in sorted(added):
            if runs and nr == runs[-1][-1] + 1:
                runs[-1].append(nr)
            else:
                runs.append([nr])
        shift = 0
        for run in runs:
            start = run[0]
            out.append("@@ -%d,0 +%d,%d @@" % (start - 1 - shift, start, len(run)))
            for nr in run:
                out.append("+" + lines[nr - 1])
            shift += len(run)
    return "\n".join(out) + "\n"


def snapshot_entry(at, kind, version, hit_ratio, tot_final, inc_final, classes_total):
    """按「本周期已跑到的比例」缩放出一条快照的数字。"""
    ins_total = tot_final["INSTRUCTION"][0] + tot_final["INSTRUCTION"][1]
    covered = int(round(tot_final["INSTRUCTION"][1] * hit_ratio))
    br_total = tot_final["BRANCH"][0] + tot_final["BRANCH"][1]
    br_cov = int(round(tot_final["BRANCH"][1] * hit_ratio))
    entry = {"at": at.isoformat(timespec="seconds"), "kind": kind, "version": version,
             "instruction": round(covered * 100.0 / ins_total, 2) if ins_total else 0.0,
             "branch": round(br_cov * 100.0 / br_total, 2) if br_total else 0.0,
             "covered": covered, "total": ins_total,
             "classesHit": min(classes_total, int(math.ceil(tot_final["CLASS"][1] * min(1.0, hit_ratio + 0.15)))),
             "classesTotal": classes_total}
    if inc_final is not None:
        c = int(round(inc_final["covered"] * hit_ratio))
        entry.update({"incCovered": c, "incTotal": inc_final["total"],
                      "incPct": round(c * 100.0 / inc_final["total"], 2) if inc_final["total"] else None})
    return entry


def seed_service(cfg, proj, idx, spec, rnd, now):
    name, label, pkg, classes = spec
    pkg_path = pkg.replace(".", "/")
    versions = VERSION_TRACKS[(idx + (0 if proj == "mall" else 2)) % len(VERSION_TRACKS)]
    port = 6400 + (0 if proj == "mall" else 20) + idx + 1
    latest = versions[-1]

    fields = ServiceSpec(name=name, project=proj, version=latest, address="127.0.0.1", port=port,
                         includes=[pkg + ".*"], excludes=[pkg + ".config.*"],
                         classfiles=["./data/%s/artifacts/%s" % (name, latest)],
                         classDumpDir="/tmp/covhub-classes/%s" % name).to_fields()
    repo.upsert_service(fields, overwrite=True)
    svc = repo.get_service(name, cfg)
    root = ensure_dirs(cfg, svc)

    # 每个版本的源码：在上一版基础上给几个文件加几行（这就是 diff 的来源）
    sources = {}
    base_rate = {c: rnd.uniform(0.25, 0.95) for c in classes}
    unit_rate = {c: min(0.98, base_rate[c] * rnd.uniform(0.6, 1.3)) for c in classes}
    for c in classes:
        lines, probes = gen_source(pkg, c, rnd)
        sources[c + ".java"] = [lines, probes]

    # 时间线：第一个版本从 ~75 天前开始，每个周期 8–18 天；最后一个版本是当前周期
    cycle_days = [rnd.uniform(8, 18) for _ in versions]
    start = now - timedelta(days=sum(cycle_days[:-1]) + rnd.uniform(3, 10))
    prev_head = None
    for vi, version in enumerate(versions):
        is_current = vi == len(versions) - 1
        cycle_start = start
        cycle_end = now if is_current else start + timedelta(days=cycle_days[vi])
        start = cycle_end

        # 这一版的改动：2–5 个文件各插入 1–3 段新增行
        changes = {}
        if vi > 0:
            for fname in rnd.sample(list(sources), rnd.randint(2, min(5, len(sources)))):
                lines, probes = sources[fname]
                old_len = len(lines)
                # 在旧行号 pos 之后插一段：先定好所有插入点，再从头走一遍重排行号
                inserts = {}
                for _ in range(rnd.randint(1, 3)):
                    pos = rnd.randint(12, old_len - 2)
                    m = rnd.choice(METHODS)
                    block = ["        " + rnd.choice(STMTS).format(m=m, c=fname[:-5], M=m.upper()) for _ in range(rnd.randint(1, 5))]
                    if rnd.random() < 0.3:
                        block.insert(0, "        // %s：%s" % (version, rnd.choice(["兼容旧渠道", "补幂等", "临时开关", "修 NPE"])))
                    inserts.setdefault(pos, []).extend(block)
                new_lines, new_probes, added = [], set(), []
                for old_nr, text in enumerate(lines, 1):
                    new_lines.append(text)
                    if old_nr in probes:
                        new_probes.add(len(new_lines))
                    for t in inserts.get(old_nr, []):
                        new_lines.append(t)
                        added.append(len(new_lines))
                        if not t.strip().startswith("//"):
                            new_probes.add(len(new_lines))
                sources[fname] = [new_lines, new_probes]
                changes[fname] = (old_len, added, new_lines)

        files = {f: (l, p) for f, (l, p) in sources.items()}
        # 运行时命中的行：按文件基础命中率抽；新加的行命中率略低（新代码没跑到的多）
        run_hit = {}
        for fname, (lines, probes) in files.items():
            rate = base_rate[fname[:-5]] * rnd.uniform(0.9, 1.05)
            added = set(changes.get(fname, (0, [], None))[1])
            run_hit[fname] = {nr for nr in probes if rnd.random() < (rate * 0.7 if nr in added else rate)}
        unit_hit = {}
        for fname, (lines, probes) in files.items():
            rate = unit_rate[fname[:-5]]
            unit_hit[fname] = {nr for nr in probes if rnd.random() < rate}

        rt_xml, rt_tot = jacoco_xml(pkg_path, files, run_hit)
        report_dir = os.path.join(root, "current" if is_current else "versions/%s" % version)
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "jacoco.xml"), "w", encoding="utf-8") as f:
            f.write(rt_xml)
        html_report(os.path.join(report_dir, "html"), "%s %s" % (name, version), pkg_path, files, run_hit, rt_tot)

        # diff → 走真实的解析 / 入库路径
        inc_final = None
        if changes:
            head = "%08x" % rnd.getrandbits(32)
            text = make_diff(pkg_path, changes)
            build.store_diff(cfg, svc, version, prev_head or "v%s" % versions[vi - 1], head, text)
            prev_head = head
            diff_lines = build.load_diff_lines(cfg, svc, version)
            parsed = inc.parse_jacoco(os.path.join(report_dir, "jacoco.xml"))
            result = inc.compute(diff_lines, parsed["files"])
            result["version"] = version
            _snippets(result, pkg_path, files)
            inc.write_json(os.path.join(report_dir, "incremental.json"), result)
            inc_final = result
            _set_at(Diff, name, version, cycle_start - timedelta(hours=rnd.uniform(2, 30)))

        # 单测报告（少数版本故意缺一份，看板上能看到「—」）
        if rnd.random() > 0.12:
            ut_xml, _ = jacoco_xml(pkg_path, files, unit_hit)
            tmp = os.path.join(root, "unit", "_seed.xml")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(ut_xml)
            build.store_unit_report(cfg, svc, version, tmp)
            os.unlink(tmp)
            if inc_final is not None:
                # store_unit_report 只在 svc.version == version 时存片段，历史版本这里补上
                unit_dir = os.path.join(root, "unit", version)
                r = inc.read_json(os.path.join(unit_dir, "incremental.json"))
                if r:
                    _snippets(r, pkg_path, files)
                    inc.write_json(os.path.join(unit_dir, "incremental.json"), r)
            _set_at(UnitReport, name, version, cycle_start - timedelta(hours=rnd.uniform(1, 20)))

        # 快照：周期内每 4–9 小时一条 watch，覆盖率沿饱和曲线爬升
        classes_total = len(files)
        span_h = (cycle_end - cycle_start).total_seconds() / 3600
        t = cycle_start + timedelta(hours=rnd.uniform(0.5, 2))
        snapshots = []
        while t < cycle_end:
            frac = (t - cycle_start).total_seconds() / 3600 / span_h
            ratio = 0.15 + 0.85 * (1 - math.exp(-4.5 * frac)) + rnd.uniform(-0.02, 0.02)
            snapshots.append((t, "watch" if rnd.random() > 0.15 else "dump", ratio))
            t += timedelta(hours=rnd.uniform(4, 9))
        if is_current:
            # 最后一条放在几分钟前，看板上别一上来全是「采集停了」
            snapshots.append((now - timedelta(minutes=rnd.uniform(1, 9)), "watch", snapshots[-1][2] if snapshots else 0.5))
        for at, kind, ratio in snapshots[-40:]:
            repo.add_snapshot(name, snapshot_entry(at, kind, version, min(0.98, ratio), rt_tot, inc_final, classes_total))

        if not is_current:
            entry = snapshot_entry(cycle_end, "predeploy", version, 1.0, rt_tot, inc_final, classes_total)
            row = repo.add_snapshot(name, entry)
            exec_count = len(snapshots) + 1
            match_rate = 100.0 if rnd.random() > 0.15 else round(rnd.uniform(93, 99.5), 1)
            manifest = {"service": name, "version": version, "sealedAt": entry["at"], "sealedBy": "predeploy",
                        "summary": entry, "classfiles": ["./data/%s/artifacts/%s" % (name, version)],
                        "fingerprint": "%016x" % rnd.getrandbits(64), "execCount": exec_count, "matchRate": match_rate,
                        "healthVerdict": "正常" if match_rate == 100.0 else "部分类指纹不匹配", "merged": "merged.exec",
                        "note": "演示数据，由 tools/seed_demo.py 生成"}
            with open(os.path.join(report_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            repo.finish_archive(name, snapshot_id=row["id"], version=version, archive_dir="versions/%s" % version,
                                sealed_by="predeploy", sealed_at=entry["at"], fingerprint=manifest["fingerprint"],
                                exec_count=exec_count, match_rate=match_rate,
                                health_verdict=manifest["healthVerdict"], merged="merged.exec")
    return len(versions)


def _snippets(result, pkg_path, files):
    for path, entry in result.get("files", {}).items():
        fname = path.rsplit("/", 1)[-1]
        if fname not in files or not entry.get("added"):
            continue
        lines = files[fname][0]
        wanted = set()
        for nr in entry["added"]:
            for k in range(nr - build.SNIPPET_CONTEXT, nr + build.SNIPPET_CONTEXT + 1):
                if 1 <= k <= len(lines):
                    wanted.add(k)
        entry["snippets"] = {str(nr): lines[nr - 1] for nr in sorted(wanted)}


def _set_at(model, name, version, at):
    """store_* 用的是 now()，演示数据要把时刻改回时间线上。"""
    from sqlalchemy import select
    from covhub.db.models import Service
    with session_scope() as s:
        sid = s.scalar(select(Service.id).where(Service.name == name))
        row = s.scalar(select(model).where(model.service_id == sid, model.version == version))
        if row is not None:
            row.at = at.replace(microsecond=0)


def reset(cfg, projects):
    for pname, _, _, services in projects:
        for spec in services:
            try:
                svc = repo.get_service(spec[0], cfg)
            except Exception:
                svc = None
            if svc:
                shutil.rmtree(svc_dir(cfg, svc), ignore_errors=True)
                repo.remove_service(spec[0])
                print("  已删服务 %s" % spec[0])
        try:
            repo.remove_project(pname)
            print("  已删项目 %s" % pname)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config", help="hub 配置文件（默认按 covhub.yaml → covhub.json 探测）")
    ap.add_argument("--reset", action="store_true", help="先删掉已有的演示项目 / 服务及其数据目录")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    cfg_path = resolve_config_path(args.config)
    cfg = load_config(cfg_path)
    prepare_database(cfg)
    rnd = random.Random(args.seed)
    now = datetime.now().replace(microsecond=0)
    projects = [MALL, PAY]

    if args.reset:
        print("清理旧的演示数据…")
        reset(cfg, projects)

    for pname, title, desc, services in projects:
        try:
            repo.add_project({"name": pname, "title": title, "description": desc})
        except Exception:
            repo.update_project(pname, {"title": title, "description": desc})
        print("项目 %s（%s）" % (pname, title))
        for idx, spec in enumerate(services):
            n = seed_service(cfg, pname, idx, spec, rnd, now)
            print("  %-20s %s，%d 个版本" % (spec[0], spec[1], n))
    print("完成。hub 不用重启，刷新看板即可。")


if __name__ == "__main__":
    main()
