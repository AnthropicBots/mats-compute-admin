"""
compute_admin/budget_tracker.py
MATS Compute Admin — Real-time budget tracking, alerting, and reporting.

Aggregates spend data from multiple platforms, fires configurable alerts,
and generates per-scholar / per-cohort / platform-level reports.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Spend event log (append-only audit trail)
# ──────────────────────────────────────────────

@dataclass
class SpendEvent:
    scholar_id: str
    scholar_name: str
    platform: str
    amount_usd: float
    description: str
    recorded_at: str = field(default_factory=lambda: _now())
    event_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.event_id is None:
            import uuid
            self.event_id = str(uuid.uuid4())[:12]


@dataclass
class BudgetAlert:
    level: str            # INFO | WARNING | CRITICAL | OVERAGE
    scholar_id: str
    scholar_name: str
    platform: str
    spent_usd: float
    budget_usd: float
    utilisation_pct: float
    triggered_at: str = field(default_factory=lambda: _now())
    acknowledged: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# BudgetTracker
# ──────────────────────────────────────────────

class BudgetTracker:
    """
    Central spend ledger.  Records every spend event, derives utilisation,
    and emits structured alerts at configurable thresholds.

    Usage:
        tracker = BudgetTracker()
        tracker.record("s01", "Alice", "anthropic", 12.50, "Claude API batch run")
        alerts = tracker.check_alerts(registry)
    """

    def __init__(
        self,
        log_path: Path = Path("data/spend_log.jsonl"),
        alert_path: Path = Path("data/alerts.jsonl"),
        warn_pct: float = 75.0,
        critical_pct: float = 90.0,
    ) -> None:
        self.log_path = log_path
        self.alert_path = alert_path
        self.warn_pct = warn_pct
        self.critical_pct = critical_pct
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.alert_path.parent.mkdir(parents=True, exist_ok=True)

    # ── recording ────────────────────────────

    def record(
        self,
        scholar_id: str,
        scholar_name: str,
        platform: str,
        amount_usd: float,
        description: str = "",
    ) -> SpendEvent:
        event = SpendEvent(
            scholar_id=scholar_id,
            scholar_name=scholar_name,
            platform=platform,
            amount_usd=round(amount_usd, 4),
            description=description,
        )
        with open(self.log_path, "a") as f:
            f.write(json.dumps(self._event_to_dict(event)) + "\n")
        logger.debug("Recorded spend: %s / %s — $%.4f", scholar_name, platform, amount_usd)
        return event

    # ── querying ─────────────────────────────

    def events(
        self,
        scholar_id: Optional[str] = None,
        platform: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SpendEvent]:
        if not self.log_path.exists():
            return []
        results: list[SpendEvent] = []
        with open(self.log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ev = SpendEvent(**d)
                if scholar_id and ev.scholar_id != scholar_id:
                    continue
                if platform and ev.platform != platform:
                    continue
                rec = datetime.fromisoformat(ev.recorded_at)
                if since and rec < since:
                    continue
                if until and rec > until:
                    continue
                results.append(ev)
        return results

    def total_spend(self, scholar_id: Optional[str] = None, platform: Optional[str] = None) -> float:
        return round(sum(e.amount_usd for e in self.events(scholar_id=scholar_id, platform=platform)), 4)

    def spend_last_n_days(self, n: int, scholar_id: Optional[str] = None) -> float:
        cutoff = datetime.now(timezone.utc) - timedelta(days=n)
        return round(
            sum(e.amount_usd for e in self.events(scholar_id=scholar_id, since=cutoff)), 4
        )

    # ── alerting ─────────────────────────────

    def check_alerts(self, registry) -> list[BudgetAlert]:
        """
        Sweep registry and produce fresh BudgetAlert objects.
        registry: ScholarRegistry  (avoid circular import — passed by caller)
        """
        from compute_admin.scholar_manager import Status

        alerts: list[BudgetAlert] = []
        for scholar in registry.list_all(status=Status.ACTIVE):
            for pa in scholar.platform_access:
                if pa.revoked_at:
                    continue
                if pa.budget_usd == 0:
                    continue
                pct = pa.utilisation_pct
                if pct >= 100:
                    level = "OVERAGE"
                elif pct >= self.critical_pct:
                    level = "CRITICAL"
                elif pct >= self.warn_pct:
                    level = "WARNING"
                else:
                    continue
                alert = BudgetAlert(
                    level=level,
                    scholar_id=scholar.scholar_id,
                    scholar_name=scholar.name,
                    platform=pa.platform.value,
                    spent_usd=pa.spent_usd,
                    budget_usd=pa.budget_usd,
                    utilisation_pct=pct,
                )
                alerts.append(alert)
                self._write_alert(alert)
                logger.warning(
                    "[%s] %s / %s — %.1f%% ($%.2f / $%.2f)",
                    level, scholar.name, pa.platform.value,
                    pct, pa.spent_usd, pa.budget_usd,
                )
        return sorted(alerts, key=lambda a: a.utilisation_pct, reverse=True)

    # ── reports ───────────────────────────────

    def cohort_report(self, registry, cohort: Optional[str] = None) -> list[dict]:
        """Per-scholar summary suitable for Google Sheets export."""
        from compute_admin.scholar_manager import Status

        rows = []
        scholars = registry.list_all(cohort=cohort, status=Status.ACTIVE)
        for s in scholars:
            for pa in s.platform_access:
                rows.append(
                    {
                        "scholar_id": s.scholar_id,
                        "name": s.name,
                        "email": s.email,
                        "cohort": s.cohort,
                        "platform": pa.platform.value,
                        "budget_usd": pa.budget_usd,
                        "spent_usd": pa.spent_usd,
                        "remaining_usd": pa.remaining_usd,
                        "utilisation_pct": pa.utilisation_pct,
                        "status": "⚠️ OVERAGE" if pa.utilisation_pct >= 100
                                  else "🔴 CRITICAL" if pa.utilisation_pct >= self.critical_pct
                                  else "🟡 WARNING" if pa.utilisation_pct >= self.warn_pct
                                  else "🟢 OK",
                    }
                )
        return rows

    def to_csv(self, rows: list[dict]) -> str:
        if not rows:
            return ""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()

    def platform_summary(self, registry) -> dict[str, dict]:
        """Aggregate spend and budget per platform across all active scholars."""
        from compute_admin.scholar_manager import Status

        summary: dict[str, dict] = {}
        for s in registry.list_all(status=Status.ACTIVE):
            for pa in s.platform_access:
                if pa.revoked_at:
                    continue
                key = pa.platform.value
                if key not in summary:
                    summary[key] = {"budget_usd": 0.0, "spent_usd": 0.0, "scholars": 0}
                summary[key]["budget_usd"] = round(summary[key]["budget_usd"] + pa.budget_usd, 4)
                summary[key]["spent_usd"] = round(summary[key]["spent_usd"] + pa.spent_usd, 4)
                summary[key]["scholars"] += 1
        # add utilisation_pct
        for v in summary.values():
            v["utilisation_pct"] = (
                round((v["spent_usd"] / v["budget_usd"]) * 100, 2) if v["budget_usd"] else 0.0
            )
        return summary

    # ── internals ────────────────────────────

    def _write_alert(self, alert: BudgetAlert) -> None:
        with open(self.alert_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "level": alert.level,
                        "scholar_id": alert.scholar_id,
                        "scholar_name": alert.scholar_name,
                        "platform": alert.platform,
                        "spent_usd": alert.spent_usd,
                        "budget_usd": alert.budget_usd,
                        "utilisation_pct": alert.utilisation_pct,
                        "triggered_at": alert.triggered_at,
                    }
                )
                + "\n"
            )

    @staticmethod
    def _event_to_dict(e: SpendEvent) -> dict:
        return {
            "event_id": e.event_id,
            "scholar_id": e.scholar_id,
            "scholar_name": e.scholar_name,
            "platform": e.platform,
            "amount_usd": e.amount_usd,
            "description": e.description,
            "recorded_at": e.recorded_at,
        }
