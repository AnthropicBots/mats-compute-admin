"""
compute_admin/cli.py
MATS Compute Admin — Command-line interface.

Usage:
    python -m compute_admin.cli --help
    python -m compute_admin.cli scholars list
    python -m compute_admin.cli budget alerts
    python -m compute_admin.cli cluster status
    python -m compute_admin.cli audit run
    python -m compute_admin.cli requests queue
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# ──────────────────────────────────────────────
# Lazy imports (keep startup fast)
# ──────────────────────────────────────────────

def _registry():
    from compute_admin.scholar_manager import ScholarRegistry
    return ScholarRegistry()

def _budget():
    from compute_admin.budget_tracker import BudgetTracker
    return BudgetTracker()

def _keys():
    from compute_admin.api_key_manager import APIKeyManager
    return APIKeyManager()

def _cluster():
    from compute_admin.cluster_monitor import ClusterMonitor
    return ClusterMonitor()

def _requests():
    from compute_admin.request_handler import RequestHandler
    return RequestHandler()

def _auditor():
    from compute_admin.platform_auditor import PlatformAuditor
    return PlatformAuditor()


# ──────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────

def _table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("  (no results)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep    = "  ".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _json_out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ──────────────────────────────────────────────
# Sub-commands
# ──────────────────────────────────────────────

# ── scholars ────────────────────────────────

def cmd_scholars(args) -> int:
    reg = _registry()
    if args.scholars_cmd == "list":
        scholars = reg.list_all(
            cohort=args.cohort if hasattr(args, "cohort") else None,
        )
        rows = [
            {
                "ID": s.scholar_id,
                "Name": s.name,
                "Role": s.role.value,
                "Status": s.status.value,
                "Cohort": s.cohort,
                "Platforms": len(s.platform_access),
                "Spent $": f"{s.total_spent():.2f}",
                "Budget $": f"{s.total_budget():.2f}",
            }
            for s in scholars
        ]
        _table(rows, ["ID", "Name", "Role", "Status", "Cohort", "Platforms", "Spent $", "Budget $"])

    elif args.scholars_cmd == "show":
        s = reg.get(args.id)
        print(f"\nScholar: {s.name}  ({s.scholar_id})")
        print(f"  Email   : {s.email}")
        print(f"  Role    : {s.role.value}")
        print(f"  Status  : {s.status.value}")
        print(f"  Cohort  : {s.cohort}")
        print(f"  Created : {s.created_at[:10]}")
        print(f"\n  Platform Access:")
        for p in s.platform_access:
            tag = "✅" if not p.revoked_at else "❌"
            print(f"    {tag} {p.platform.value:15s}  ${p.spent_usd:.2f} / ${p.budget_usd:.2f}  ({p.utilisation_pct:.1f}%)")

    elif args.scholars_cmd == "offboard":
        s = reg.offboard(args.id, reason=args.reason or "")
        print(f"✅ Offboarded: {s.name} ({s.scholar_id})")

    return 0


# ── budget ───────────────────────────────────

def cmd_budget(args) -> int:
    reg = _registry()
    bt = _budget()

    if args.budget_cmd == "alerts":
        alerts = bt.check_alerts(reg)
        if not alerts:
            print("✅ No budget alerts — all scholars within threshold.")
            return 0
        rows = [
            {
                "Level": a.level,
                "Scholar": a.scholar_name,
                "Platform": a.platform,
                "Spent $": f"{a.spent_usd:.2f}",
                "Budget $": f"{a.budget_usd:.2f}",
                "Usage %": f"{a.utilisation_pct:.1f}",
            }
            for a in alerts
        ]
        _table(rows, ["Level", "Scholar", "Platform", "Spent $", "Budget $", "Usage %"])

    elif args.budget_cmd == "report":
        rows = bt.cohort_report(reg, cohort=getattr(args, "cohort", None))
        _table(rows, ["name", "platform", "budget_usd", "spent_usd", "remaining_usd", "utilisation_pct", "status"])

    elif args.budget_cmd == "platform-summary":
        summary = bt.platform_summary(reg)
        rows = [
            {"Platform": k, "Budget $": f"{v['budget_usd']:.2f}", "Spent $": f"{v['spent_usd']:.2f}", "Scholars": v["scholars"], "Usage %": f"{v['utilisation_pct']:.1f}"}
            for k, v in sorted(summary.items())
        ]
        _table(rows, ["Platform", "Budget $", "Spent $", "Scholars", "Usage %"])

    return 0


# ── cluster ───────────────────────────────────

def cmd_cluster(args) -> int:
    mon = _cluster()
    if args.cluster_cmd == "status":
        snap = mon.health_snapshot()
        print(f"\n🖥️  MATS Cluster Health — {snap['generated_at'][:19]}  [mode: {snap['mode']}]")
        print(f"  Running jobs   : {snap['running_jobs']}")
        print(f"  Pending jobs   : {snap['pending_jobs']}")
        print(f"  Stalled jobs   : {snap['stalled_jobs']}")
        print(f"  Failed (recent): {snap['failed_jobs_recent']}")
        print(f"  GPUs in use    : {snap['gpus_in_use']}")
        if snap["stalled_details"]:
            print(f"\n  ⚠️  Stalled jobs:")
            for j in snap["stalled_details"]:
                print(f"    JobID {j['job_id']}  user={j['user']}  elapsed={j['elapsed_h']:.1f}h")
        if snap["top_gpu_consumers"]:
            print("\n  📊 Top GPU-hour consumers:")
            for uid, hrs in snap["top_gpu_consumers"]:
                print(f"    {uid:20s}  {hrs:.1f} GPU-h")

    elif args.cluster_cmd == "stalled":
        jobs = mon.stalled_jobs()
        if not jobs:
            print("✅ No stalled jobs.")
            return 0
        rows = [{"JobID": j.job_id, "User": j.username, "Name": j.name, "GPUs": j.gpus, "Elapsed h": f"{j.elapsed_hours:.1f}"} for j in jobs]
        _table(rows, ["JobID", "User", "Name", "GPUs", "Elapsed h"])

    elif args.cluster_cmd == "failed":
        jobs = mon.failed_jobs()
        if not jobs:
            print("✅ No recent failed jobs.")
            return 0
        rows = [{"JobID": j.job_id, "User": j.username, "State": j.state.value, "ExitCode": j.exit_code, "GPUs": j.gpus} for j in jobs]
        _table(rows, ["JobID", "User", "State", "ExitCode", "GPUs"])

    return 0


# ── requests ─────────────────────────────────

def cmd_requests(args) -> int:
    rh = _requests()
    if args.requests_cmd == "queue":
        queue = rh.open_queue()
        if not queue:
            print("✅ Request queue is empty.")
            return 0
        rows = [
            {
                "ID": r.request_id,
                "Scholar": r.scholar_name,
                "Type": r.request_type.value,
                "Priority": r.priority.value,
                "Age h": f"{r.age_hours:.1f}",
                "SLA": "⚠️ BREACHED" if r.is_sla_breached else "✅ OK",
            }
            for r in queue
        ]
        _table(rows, ["ID", "Scholar", "Type", "Priority", "Age h", "SLA"])

    elif args.requests_cmd == "stats":
        _json_out(rh.queue_stats())

    return 0


# ── audit ─────────────────────────────────────

def cmd_audit(args) -> int:
    if args.audit_cmd == "run":
        reg = _registry()
        km  = _keys()
        bt  = _budget()
        aud = _auditor()
        report = aud.run_full_audit(registry=reg, key_manager=km, budget_tracker=bt)
        print(f"\n🔒 Audit Complete — {report.generated_at[:10]}  (ID: {report.report_id})")
        print(f"   Findings: {report.summary['total_findings']} total")
        print(f"   🔴 Critical: {report.summary['critical']}")
        print(f"   🟠 High    : {report.summary['high']}")
        print(f"   🟡 Medium  : {report.summary['medium']}")
        print(f"   🟢 Low     : {report.summary['low']}")
        if report.findings:
            print("\n   Top Issues:")
            for fn in report.findings[:5]:
                print(f"     [{fn.severity}] {fn.description}")
        print(f"\n   📋 Slack digest preview:")
        print(aud.slack_digest(report))

    return 0


# ──────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mats-admin",
        description="MATS Compute Administrator CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # scholars
    sc = sub.add_parser("scholars", help="Scholar management")
    sc_sub = sc.add_subparsers(dest="scholars_cmd", required=True)
    sc_list = sc_sub.add_parser("list", help="List all scholars")
    sc_list.add_argument("--cohort", default=None)
    sc_show = sc_sub.add_parser("show", help="Show scholar detail")
    sc_show.add_argument("id")
    sc_off = sc_sub.add_parser("offboard", help="Offboard a scholar")
    sc_off.add_argument("id")
    sc_off.add_argument("--reason", default="")

    # budget
    bc = sub.add_parser("budget", help="Budget tracking")
    bc_sub = bc.add_subparsers(dest="budget_cmd", required=True)
    bc_sub.add_parser("alerts", help="Show active budget alerts")
    bc_rep = bc_sub.add_parser("report", help="Full cohort budget report")
    bc_rep.add_argument("--cohort", default=None)
    bc_sub.add_parser("platform-summary", help="Per-platform budget summary")

    # cluster
    cc = sub.add_parser("cluster", help="HPC cluster monitoring")
    cc_sub = cc.add_subparsers(dest="cluster_cmd", required=True)
    cc_sub.add_parser("status", help="Cluster health snapshot")
    cc_sub.add_parser("stalled", help="List stalled jobs")
    cc_sub.add_parser("failed", help="List recent failed jobs")

    # requests
    rc = sub.add_parser("requests", help="Compute request queue")
    rc_sub = rc.add_subparsers(dest="requests_cmd", required=True)
    rc_sub.add_parser("queue", help="Open request queue")
    rc_sub.add_parser("stats", help="Queue statistics")

    # audit
    ac = sub.add_parser("audit", help="Security & compliance audit")
    ac_sub = ac.add_subparsers(dest="audit_cmd", required=True)
    ac_sub.add_parser("run", help="Run full security audit")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "scholars": cmd_scholars,
        "budget":   cmd_budget,
        "cluster":  cmd_cluster,
        "requests": cmd_requests,
        "audit":    cmd_audit,
    }
    fn = dispatch.get(args.command)
    if fn:
        sys.exit(fn(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
