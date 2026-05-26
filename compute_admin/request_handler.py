"""
compute_admin/request_handler.py
MATS Compute Admin — Compute request triage and lifecycle management.

Scholars and mentors submit requests for additional compute (GPU time,
API budget top-ups, new platform access).  This module manages the
queue, approval workflow, SLA tracking, and audit log.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SLA_RESPONSE_HOURS = 4    # target first-response SLA
SLA_RESOLUTION_HOURS = 24 # target resolution SLA


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Domain models
# ──────────────────────────────────────────────

class RequestType(str, Enum):
    BUDGET_TOPUP        = "budget_topup"
    NEW_PLATFORM_ACCESS = "new_platform_access"
    GPU_ALLOCATION      = "gpu_allocation"
    API_KEY_NEW         = "api_key_new"
    API_KEY_ROTATION    = "api_key_rotation"
    ACCOUNT_SETUP       = "account_setup"
    TROUBLESHOOT        = "troubleshoot"
    OTHER               = "other"


class Priority(str, Enum):
    LOW      = "low"
    NORMAL   = "normal"
    HIGH     = "high"
    URGENT   = "urgent"   # e.g. paper deadline within 24h


class RequestStatus(str, Enum):
    OPEN        = "open"
    IN_PROGRESS = "in_progress"
    PENDING_INFO = "pending_info"   # waiting on scholar clarification
    APPROVED    = "approved"
    REJECTED    = "rejected"
    RESOLVED    = "resolved"


@dataclass
class Comment:
    author: str
    text: str
    timestamp: str = field(default_factory=_now)


@dataclass
class ComputeRequest:
    scholar_id: str
    scholar_name: str
    request_type: RequestType
    description: str
    platform: str = ""
    amount_usd: Optional[float] = None       # for budget top-ups
    gpu_hours: Optional[float] = None        # for GPU allocations
    priority: Priority = Priority.NORMAL
    status: RequestStatus = RequestStatus.OPEN
    request_id: str = field(default_factory=lambda: f"REQ-{uuid.uuid4().hex[:6].upper()}")
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    resolved_at: Optional[str] = None
    assigned_to: Optional[str] = None
    comments: list[Comment] = field(default_factory=list)

    # ── SLA helpers ──────────────────────────

    @property
    def age_hours(self) -> float:
        created = datetime.fromisoformat(self.created_at)
        return round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 2)

    @property
    def is_sla_breached(self) -> bool:
        if self.status in {RequestStatus.RESOLVED, RequestStatus.REJECTED}:
            return False
        return self.age_hours > SLA_RESOLUTION_HOURS

    @property
    def is_response_sla_breached(self) -> bool:
        if self.status != RequestStatus.OPEN:
            return False
        return self.age_hours > SLA_RESPONSE_HOURS


# ──────────────────────────────────────────────
# RequestHandler
# ──────────────────────────────────────────────

class RequestHandler:
    """
    Manages the compute request queue: triage, assign, approve/reject, resolve.

    Designed to integrate with Airtable as the backing store in production.
    Falls back to local JSON for dev / testing.
    """

    def __init__(self, queue_path: Path = Path("data/requests.json")) -> None:
        self._path = queue_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._requests: dict[str, ComputeRequest] = {}
        self._load()

    # ── CRUD ─────────────────────────────────

    def submit(
        self,
        scholar_id: str,
        scholar_name: str,
        request_type: RequestType,
        description: str,
        platform: str = "",
        amount_usd: Optional[float] = None,
        gpu_hours: Optional[float] = None,
        priority: Priority = Priority.NORMAL,
    ) -> ComputeRequest:
        req = ComputeRequest(
            scholar_id=scholar_id,
            scholar_name=scholar_name,
            request_type=request_type,
            description=description,
            platform=platform,
            amount_usd=amount_usd,
            gpu_hours=gpu_hours,
            priority=priority,
        )
        self._requests[req.request_id] = req
        self._save()
        logger.info(
            "Request submitted: %s — %s (%s) by %s",
            req.request_id, request_type.value, priority.value, scholar_name,
        )
        return req

    def assign(self, request_id: str, admin_username: str) -> ComputeRequest:
        req = self._get(request_id)
        req.assigned_to = admin_username
        req.status = RequestStatus.IN_PROGRESS
        req.updated_at = _now()
        self._save()
        return req

    def add_comment(self, request_id: str, author: str, text: str) -> ComputeRequest:
        req = self._get(request_id)
        req.comments.append(Comment(author=author, text=text))
        req.updated_at = _now()
        self._save()
        return req

    def approve(self, request_id: str, admin: str, note: str = "") -> ComputeRequest:
        return self._transition(request_id, RequestStatus.APPROVED, admin, note)

    def reject(self, request_id: str, admin: str, reason: str = "") -> ComputeRequest:
        return self._transition(request_id, RequestStatus.REJECTED, admin, reason)

    def resolve(self, request_id: str, admin: str, note: str = "") -> ComputeRequest:
        req = self._transition(request_id, RequestStatus.RESOLVED, admin, note)
        req.resolved_at = _now()
        self._save()
        return req

    # ── queue views ──────────────────────────

    def open_queue(self, sort_by_priority: bool = True) -> list[ComputeRequest]:
        reqs = [r for r in self._requests.values() if r.status == RequestStatus.OPEN]
        if sort_by_priority:
            order = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.NORMAL: 2, Priority.LOW: 3}
            reqs.sort(key=lambda r: (order[r.priority], r.created_at))
        return reqs

    def breached_sla(self) -> list[ComputeRequest]:
        return [r for r in self._requests.values() if r.is_sla_breached]

    def response_sla_at_risk(self) -> list[ComputeRequest]:
        return [r for r in self._requests.values() if r.is_response_sla_breached]

    def scholar_history(self, scholar_id: str) -> list[ComputeRequest]:
        return sorted(
            [r for r in self._requests.values() if r.scholar_id == scholar_id],
            key=lambda r: r.created_at, reverse=True,
        )

    # ── triage heuristics ─────────────────────

    def auto_triage(self, request_id: str) -> Priority:
        """
        Suggest a priority level based on request type, amount, and scholar history.
        Returns the suggested Priority (does NOT mutate the record).
        """
        req = self._get(request_id)
        # Explicit overrides
        if req.request_type in {RequestType.TROUBLESHOOT}:
            if req.amount_usd and req.amount_usd > 500:
                return Priority.URGENT
        if req.request_type == RequestType.BUDGET_TOPUP:
            if req.amount_usd and req.amount_usd > 1000:
                return Priority.HIGH
        # Recency / frequency: flag scholars with multiple open requests
        scholar_opens = [
            r for r in self._requests.values()
            if r.scholar_id == req.scholar_id and r.status == RequestStatus.OPEN
        ]
        if len(scholar_opens) >= 3:
            return Priority.HIGH
        return Priority.NORMAL

    # ── dashboard summary ─────────────────────

    def queue_stats(self) -> dict:
        total = len(self._requests)
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for r in self._requests.values():
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            by_type[r.request_type.value] = by_type.get(r.request_type.value, 0) + 1
            by_priority[r.priority.value] = by_priority.get(r.priority.value, 0) + 1
        breached = self.breached_sla()
        return {
            "total_requests": total,
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "sla_breached": len(breached),
            "open_queue_depth": by_status.get("open", 0),
        }

    # ── persistence ───────────────────────────

    def _get(self, request_id: str) -> ComputeRequest:
        if request_id not in self._requests:
            raise KeyError(f"Request {request_id!r} not found.")
        return self._requests[request_id]

    def _transition(
        self, request_id: str, new_status: RequestStatus, admin: str, note: str
    ) -> ComputeRequest:
        req = self._get(request_id)
        old = req.status
        req.status = new_status
        req.updated_at = _now()
        if note:
            req.comments.append(Comment(author=admin, text=f"[{new_status.value.upper()}] {note}"))
        self._save()
        logger.info("Request %s: %s → %s by %s", request_id, old.value, new_status.value, admin)
        return req

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            for rid, d in json.load(f).items():
                d["request_type"] = RequestType(d["request_type"])
                d["priority"] = Priority(d["priority"])
                d["status"] = RequestStatus(d["status"])
                d["comments"] = [Comment(**c) for c in d.get("comments", [])]
                self._requests[rid] = ComputeRequest(**d)

    def _save(self) -> None:
        def _to_dict(r: ComputeRequest) -> dict:
            return {
                "scholar_id": r.scholar_id,
                "scholar_name": r.scholar_name,
                "request_type": r.request_type.value,
                "description": r.description,
                "platform": r.platform,
                "amount_usd": r.amount_usd,
                "gpu_hours": r.gpu_hours,
                "priority": r.priority.value,
                "status": r.status.value,
                "request_id": r.request_id,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "resolved_at": r.resolved_at,
                "assigned_to": r.assigned_to,
                "comments": [{"author": c.author, "text": c.text, "timestamp": c.timestamp} for c in r.comments],
            }

        with open(self._path, "w") as f:
            json.dump({rid: _to_dict(r) for rid, r in self._requests.items()}, f, indent=2)
