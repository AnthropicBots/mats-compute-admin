# MATS Compute Admin

A production-grade compute resource administration system built specifically for the operational requirements of a MATS Research fellowship program — covering the exact scope of the Compute Administrator role.

## What this covers

| MATS Role Requirement | Module |
|---|---|
| Scholar account provisioning & offboarding | `scholar_manager.py` |
| Budget tracking, alerts, CSV exports | `budget_tracker.py` |
| API key lifecycle (rotation, revocation, audit) | `api_key_manager.py` |
| Slurm/HPC cluster monitoring, stalled job detection | `cluster_monitor.py` |
| Compute request queue, SLA tracking, triage | `request_handler.py` |
| Weekly security/IAM audit (RBAC, orphaned access) | `platform_auditor.py` |
| CLI for all of the above | `cli.py` |

## Quick start

```bash
pip install -e ".[dev]"
python scripts/seed_demo_data.py      # populate realistic demo data

# CLI commands
mats-admin scholars list
mats-admin budget alerts
mats-admin budget platform-summary
mats-admin cluster status
mats-admin cluster stalled
mats-admin requests queue
mats-admin audit run
```

## Architecture

```
compute_admin/
├── scholar_manager.py   — Scholar lifecycle, RBAC, platform access grants
├── budget_tracker.py    — Spend ledger, alert thresholds, cohort reports
├── api_key_manager.py   — Key registration, rotation policy, security audit
├── cluster_monitor.py   — Slurm squeue/sacct parsing, GPU-hour accounting
├── request_handler.py   — Request queue, approval workflow, SLA enforcement
├── platform_auditor.py  — Full IAM audit engine + Slack digest generation
└── cli.py               — Unified CLI (argparse)
scripts/
└── seed_demo_data.py    — Realistic demo data for all modules
tests/
└── test_compute_admin.py — 41 tests, 100% passing
```

## Test results

```
41 passed in 0.15s
```

Covers: ScholarRegistry (10), BudgetTracker (6), APIKeyManager (7), RequestHandler (6), PlatformAuditor (5), ClusterMonitor (7).

## Key design decisions

- **Zero mandatory dependencies** — stdlib-only core. Optional extras for Airtable, Slack, and cloud SDKs.
- **Raw API keys are never stored** — `api_key_manager.py` stores SHA-256 fingerprints only; real keys must live in a vault (AWS Secrets Manager / HashiCorp Vault).
- **Slurm mock mode** — `cluster_monitor.py` auto-detects when `squeue` is absent and falls back to realistic mock data, enabling full local dev/CI without an HPC cluster.
- **Airtable-ready persistence** — `ScholarRegistry._load/_save` and `RequestHandler._load/_save` are thin adapters; swap them for `pyairtable` calls with no changes to business logic.
- **Structured audit log** — all findings written as JSONL for easy ingestion into Elasticsearch/Splunk.

## Production integration points

- **Airtable**: Replace `_load/_save` in `ScholarRegistry` and `RequestHandler` with `pyairtable` API calls.
- **Slack**: `PlatformAuditor.slack_digest()` returns ready-to-post Markdown; post via `slack-sdk` Incoming Webhooks.
- **Secrets vault**: Replace `api_key_manager` mock storage with AWS Secrets Manager `boto3` calls.
- **Cron/GitHub Actions**: Run `mats-admin audit run` nightly; run `mats-admin budget alerts` on every spend event.
# mats-compute-admin
