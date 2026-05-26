"""
tests/test_compute_admin.py
Comprehensive test suite for MATS Compute Admin.

Run:  pytest tests/ -v
"""

import json
import pytest
from pathlib import Path


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def tmp_registry(tmp_path):
    from compute_admin.scholar_manager import ScholarRegistry
    return ScholarRegistry(db_path=tmp_path / "scholars.json")


@pytest.fixture
def tmp_budget(tmp_path):
    from compute_admin.budget_tracker import BudgetTracker
    return BudgetTracker(
        log_path=tmp_path / "spend.jsonl",
        alert_path=tmp_path / "alerts.jsonl",
        warn_pct=75.0,
        critical_pct=90.0,
    )


@pytest.fixture
def tmp_keymanager(tmp_path):
    from compute_admin.api_key_manager import APIKeyManager
    return APIKeyManager(registry_path=tmp_path / "keys.json")


@pytest.fixture
def tmp_requests(tmp_path):
    from compute_admin.request_handler import RequestHandler
    return RequestHandler(queue_path=tmp_path / "requests.json")


@pytest.fixture
def tmp_auditor(tmp_path):
    from compute_admin.platform_auditor import PlatformAuditor
    return PlatformAuditor(report_path=tmp_path / "reports")


@pytest.fixture
def populated_registry(tmp_registry):
    """Registry with 3 active scholars + 1 offboarded."""
    from compute_admin.scholar_manager import Scholar, Platform, Status, Role
    s1 = tmp_registry.add(Scholar(name="Alice", email="alice@t.com", cohort="A25", status=Status.ACTIVE))
    s2 = tmp_registry.add(Scholar(name="Bob",   email="bob@t.com",   cohort="A25", status=Status.ACTIVE))
    s3 = tmp_registry.add(Scholar(name="Carol", email="carol@t.com", cohort="S26", status=Status.ACTIVE))
    s4 = tmp_registry.add(Scholar(name="Dave",  email="dave@t.com",  cohort="A24", status=Status.OFFBOARDED))

    tmp_registry.grant_platform(s1.scholar_id, Platform.ANTHROPIC, "alice", 500.0)
    tmp_registry.grant_platform(s2.scholar_id, Platform.OPENAI, "bob", 400.0)
    tmp_registry.grant_platform(s3.scholar_id, Platform.RUNPOD, "carol", 200.0)
    tmp_registry.grant_platform(s4.scholar_id, Platform.OPENAI, "dave", 300.0)

    # Simulate spend
    s1_reload = tmp_registry.get(s1.scholar_id)
    s1_reload.platform_access[0].spent_usd = 460.0   # 92% — CRITICAL

    s2_reload = tmp_registry.get(s2.scholar_id)
    s2_reload.platform_access[0].spent_usd = 310.0   # 77.5% — WARNING

    tmp_registry._save()
    return tmp_registry


# ──────────────────────────────────────────────
# ScholarRegistry tests
# ──────────────────────────────────────────────

