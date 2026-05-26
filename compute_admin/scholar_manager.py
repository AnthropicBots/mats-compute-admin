"""
compute_admin/scholar_manager.py
MATS Compute Admin — Scholar lifecycle management.

Handles creation, updates, offboarding, and RBAC enforcement for scholars
across GPU, API, and cloud platforms.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Domain models
# ──────────────────────────────────────────────

class Role(str, Enum):
    SCHOLAR = "scholar"
    MENTOR = "mentor"
    COLLABORATOR = "collaborator"
    ADMIN = "admin"


class Platform(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    RUNPOD = "runpod"
    LAMBDA_LABS = "lambda_labs"
    AWS = "aws"
    GCP = "gcp"
    MATS_CLUSTER = "mats_cluster"


class Status(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    OFFBOARDED = "offboarded"
    PENDING = "pending"


@dataclass
class PlatformAccess:
    platform: Platform
    username: str
    budget_usd: float
    spent_usd: float = 0.0
    api_key_id: Optional[str] = None          # reference id, never the key itself
    granted_at: str = field(default_factory=lambda: _now())
    revoked_at: Optional[str] = None

    @property
    def remaining_usd(self) -> float:
        return round(self.budget_usd - self.spent_usd, 4)

    @property
    def utilisation_pct(self) -> float:
        if self.budget_usd == 0:
            return 0.0
        return round((self.spent_usd / self.budget_usd) * 100, 2)


@dataclass
class Scholar:
    name: str
    email: str
    role: Role = Role.SCHOLAR
    status: Status = Status.PENDING
    cohort: str = ""
    scholar_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    platform_access: list[PlatformAccess] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    notes: str = ""

    # ── helpers ──────────────────────────────

    def total_budget(self) -> float:
        return round(sum(p.budget_usd for p in self.platform_access), 4)

    def total_spent(self) -> float:
        return round(sum(p.spent_usd for p in self.platform_access), 4)

    def get_platform(self, platform: Platform) -> Optional[PlatformAccess]:
        return next((p for p in self.platform_access if p.platform == platform), None)

    def is_over_budget(self, threshold: float = 0.90) -> list[PlatformAccess]:
        """Return platforms where utilisation ≥ threshold."""
        return [p for p in self.platform_access if p.utilisation_pct >= threshold * 100]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Registry (JSON-backed for portability;
# swap store() / load() for Airtable / SQLite)
# ──────────────────────────────────────────────

class ScholarRegistry:
    """
    Persistent scholar registry.  Backed by a JSON file by default.
    Replace _load / _save with Airtable API calls in production.
    """

    def __init__(self, db_path: Path = Path("data/scholars.json")) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._scholars: dict[str, Scholar] = {}
        self._load()

    # ── CRUD ─────────────────────────────────

    def add(self, scholar: Scholar) -> Scholar:
        if self._by_email(scholar.email):
            raise ValueError(f"Scholar with email {scholar.email!r} already exists.")
        self._scholars[scholar.scholar_id] = scholar
        self._save()
        logger.info("Scholar added: %s (%s)", scholar.name, scholar.scholar_id)
        return scholar

    def get(self, scholar_id: str) -> Scholar:
        if scholar_id not in self._scholars:
            raise KeyError(f"Scholar {scholar_id!r} not found.")
        return self._scholars[scholar_id]

    def update(self, scholar_id: str, **kwargs) -> Scholar:
        s = self.get(scholar_id)
        for k, v in kwargs.items():
            if hasattr(s, k):
                setattr(s, k, v)
            else:
                raise AttributeError(f"Scholar has no attribute {k!r}.")
        s.updated_at = _now()
        self._save()
        return s

    def offboard(self, scholar_id: str, reason: str = "") -> Scholar:
        """Suspend access and revoke all platform keys."""
        s = self.get(scholar_id)
        s.status = Status.OFFBOARDED
        s.updated_at = _now()
        s.notes += f"\n[OFFBOARDED {_now()}] {reason}".strip()
        for p in s.platform_access:
            p.revoked_at = _now()
        self._save()
        logger.warning("Scholar offboarded: %s — %s", s.name, reason)
        return s

    def list_all(
        self,
        role: Optional[Role] = None,
        status: Optional[Status] = None,
        cohort: Optional[str] = None,
    ) -> list[Scholar]:
        results = list(self._scholars.values())
        if role:
            results = [s for s in results if s.role == role]
        if status:
            results = [s for s in results if s.status == status]
        if cohort:
            results = [s for s in results if s.cohort == cohort]
        return sorted(results, key=lambda s: s.name)

    # ── platform access helpers ───────────────

    def grant_platform(
        self,
        scholar_id: str,
        platform: Platform,
        username: str,
        budget_usd: float,
        api_key_id: Optional[str] = None,
    ) -> PlatformAccess:
        s = self.get(scholar_id)
        if s.get_platform(platform):
            raise ValueError(f"{s.name} already has access to {platform.value}.")
        access = PlatformAccess(
            platform=platform,
            username=username,
            budget_usd=budget_usd,
            api_key_id=api_key_id,
        )
        s.platform_access.append(access)
        s.updated_at = _now()
        self._save()
        logger.info("Granted %s access to %s ($%.2f)", s.name, platform.value, budget_usd)
        return access

    def revoke_platform(self, scholar_id: str, platform: Platform) -> None:
        s = self.get(scholar_id)
        access = s.get_platform(platform)
        if not access:
            raise ValueError(f"{s.name} has no access to {platform.value}.")
        access.revoked_at = _now()
        s.updated_at = _now()
        self._save()
        logger.warning("Revoked %s access to %s", s.name, platform.value)

    def record_spend(
        self, scholar_id: str, platform: Platform, amount_usd: float
    ) -> PlatformAccess:
        s = self.get(scholar_id)
        access = s.get_platform(platform)
        if not access:
            raise ValueError(f"{s.name} has no access to {platform.value}.")
        if access.revoked_at:
            raise PermissionError(f"{s.name}'s {platform.value} access is revoked.")
        access.spent_usd = round(access.spent_usd + amount_usd, 4)
        s.updated_at = _now()
        self._save()
        return access

    # ── budget audit ──────────────────────────

    def budget_alerts(self, warn_pct: float = 80.0, critical_pct: float = 95.0) -> list[dict]:
        alerts = []
        for s in self.list_all(status=Status.ACTIVE):
            for p in s.platform_access:
                if p.revoked_at:
                    continue
                if p.utilisation_pct >= critical_pct:
                    level = "CRITICAL"
                elif p.utilisation_pct >= warn_pct:
                    level = "WARNING"
                else:
                    continue
                alerts.append(
                    {
                        "level": level,
                        "scholar": s.name,
                        "scholar_id": s.scholar_id,
                        "platform": p.platform.value,
                        "spent": p.spent_usd,
                        "budget": p.budget_usd,
                        "utilisation_pct": p.utilisation_pct,
                    }
                )
        return sorted(alerts, key=lambda a: a["utilisation_pct"], reverse=True)

    # ── persistence ───────────────────────────

    def _load(self) -> None:
        if not self.db_path.exists():
            return
        with open(self.db_path) as f:
            raw = json.load(f)
        for sid, data in raw.items():
            # reconstruct nested dataclasses
            data["role"] = Role(data["role"])
            data["status"] = Status(data["status"])
            data["platform_access"] = [
                PlatformAccess(
                    platform=Platform(pa["platform"]),
                    username=pa["username"],
                    budget_usd=pa["budget_usd"],
                    spent_usd=pa["spent_usd"],
                    api_key_id=pa.get("api_key_id"),
                    granted_at=pa["granted_at"],
                    revoked_at=pa.get("revoked_at"),
                )
                for pa in data.get("platform_access", [])
            ]
            self._scholars[sid] = Scholar(**data)

    def _save(self) -> None:
        with open(self.db_path, "w") as f:
            json.dump(
                {sid: _scholar_to_dict(s) for sid, s in self._scholars.items()},
                f,
                indent=2,
            )

    # ── helpers ───────────────────────────────

    def _by_email(self, email: str) -> Optional[Scholar]:
        return next(
            (s for s in self._scholars.values() if s.email == email), None
        )


def _scholar_to_dict(s: Scholar) -> dict:
    d = asdict(s)
    d["role"] = s.role.value
    d["status"] = s.status.value
    for pa in d["platform_access"]:
        pa["platform"] = pa["platform"]  # already value from asdict with Enum
    # Fix: asdict() doesn't auto-convert nested Enum .value — patch manually
    for pa_obj, pa_dict in zip(s.platform_access, d["platform_access"]):
        pa_dict["platform"] = pa_obj.platform.value
    return d
