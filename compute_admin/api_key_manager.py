"""
compute_admin/api_key_manager.py
MATS Compute Admin — API key lifecycle management.

Never stores raw keys.  Maintains a registry of key metadata
(id, platform, scholar, created, rotated, revoked) to enforce
rotation policy and support security audits.

In production:
  - Raw keys live in a secrets manager (AWS Secrets Manager / HashiCorp Vault).
  - This module talks to that vault via its SDK.
  - The audit log here records *references*, not secrets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROTATION_DAYS: dict[str, int] = {
    "openai":      30,
    "anthropic":   30,
    "runpod":      60,
    "lambda_labs": 60,
    "aws":         90,
    "gcp":         90,
    "mats_cluster": 180,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class KeyRecord:
    key_id: str                     # unique reference ID (not the key)
    platform: str
    scholar_id: str
    scholar_name: str
    key_hash: str                   # SHA-256 prefix fingerprint for verification
    created_at: str = field(default_factory=_now)
    last_rotated_at: Optional[str] = None
    revoked_at: Optional[str] = None
    rotation_days: int = 30
    notes: str = ""

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def days_since_rotation(self) -> int:
        base = self.last_rotated_at or self.created_at
        dt = datetime.fromisoformat(base)
        return (datetime.now(timezone.utc) - dt).days

    @property
    def rotation_due(self) -> bool:
        return self.days_since_rotation >= self.rotation_days

    @property
    def days_until_rotation(self) -> int:
        return max(0, self.rotation_days - self.days_since_rotation)


class APIKeyManager:
    """
    Manages the full lifecycle of API key references across platforms.

    Real secret retrieval / rotation MUST be delegated to a vault.
    This class handles the metadata, audit log, and policy enforcement.
    """

    def __init__(self, registry_path: Path = Path("data/api_keys.json")) -> None:
        self._path = registry_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._keys: dict[str, KeyRecord] = {}
        self._load()

    # ── provisioning ─────────────────────────

    def register_key(
        self,
        raw_key: str,
        platform: str,
        scholar_id: str,
        scholar_name: str,
        notes: str = "",
    ) -> KeyRecord:
        """Register a new key reference.  raw_key is hashed immediately and never stored."""
        key_id = f"kid_{uuid.uuid4().hex[:10]}"
        fingerprint = _fingerprint(raw_key)
        rotation_days = _ROTATION_DAYS.get(platform.lower(), 30)
        record = KeyRecord(
            key_id=key_id,
            platform=platform,
            scholar_id=scholar_id,
            scholar_name=scholar_name,
            key_hash=fingerprint,
            rotation_days=rotation_days,
            notes=notes,
        )
        self._keys[key_id] = record
        self._save()
        logger.info(
            "Registered key %s for %s on %s (rotate every %d days)",
            key_id, scholar_name, platform, rotation_days,
        )
        return record

    def rotate_key(self, key_id: str, new_raw_key: str, reason: str = "") -> KeyRecord:
        """Mark a key as rotated; update fingerprint."""
        rec = self._get(key_id)
        rec.key_hash = _fingerprint(new_raw_key)
        rec.last_rotated_at = _now()
        rec.notes += f"\n[ROTATED {_now()}] {reason}".strip()
        self._save()
        logger.info("Rotated key %s (%s / %s)", key_id, rec.scholar_name, rec.platform)
        return rec

    def revoke_key(self, key_id: str, reason: str = "") -> KeyRecord:
        """Mark a key as revoked (e.g. scholar offboarded)."""
        rec = self._get(key_id)
        rec.revoked_at = _now()
        rec.notes += f"\n[REVOKED {_now()}] {reason}".strip()
        self._save()
        logger.warning("Revoked key %s (%s / %s) — %s", key_id, rec.scholar_name, rec.platform, reason)
        return rec

    def verify_key(self, key_id: str, raw_key: str) -> bool:
        """Check whether a raw key matches the stored fingerprint."""
        rec = self._get(key_id)
        return rec.key_hash == _fingerprint(raw_key)

    # ── policy audit ─────────────────────────

    def due_for_rotation(self) -> list[KeyRecord]:
        return [r for r in self._keys.values() if r.is_active and r.rotation_due]

    def expiring_soon(self, within_days: int = 7) -> list[KeyRecord]:
        return [
            r for r in self._keys.values()
            if r.is_active and 0 < r.days_until_rotation <= within_days
        ]

    def keys_for_scholar(self, scholar_id: str, active_only: bool = True) -> list[KeyRecord]:
        return [
            r for r in self._keys.values()
            if r.scholar_id == scholar_id and (not active_only or r.is_active)
        ]

    def security_audit(self) -> dict:
        """
        Full IAM-style audit snapshot suitable for a weekly review.
        Returns counts and lists for overdue rotations, expiring keys,
        revoked keys by scholar, and per-platform statistics.
        """
        overdue = self.due_for_rotation()
        expiring = self.expiring_soon(within_days=7)
        active = [r for r in self._keys.values() if r.is_active]
        revoked = [r for r in self._keys.values() if not r.is_active]

        platform_stats: dict[str, dict] = {}
        for r in self._keys.values():
            stats = platform_stats.setdefault(r.platform, {"active": 0, "revoked": 0, "overdue": 0})
            if r.is_active:
                stats["active"] += 1
                if r.rotation_due:
                    stats["overdue"] += 1
            else:
                stats["revoked"] += 1

        return {
            "generated_at": _now(),
            "summary": {
                "total_keys": len(self._keys),
                "active": len(active),
                "revoked": len(revoked),
                "overdue_rotation": len(overdue),
                "expiring_within_7d": len(expiring),
            },
            "overdue": [_key_to_audit_row(r) for r in overdue],
            "expiring_soon": [_key_to_audit_row(r) for r in expiring],
            "platform_stats": platform_stats,
        }

    # ── persistence ───────────────────────────

    def _get(self, key_id: str) -> KeyRecord:
        if key_id not in self._keys:
            raise KeyError(f"Key {key_id!r} not found.")
        return self._keys[key_id]

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            for kid, d in json.load(f).items():
                self._keys[kid] = KeyRecord(**d)

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(
                {kid: _key_record_to_dict(r) for kid, r in self._keys.items()},
                f, indent=2,
            )


# ── helpers ───────────────────────────────────

def _fingerprint(raw_key: str) -> str:
    """SHA-256 hex digest of key — used only for verification, never exposed."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _key_record_to_dict(r: KeyRecord) -> dict:
    return {
        "key_id": r.key_id,
        "platform": r.platform,
        "scholar_id": r.scholar_id,
        "scholar_name": r.scholar_name,
        "key_hash": r.key_hash,
        "created_at": r.created_at,
        "last_rotated_at": r.last_rotated_at,
        "revoked_at": r.revoked_at,
        "rotation_days": r.rotation_days,
        "notes": r.notes,
    }


def _key_to_audit_row(r: KeyRecord) -> dict:
    return {
        "key_id": r.key_id,
        "scholar": r.scholar_name,
        "platform": r.platform,
        "days_since_rotation": r.days_since_rotation,
        "days_until_due": r.days_until_rotation,
        "rotation_days": r.rotation_days,
    }
