#!/usr/bin/env python3
"""
scripts/seed_demo_data.py
Populate the system with realistic demo data for testing / showcasing.

Run:   python scripts/seed_demo_data.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from compute_admin.scholar_manager import (
    Scholar, ScholarRegistry, Platform, Role, Status,
)
from compute_admin.budget_tracker import BudgetTracker
from compute_admin.api_key_manager import APIKeyManager
from compute_admin.request_handler import RequestHandler, RequestType, Priority
from compute_admin.platform_auditor import PlatformAuditor

print("🌱 Seeding MATS Compute Admin demo data...")

# ── scholars ─────────────────────────────────

reg = ScholarRegistry()
bt  = BudgetTracker()
km  = APIKeyManager()
rh  = RequestHandler()

scholars_data = [
    # (name, email, cohort, platform_grants)
    ("Alice Chen",    "alice@example.com",   "Autumn-2025",
     [(Platform.ANTHROPIC, "alice_chen",  500.0, 412.5),
      (Platform.MATS_CLUSTER, "achen",    0.0,   0.0)]),

    ("Bob Okafor",    "bob@example.com",     "Autumn-2025",
     [(Platform.OPENAI,     "bob_okafor", 400.0, 398.1),   # near limit
      (Platform.RUNPOD,     "bokafor",    300.0, 125.0)]),

    ("Carol Vasquez", "carol@example.com",   "Autumn-2025",
     [(Platform.ANTHROPIC,  "carol_v",    500.0, 50.0),
      (Platform.LAMBDA_LABS,"cvasquez",   200.0, 89.3)]),

    ("Dave Kim",      "dave@example.com",    "Spring-2026",
     [(Platform.OPENAI,     "dave_kim",   600.0, 615.0),   # overage!
      (Platform.AWS,        "dkim",       400.0, 200.0)]),

    ("Eve Martinez",  "eve@example.com",     "Spring-2026",
     [(Platform.ANTHROPIC,  "eve_m",      500.0, 120.0),
      (Platform.GCP,        "emartinez",  300.0, 88.0)]),

    # Offboarded scholar — should trigger security finding
    ("Frank Li",      "frank@example.com",   "Autumn-2024",
     [(Platform.OPENAI,     "frank_li",   300.0, 50.0)]),
]

created_scholars = []
for name, email, cohort, grants in scholars_data:
    try:
        s = reg.add(Scholar(name=name, email=email, cohort=cohort, status=Status.ACTIVE))
    except ValueError:
        s = next(x for x in reg.list_all() if x.email == email)

    for platform, username, budget, spent in grants:
        try:
            reg.grant_platform(s.scholar_id, platform, username, budget)
        except ValueError:
            pass
        pa = s.get_platform(platform)
        if pa and spent > 0:
            pa.spent_usd = spent
    created_scholars.append(s)

# Force save with updated spend
from compute_admin.scholar_manager import _scholar_to_dict
import json
reg._save()

# Offboard Frank
frank = next(x for x in reg.list_all() if x.email == "frank@example.com")
# Intentionally NOT revoking platform access — creates CRITICAL finding
frank.status = Status.OFFBOARDED
reg._save()

print(f"  ✅ Created {len(created_scholars)} scholars")

# ── spend events ─────────────────────────────

spend_events = [
    ("alice",   "Alice Chen",    "anthropic",  50.0,  "Constitutional AI probe batch"),
    ("bob",     "Bob Okafor",    "openai",     12.0,  "RLHF ablation study"),
    ("carol",   "Carol Vasquez", "anthropic",  8.5,   "Mechanistic interp sweep"),
    ("dave",    "Dave Kim",      "openai",     25.0,  "Fine-tune run A/B"),
    ("eve",     "Eve Martinez",  "anthropic",  15.0,  "Safety evaluation batch"),
]
alice = next(x for x in reg.list_all() if x.email == "alice@example.com")
for name_prefix, name, platform, amount, desc in spend_events:
    s = next(x for x in reg.list_all() if name.split()[0].lower() in x.name.lower())
    bt.record(s.scholar_id, s.name, platform, amount, desc)

print(f"  ✅ Created {len(spend_events)} spend events")

# ── API keys ─────────────────────────────────

for s in reg.list_all():
    if s.status == Status.ACTIVE:
        for pa in s.platform_access:
            try:
                fake_key = f"sk-{s.scholar_id}-{pa.platform.value}-testkey"
                km.register_key(fake_key, pa.platform.value, s.scholar_id, s.name)
            except Exception:
                pass

# Also register a key for offboarded Frank (security finding)
frank_reload = next(x for x in reg.list_all() if x.email == "frank@example.com")
try:
    km.register_key("sk-frank-openai-stalekey", "openai", frank_reload.scholar_id, frank_reload.name, notes="Pre-offboarding key — not revoked (intentional for demo)")
except Exception:
    pass

print("  ✅ Registered API keys")

# ── compute requests ─────────────────────────

requests_data = [
    ("Bob Okafor",    "openai",    RequestType.BUDGET_TOPUP,        "Nearly at limit — need $200 top-up for deadline experiment",       200.0,  None, Priority.HIGH),
    ("Carol Vasquez", "anthropic", RequestType.GPU_ALLOCATION,      "Requesting 40 GPU-hours for mechanistic interp study",             None,   40.0, Priority.NORMAL),
    ("Dave Kim",      "openai",    RequestType.BUDGET_TOPUP,        "Overage on account — please top up $100 to cover",                100.0,  None, Priority.URGENT),
    ("Alice Chen",    "mats_cluster","RequestType.TROUBLESHOOT",    "SSH key rejected after password change — can't submit jobs",       None,   None, Priority.HIGH),
    ("Eve Martinez",  "gcp",       RequestType.NEW_PLATFORM_ACCESS, "Need GCP Vertex AI access for interpretability experiments",       None,   None, Priority.NORMAL),
]

for name, platform, req_type, desc, amount, gpu_h, prio in requests_data:
    s = next(x for x in reg.list_all() if name.split()[0].lower() in x.name.lower())
    rh.submit(
        scholar_id=s.scholar_id,
        scholar_name=s.name,
        request_type=req_type if isinstance(req_type, RequestType) else RequestType.TROUBLESHOOT,
        description=desc,
        platform=platform,
        amount_usd=amount,
        gpu_hours=gpu_h,
        priority=prio,
    )

print(f"  ✅ Created {len(requests_data)} compute requests")

# ── run security audit ────────────────────────

aud = PlatformAuditor()
report = aud.run_full_audit(registry=reg, key_manager=km, budget_tracker=bt)
print(f"\n🔒 Security Audit complete: {report.summary['total_findings']} findings")
print(f"   Critical: {report.summary['critical']}  |  High: {report.summary['high']}")

print("\n✅ Demo data seeded successfully!")
print("   Run:  python -m compute_admin.cli scholars list")
print("         python -m compute_admin.cli budget alerts")
print("         python -m compute_admin.cli cluster status")
print("         python -m compute_admin.cli audit run")
