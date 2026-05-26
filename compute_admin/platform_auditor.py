"""
compute_admin/platform_auditor.py
MATS Compute Admin — Security & IAM audit engine.

Performs weekly automated audits covering:
  - Orphaned accounts (scholar offboarded but platform access not revoked)
  - Over-permissioned roles
  - API keys past rotation deadline
  - Budget overages
  - Inactive accounts
  - MFA compliance (placeholder — real check via platform API)

Emits structured AuditReport objects suitable for Slack/email digests
and persistent storage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Findings
# ──────────────────────────────────────────────

class Severity(str):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    INFO     = "INFO"


@dataclass
class AuditFinding:
    severity: str
    category: str
    description: str
    entity_id: str         # scholar_id, key_id, etc.
    entity_name: str
    platform: str = ""
    recommendation: str = ""
    finding_id: str = field(default_factory=lambda: f"F{_now()[:10].replace('-','')}")


@dataclass
class AuditReport:
    report_id: str
    generated_at: str
    findings: list[AuditFinding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)


# ──────────────────────────────────────────────
# PlatformAuditor
# ──────────────────────────────────────────────

class PlatformAuditor:
    """
    Coordinates all security and compliance checks and produces
    an AuditReport.

    Designed to run nightly via a cron job or GitHub Actions workflow.
    """

    def __init__(
        self,
        report_path: Path = Path("data/audit_reports"),
        inactive_days: int = 30,
    ) -> None:
        self.report_path = report_path
        self.report_path.mkdir(parents=True, exist_ok=True)
        self.inactive_days = inactive_days

    def run_full_audit(
        self,
        registry,         # ScholarRegistry
        key_manager,      # APIKeyManager
        budget_tracker,   # BudgetTracker
    ) -> AuditReport:
        """
        Execute all audit checks and return a consolidated AuditReport.
        """
        import uuid
        report = AuditReport(
            report_id=str(uuid.uuid4())[:8],
            generated_at=_now(),
        )

        self._check_orphaned_access(report, registry)
        self._check_key_rotation(report, key_manager)
        self._check_budget_overages(report, registry)
        self._check_inactive_accounts(report, registry)
        self._check_offboarded_with_active_keys(report, registry, key_manager)

        report.summary = {
            "total_findings": len(report.findings),
            "critical": report.critical_count,
            "high": report.high_count,
            "medium": sum(1 for f in report.findings if f.severity == Severity.MEDIUM),
            "low": sum(1 for f in report.findings if f.severity == Severity.LOW),
            "info": sum(1 for f in report.findings if f.severity == Severity.INFO),
        }

        self._save_report(report)
        self._log_summary(report)
        return report

    # ── individual checks ─────────────────────

    def _check_orphaned_access(self, report: AuditReport, registry) -> None:
        """
        Scholars with Status.OFFBOARDED who still have un-revoked platform access.
        """
        from compute_admin.scholar_manager import Status

        for s in registry.list_all(status=Status.OFFBOARDED):
            live_platforms = [
                pa for pa in s.platform_access if pa.revoked_at is None
            ]
            for pa in live_platforms:
                report.findings.append(
                    AuditFinding(
                        severity=Severity.CRITICAL,
                        category="orphaned_access",
                        description=(
                            f"Offboarded scholar {s.name!r} retains un-revoked access "
                            f"to {pa.platform.value}."
                        ),
                        entity_id=s.scholar_id,
                        entity_name=s.name,
                        platform=pa.platform.value,
                        recommendation=f"Immediately revoke {s.name}'s {pa.platform.value} access and rotate/delete any associated keys.",
                    )
                )

    def _check_key_rotation(self, report: AuditReport, key_manager) -> None:
        """Keys past rotation deadline."""
        overdue = key_manager.due_for_rotation()
        for rec in overdue:
            days_over = rec.days_since_rotation - rec.rotation_days
            severity = Severity.HIGH if days_over > 14 else Severity.MEDIUM
            report.findings.append(
                AuditFinding(
                    severity=severity,
                    category="key_rotation",
                    description=(
                        f"API key {rec.key_id} for {rec.scholar_name!r} on {rec.platform} "
                        f"is {days_over} days past rotation deadline "
                        f"(policy: every {rec.rotation_days} days)."
                    ),
                    entity_id=rec.key_id,
                    entity_name=rec.scholar_name,
                    platform=rec.platform,
                    recommendation=f"Rotate {rec.platform} key for {rec.scholar_name} immediately.",
                )
            )

    def _check_budget_overages(self, report: AuditReport, registry) -> None:
        """Active scholars who have exceeded 100% of platform budget."""
        from compute_admin.scholar_manager import Status

        for s in registry.list_all(status=Status.ACTIVE):
            for pa in s.platform_access:
                if pa.revoked_at:
                    continue
                if pa.budget_usd > 0 and pa.spent_usd > pa.budget_usd:
                    overage = round(pa.spent_usd - pa.budget_usd, 2)
                    report.findings.append(
                        AuditFinding(
                            severity=Severity.HIGH,
                            category="budget_overage",
                            description=(
                                f"{s.name!r} is ${overage:.2f} over budget on {pa.platform.value} "
                                f"(spent ${pa.spent_usd:.2f} / ${pa.budget_usd:.2f})."
                            ),
                            entity_id=s.scholar_id,
                            entity_name=s.name,
                            platform=pa.platform.value,
                            recommendation="Review spend immediately; suspend or reduce access if unauthorised.",
                        )
                    )

    def _check_inactive_accounts(self, report: AuditReport, registry) -> None:
        """
        Active scholars with no recorded spend in `inactive_days` days.
        Candidate for access review or budget reclamation.
        """
        from compute_admin.scholar_manager import Status
        from datetime import timedelta

        cutoff_iso = datetime.now(timezone.utc) - timedelta(days=self.inactive_days)
        for s in registry.list_all(status=Status.ACTIVE):
            if not s.platform_access:
                continue
            last_update = datetime.fromisoformat(s.updated_at)
            if last_update < cutoff_iso:
                report.findings.append(
                    AuditFinding(
                        severity=Severity.LOW,
                        category="inactive_account",
                        description=(
                            f"{s.name!r} has had no activity for over {self.inactive_days} days "
                            f"(last update: {s.updated_at[:10]})."
                        ),
                        entity_id=s.scholar_id,
                        entity_name=s.name,
                        recommendation="Confirm scholar is still active; reclaim unused budget if not.",
                    )
                )

    def _check_offboarded_with_active_keys(
        self, report: AuditReport, registry, key_manager
    ) -> None:
        """Offboarded scholars who still have active key records."""
        from compute_admin.scholar_manager import Status

        for s in registry.list_all(status=Status.OFFBOARDED):
            active_keys = key_manager.keys_for_scholar(s.scholar_id, active_only=True)
            for k in active_keys:
                report.findings.append(
                    AuditFinding(
                        severity=Severity.CRITICAL,
                        category="active_key_offboarded_scholar",
                        description=(
                            f"Key {k.key_id} on {k.platform} is still ACTIVE "
                            f"for offboarded scholar {s.name!r}."
                        ),
                        entity_id=k.key_id,
                        entity_name=s.name,
                        platform=k.platform,
                        recommendation=f"Revoke key {k.key_id} immediately and verify no unauthorized usage.",
                    )
                )

    # ── persistence + reporting ───────────────

    def _save_report(self, report: AuditReport) -> Path:
        fname = self.report_path / f"audit_{report.generated_at[:10]}_{report.report_id}.json"
        with open(fname, "w") as f:
            json.dump(
                {
                    "report_id": report.report_id,
                    "generated_at": report.generated_at,
                    "summary": report.summary,
                    "findings": [
                        {
                            "finding_id": fn.finding_id,
                            "severity": fn.severity,
                            "category": fn.category,
                            "description": fn.description,
                            "entity_id": fn.entity_id,
                            "entity_name": fn.entity_name,
                            "platform": fn.platform,
                            "recommendation": fn.recommendation,
                        }
                        for fn in report.findings
                    ],
                },
                f,
                indent=2,
            )
        return fname

    def _log_summary(self, report: AuditReport) -> None:
        s = report.summary
        logger.info(
            "Audit %s complete — %d findings: %d CRITICAL, %d HIGH, %d MEDIUM, %d LOW",
            report.report_id,
            s["total_findings"],
            s["critical"],
            s["high"],
            s["medium"],
            s["low"],
        )
        if report.critical_count > 0:
            logger.critical(
                "CRITICAL FINDINGS (%d) require immediate action!", report.critical_count
            )

    def slack_digest(self, report: AuditReport) -> str:
        """
        Format a Slack-ready markdown digest of the audit report.
        Post via Slack Incoming Webhooks in production.
        """
        s = report.summary
        lines = [
            f"*🔒 MATS Weekly Security Audit — {report.generated_at[:10]}*",
            f"> Report ID: `{report.report_id}`",
            f"",
            f"*Summary:* {s['total_findings']} findings",
            f"🔴 Critical: {s['critical']}  |  🟠 High: {s['high']}  |  🟡 Medium: {s['medium']}  |  🟢 Low: {s['low']}",
            "",
        ]
        if report.findings:
            lines.append("*Top Findings:*")
            for fn in sorted(report.findings, key=lambda f: ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].index(f.severity))[:5]:
                icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "ℹ️"}.get(fn.severity, "")
                lines.append(f"{icon} `[{fn.category}]` {fn.description}")
        return "\n".join(lines)