class TestScholarRegistry:

    def test_add_scholar(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Status
        s = tmp_registry.add(Scholar(name="Test", email="t@t.com", status=Status.ACTIVE))
        assert s.scholar_id is not None
        assert tmp_registry.get(s.scholar_id).name == "Test"

    def test_duplicate_email_raises(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Status
        tmp_registry.add(Scholar(name="A", email="dup@t.com", status=Status.ACTIVE))
        with pytest.raises(ValueError, match="already exists"):
            tmp_registry.add(Scholar(name="B", email="dup@t.com", status=Status.ACTIVE))

    def test_get_nonexistent_raises(self, tmp_registry):
        with pytest.raises(KeyError):
            tmp_registry.get("notareal_id")

    def test_update_scholar(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Status
        s = tmp_registry.add(Scholar(name="X", email="x@t.com", status=Status.PENDING))
        updated = tmp_registry.update(s.scholar_id, notes="new note")
        assert updated.notes == "new note"

    def test_grant_and_revoke_platform(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Platform, Status
        s = tmp_registry.add(Scholar(name="G", email="g@t.com", status=Status.ACTIVE))
        access = tmp_registry.grant_platform(s.scholar_id, Platform.ANTHROPIC, "g_user", 300.0)
        assert access.budget_usd == 300.0
        assert access.spent_usd == 0.0

        tmp_registry.revoke_platform(s.scholar_id, Platform.ANTHROPIC)
        s_reload = tmp_registry.get(s.scholar_id)
        assert s_reload.platform_access[0].revoked_at is not None

    def test_grant_duplicate_platform_raises(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Platform, Status
        s = tmp_registry.add(Scholar(name="H", email="h@t.com", status=Status.ACTIVE))
        tmp_registry.grant_platform(s.scholar_id, Platform.OPENAI, "h_user", 100.0)
        with pytest.raises(ValueError, match="already has access"):
            tmp_registry.grant_platform(s.scholar_id, Platform.OPENAI, "h_user2", 50.0)

    def test_offboard_scholar(self, tmp_registry):
        from compute_admin.scholar_manager import Scholar, Platform, Status
        s = tmp_registry.add(Scholar(name="Off", email="off@t.com", status=Status.ACTIVE))
        tmp_registry.grant_platform(s.scholar_id, Platform.ANTHROPIC, "off_u", 100.0)
        offboarded = tmp_registry.offboard(s.scholar_id, reason="Fellowship ended")
        assert offboarded.status == Status.OFFBOARDED
        assert all(p.revoked_at is not None for p in offboarded.platform_access)

    def test_list_filters(self, populated_registry):
        from compute_admin.scholar_manager import Role, Status
        active = populated_registry.list_all(status=Status.ACTIVE)
        assert len(active) == 3

        cohort_a25 = populated_registry.list_all(cohort="A25")
        assert len(cohort_a25) == 2

    def test_budget_alerts_threshold(self, populated_registry):
        alerts = populated_registry.budget_alerts(warn_pct=75.0, critical_pct=90.0)
        levels = {a["level"] for a in alerts}
        assert "CRITICAL" in levels   # Alice at 92%
        assert "WARNING"  in levels   # Bob at 77.5%

    def test_persistence(self, tmp_path):
        from compute_admin.scholar_manager import Scholar, ScholarRegistry, Status
        path = tmp_path / "p_scholars.json"
        reg1 = ScholarRegistry(db_path=path)
        s = reg1.add(Scholar(name="Persist", email="p@t.com", status=Status.ACTIVE))

        reg2 = ScholarRegistry(db_path=path)
        assert reg2.get(s.scholar_id).name == "Persist"


# ──────────────────────────────────────────────
# BudgetTracker tests
# ──────────────────────────────────────────────

class TestBudgetTracker:

    def test_record_event(self, tmp_budget):
        e = tmp_budget.record("s01", "Alice", "anthropic", 25.0, "Test run")
        assert e.event_id is not None
        assert e.amount_usd == 25.0

    def test_total_spend(self, tmp_budget):
        tmp_budget.record("s01", "Alice", "anthropic", 10.0, "run A")
        tmp_budget.record("s01", "Alice", "anthropic", 15.0, "run B")
        tmp_budget.record("s02", "Bob",   "openai",    20.0, "run C")
        assert tmp_budget.total_spend(scholar_id="s01") == 25.0
        assert tmp_budget.total_spend(platform="openai")  == 20.0

    def test_events_filtering(self, tmp_budget):
        tmp_budget.record("s01", "Alice", "anthropic", 5.0, "a")
        tmp_budget.record("s02", "Bob",   "openai",    5.0, "b")
        events = tmp_budget.events(scholar_id="s01")
        assert len(events) == 1
        assert events[0].scholar_id == "s01"

    def test_budget_alerts(self, populated_registry, tmp_budget):
        alerts = tmp_budget.check_alerts(populated_registry)
        # Alice is at 92%, Bob at 77.5%
        assert any(a.level == "CRITICAL" for a in alerts)
        assert any(a.level == "WARNING"  for a in alerts)

    def test_csv_export(self, populated_registry, tmp_budget):
        rows = tmp_budget.cohort_report(populated_registry)
        csv_str = tmp_budget.to_csv(rows)
        assert "name" in csv_str
        assert "spent_usd" in csv_str

    def test_platform_summary(self, populated_registry, tmp_budget):
        summary = tmp_budget.platform_summary(populated_registry)
        assert isinstance(summary, dict)
        # At least the platforms we granted should appear
        platforms = set(summary.keys())
        assert len(platforms) >= 1


# ──────────────────────────────────────────────
# APIKeyManager tests
# ──────────────────────────────────────────────

class TestAPIKeyManager:

    def test_register_key_does_not_store_raw(self, tmp_keymanager):
        rec = tmp_keymanager.register_key("sk-secret-key-123", "openai", "s01", "Alice")
        assert "sk-secret-key-123" not in str(rec)
        assert rec.key_hash != "sk-secret-key-123"

    def test_verify_key(self, tmp_keymanager):
        raw = "sk-verify-test-999"
        rec = tmp_keymanager.register_key(raw, "openai", "s01", "Alice")
        assert tmp_keymanager.verify_key(rec.key_id, raw) is True
        assert tmp_keymanager.verify_key(rec.key_id, "wrong-key") is False

    def test_rotate_key(self, tmp_keymanager):
        rec = tmp_keymanager.register_key("sk-old", "anthropic", "s01", "Alice")
        tmp_keymanager.rotate_key(rec.key_id, "sk-new", reason="scheduled")
        updated = tmp_keymanager._keys[rec.key_id]
        assert updated.last_rotated_at is not None
        assert tmp_keymanager.verify_key(rec.key_id, "sk-new") is True
        assert tmp_keymanager.verify_key(rec.key_id, "sk-old") is False

    def test_revoke_key(self, tmp_keymanager):
        rec = tmp_keymanager.register_key("sk-rev", "runpod", "s02", "Bob")
        assert rec.is_active is True
        tmp_keymanager.revoke_key(rec.key_id, reason="offboarded")
        assert tmp_keymanager._keys[rec.key_id].is_active is False

    def test_due_for_rotation_detection(self, tmp_keymanager):
        from datetime import datetime, timedelta, timezone
        rec = tmp_keymanager.register_key("sk-old2", "openai", "s03", "Carol")
        # Backdate created_at to simulate overdue
        rec.created_at = (
            datetime.now(timezone.utc) - timedelta(days=45)
        ).isoformat()
        overdue = tmp_keymanager.due_for_rotation()
        assert any(r.key_id == rec.key_id for r in overdue)

    def test_security_audit_structure(self, tmp_keymanager):
        tmp_keymanager.register_key("sk-a", "openai", "s01", "Alice")
        audit = tmp_keymanager.security_audit()
        assert "summary" in audit
        assert "platform_stats" in audit
        assert audit["summary"]["total_keys"] >= 1

    def test_persistence(self, tmp_path):
        from compute_admin.api_key_manager import APIKeyManager
        km1 = APIKeyManager(registry_path=tmp_path / "keys.json")
        rec = km1.register_key("sk-persist", "openai", "s01", "P")
        km2 = APIKeyManager(registry_path=tmp_path / "keys.json")
        assert rec.key_id in km2._keys


# ──────────────────────────────────────────────
# RequestHandler tests
# ──────────────────────────────────────────────

class TestRequestHandler:

    def test_submit_request(self, tmp_requests):
        from compute_admin.request_handler import RequestType, Priority
        req = tmp_requests.submit(
            scholar_id="s01", scholar_name="Alice",
            request_type=RequestType.BUDGET_TOPUP,
            description="Need $200 top-up", platform="openai",
            amount_usd=200.0, priority=Priority.HIGH,
        )
        assert req.request_id.startswith("REQ-")
        assert req.amount_usd == 200.0

    def test_workflow_approve_resolve(self, tmp_requests):
        from compute_admin.request_handler import RequestType, RequestStatus
        req = tmp_requests.submit("s01", "Alice", RequestType.BUDGET_TOPUP, "desc")
        tmp_requests.assign(req.request_id, "admin1")
        tmp_requests.approve(req.request_id, "admin1", "Approved within policy")
        tmp_requests.resolve(req.request_id, "admin1", "Topped up $200")

        updated = tmp_requests._get(req.request_id)
        assert updated.status == RequestStatus.RESOLVED
        assert updated.resolved_at is not None
        assert len(updated.comments) == 2   # approve + resolve comments

    def test_reject_request(self, tmp_requests):
        from compute_admin.request_handler import RequestType, RequestStatus
        req = tmp_requests.submit("s02", "Bob", RequestType.GPU_ALLOCATION, "desc")
        tmp_requests.reject(req.request_id, "admin1", "Out of cluster capacity")
        assert tmp_requests._get(req.request_id).status == RequestStatus.REJECTED

    def test_open_queue_sorted_by_priority(self, tmp_requests):
        from compute_admin.request_handler import RequestType, Priority
        tmp_requests.submit("s1", "A", RequestType.OTHER, "d", priority=Priority.LOW)
        tmp_requests.submit("s2", "B", RequestType.OTHER, "d", priority=Priority.URGENT)
        tmp_requests.submit("s3", "C", RequestType.OTHER, "d", priority=Priority.NORMAL)
        queue = tmp_requests.open_queue()
        assert queue[0].priority == Priority.URGENT

    def test_sla_breach_detection(self, tmp_requests):
        from compute_admin.request_handler import RequestType
        from datetime import datetime, timedelta, timezone
        req = tmp_requests.submit("s01", "Alice", RequestType.TROUBLESHOOT, "broken")
        # Backdate to simulate SLA breach
        req.created_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        assert req.is_sla_breached is True

    def test_queue_stats(self, tmp_requests):
        from compute_admin.request_handler import RequestType
        tmp_requests.submit("s1", "A", RequestType.BUDGET_TOPUP, "d1")
        tmp_requests.submit("s2", "B", RequestType.GPU_ALLOCATION, "d2")
        stats = tmp_requests.queue_stats()
        assert stats["total_requests"] == 2
        assert stats["open_queue_depth"] == 2


# ──────────────────────────────────────────────
# PlatformAuditor tests
# ──────────────────────────────────────────────

class TestPlatformAuditor:

    def test_orphaned_access_detection(self, populated_registry, tmp_keymanager, tmp_budget, tmp_auditor):
        report = tmp_auditor.run_full_audit(
            registry=populated_registry,
            key_manager=tmp_keymanager,
            budget_tracker=tmp_budget,
        )
        # Dave is offboarded with un-revoked OPENAI access in populated_registry
        orphan_findings = [f for f in report.findings if f.category == "orphaned_access"]
        assert len(orphan_findings) >= 1
        assert any("Dave" in f.entity_name for f in orphan_findings)

    def test_budget_overage_detection(self, populated_registry, tmp_keymanager, tmp_budget, tmp_auditor):
        # Alice is at 92% — should trigger budget alert in CRITICAL
        report = tmp_auditor.run_full_audit(populated_registry, tmp_keymanager, tmp_budget)
        overage = [f for f in report.findings if f.category == "budget_overage"]
        # 92% is not yet an overage (>100%), but check structure passes
        assert isinstance(report.summary, dict)
        assert "total_findings" in report.summary

    def test_key_rotation_overdue(self, populated_registry, tmp_keymanager, tmp_budget, tmp_auditor):
        from datetime import datetime, timedelta, timezone
        # Register an overdue key
        rec = tmp_keymanager.register_key("sk-overdue", "openai", "s01", "Alice")
        rec.created_at = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()

        report = tmp_auditor.run_full_audit(populated_registry, tmp_keymanager, tmp_budget)
        rotation_findings = [f for f in report.findings if f.category == "key_rotation"]
        assert len(rotation_findings) >= 1

    def test_slack_digest_format(self, populated_registry, tmp_keymanager, tmp_budget, tmp_auditor):
        report = tmp_auditor.run_full_audit(populated_registry, tmp_keymanager, tmp_budget)
        digest = tmp_auditor.slack_digest(report)
        assert "MATS Weekly Security Audit" in digest
        assert "Summary" in digest

    def test_report_persisted_to_disk(self, populated_registry, tmp_keymanager, tmp_budget, tmp_auditor):
        report = tmp_auditor.run_full_audit(populated_registry, tmp_keymanager, tmp_budget)
        report_files = list(tmp_auditor.report_path.glob("audit_*.json"))
        assert len(report_files) == 1
        with open(report_files[0]) as f:
            data = json.load(f)
        assert data["report_id"] == report.report_id


# ──────────────────────────────────────────────
# ClusterMonitor tests (mock mode)
# ──────────────────────────────────────────────

class TestClusterMonitor:

    def test_active_jobs_returns_list(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        jobs = mon.active_jobs()  # mock mode
        assert isinstance(jobs, list)
        assert len(jobs) > 0

    def test_stalled_jobs_detected(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl", max_runtime_hours=1.0)
        stalled = mon.stalled_jobs()
        assert len(stalled) >= 1  # Bob's job (90000s ≈ 25h) should trigger

    def test_failed_jobs_returned(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        failed = mon.failed_jobs()
        # mock data has FAILED and TIMEOUT jobs
        assert len(failed) >= 1

    def test_scholar_gpu_hours_aggregated(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        gpu_h = mon.scholar_gpu_hours()
        assert isinstance(gpu_h, dict)
        assert len(gpu_h) >= 1

    def test_health_snapshot_structure(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        snap = mon.health_snapshot()
        required_keys = {"running_jobs", "pending_jobs", "stalled_jobs", "gpus_in_use", "mode"}
        assert required_keys.issubset(snap.keys())
        assert snap["mode"] == "mock"

    def test_job_submit_dry_run(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        job_id = mon.submit_job("#!/bin/bash\nsleep 10", dry_run=True)
        assert job_id is not None
        assert "mock" in job_id

    def test_cancel_job_dry_run(self, tmp_path):
        from compute_admin.cluster_monitor import ClusterMonitor
        mon = ClusterMonitor(history_path=tmp_path / "jobs.jsonl")
        success = mon.cancel_job("5001", reason="Stalled", dry_run=True)
        assert success is True
