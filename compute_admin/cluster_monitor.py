"""
compute_admin/cluster_monitor.py
MATS Compute Admin — HPC / Slurm cluster monitoring and job support.

Parses `squeue` / `sacct` output (or their mock equivalents in tests),
tracks job history, detects stalled jobs, and surfaces per-scholar
GPU utilisation to guide budget and capacity planning.

Requires: Slurm command-line tools on PATH in production.
          Falls back to a mock data mode for CI / local dev.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MOCK_MODE: bool = shutil.which("squeue") is None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Domain models
# ──────────────────────────────────────────────

class JobState(str, Enum):
    PENDING   = "PENDING"
    RUNNING   = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED    = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT   = "TIMEOUT"
    NODE_FAIL = "NODE_FAIL"


@dataclass
class SlurmJob:
    job_id: str
    scholar_id: str
    username: str
    name: str
    state: JobState
    partition: str
    nodes: int
    cpus: int
    gpus: int
    submit_time: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    elapsed_seconds: int = 0
    gpu_hours: float = 0.0
    exit_code: str = "0:0"

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            JobState.COMPLETED, JobState.FAILED,
            JobState.CANCELLED, JobState.TIMEOUT, JobState.NODE_FAIL,
        }

    @property
    def elapsed_hours(self) -> float:
        return round(self.elapsed_seconds / 3600, 4)


# ──────────────────────────────────────────────
# ClusterMonitor
# ──────────────────────────────────────────────

class ClusterMonitor:
    """
    Polls Slurm for live queue status and job accounting.
    Falls back to MOCK_MODE when Slurm is not installed (CI / local dev).

    Key capabilities:
    - List pending / running jobs per scholar
    - Detect stalled jobs (running > max_runtime_hours)
    - Aggregate GPU-hours consumed per scholar
    - Surface failed jobs needing admin attention
    """

    def __init__(
        self,
        history_path: Path = Path("data/job_history.jsonl"),
        max_runtime_hours: float = 24.0,
    ) -> None:
        self.history_path = history_path
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_runtime_hours = max_runtime_hours
        if MOCK_MODE:
            logger.info("ClusterMonitor: Slurm not found — running in mock mode.")

    # ── live queue ───────────────────────────

    def active_jobs(self) -> list[SlurmJob]:
        """Return currently PENDING or RUNNING jobs."""
        if MOCK_MODE:
            return _mock_active_jobs()
        return self._parse_squeue()

    def stalled_jobs(self) -> list[SlurmJob]:
        """Jobs that have been RUNNING longer than max_runtime_hours."""
        return [
            j for j in self.active_jobs()
            if j.state == JobState.RUNNING and j.elapsed_hours > self.max_runtime_hours
        ]

    # ── accounting ───────────────────────────

    def scholar_gpu_hours(self, since_iso: Optional[str] = None) -> dict[str, float]:
        """Aggregate completed GPU-hours per scholar_id from job history."""
        totals: dict[str, float] = {}
        if MOCK_MODE:
            jobs = _mock_completed_jobs()
        else:
            jobs = self._parse_sacct(since_iso=since_iso)
        for j in jobs:
            if j.state == JobState.COMPLETED:
                totals[j.scholar_id] = round(
                    totals.get(j.scholar_id, 0.0) + j.gpu_hours, 4
                )
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    def failed_jobs(self, since_iso: Optional[str] = None) -> list[SlurmJob]:
        """Return FAILED / TIMEOUT / NODE_FAIL jobs needing admin review."""
        if MOCK_MODE:
            jobs = _mock_completed_jobs()
        else:
            jobs = self._parse_sacct(since_iso=since_iso)
        return [
            j for j in jobs
            if j.state in {JobState.FAILED, JobState.TIMEOUT, JobState.NODE_FAIL}
        ]

    # ── job submission helpers ────────────────

    def submit_job(self, sbatch_script: str, dry_run: bool = False) -> Optional[str]:
        """Submit a job via sbatch. Returns job_id string."""
        if dry_run or MOCK_MODE:
            fake_id = f"mock_{_now()[:10]}_001"
            logger.info("[DRY RUN] Would submit job → %s", fake_id)
            return fake_id
        result = subprocess.run(
            ["sbatch", "--parsable", "--wrap", sbatch_script],
            capture_output=True, text=True, check=True,
        )
        job_id = result.stdout.strip()
        logger.info("Submitted job %s", job_id)
        return job_id

    def cancel_job(self, job_id: str, reason: str = "", dry_run: bool = False) -> bool:
        """Cancel a running or pending job via scancel."""
        if dry_run or MOCK_MODE:
            logger.info("[DRY RUN] Would cancel job %s — %s", job_id, reason)
            return True
        result = subprocess.run(
            ["scancel", job_id],
            capture_output=True, text=True,
        )
        success = result.returncode == 0
        if success:
            logger.warning("Cancelled job %s — %s", job_id, reason)
        else:
            logger.error("Failed to cancel job %s: %s", job_id, result.stderr)
        return success

    # ── cluster health snapshot ───────────────

    def health_snapshot(self) -> dict:
        """High-level cluster status for the admin dashboard."""
        active = self.active_jobs()
        stalled = self.stalled_jobs()
        failed = self.failed_jobs()
        gpu_hours = self.scholar_gpu_hours()

        running  = [j for j in active if j.state == JobState.RUNNING]
        pending  = [j for j in active if j.state == JobState.PENDING]
        total_gpus_in_use = sum(j.gpus for j in running)

        return {
            "generated_at": _now(),
            "mode": "mock" if MOCK_MODE else "live",
            "running_jobs": len(running),
            "pending_jobs": len(pending),
            "stalled_jobs": len(stalled),
            "failed_jobs_recent": len(failed),
            "gpus_in_use": total_gpus_in_use,
            "top_gpu_consumers": list(gpu_hours.items())[:5],
            "stalled_details": [
                {"job_id": j.job_id, "user": j.username, "elapsed_h": j.elapsed_hours}
                for j in stalled
            ],
        }

    # ── Slurm parsing ─────────────────────────

    def _parse_squeue(self) -> list[SlurmJob]:
        """
        Run squeue with a parsable format and convert to SlurmJob objects.
        Format: JobID|User|Name|State|Partition|NumNodes|NumCPUs|Tres|SubmitTime|StartTime|TimeElapsed
        """
        fmt = "%i|%u|%j|%T|%P|%D|%C|%b|%V|%S|%M"
        result = subprocess.run(
            ["squeue", "--noheader", f"--format={fmt}"],
            capture_output=True, text=True, check=True,
        )
        jobs = []
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 11:
                continue
            job_id, username, name, state, partition, nodes, cpus, tres, submit, start, elapsed = parts[:11]
            gpus = _extract_gpus_from_tres(tres)
            elapsed_s = _slurm_time_to_seconds(elapsed)
            jobs.append(
                SlurmJob(
                    job_id=job_id,
                    scholar_id=username,
                    username=username,
                    name=name,
                    state=JobState(state) if state in JobState._value2member_map_ else JobState.PENDING,
                    partition=partition,
                    nodes=int(nodes),
                    cpus=int(cpus),
                    gpus=gpus,
                    submit_time=submit,
                    start_time=start if start != "N/A" else None,
                    elapsed_seconds=elapsed_s,
                    gpu_hours=round(gpus * elapsed_s / 3600, 4),
                )
            )
        return jobs

    def _parse_sacct(self, since_iso: Optional[str] = None) -> list[SlurmJob]:
        """Parse sacct for completed / failed jobs."""
        args = [
            "sacct", "--noheader", "--parsable2",
            "--format=JobID,User,JobName,State,Partition,NNodes,NCPUS,ReqTRES,"
                     "Submit,Start,End,ElapsedRaw,ExitCode",
        ]
        if since_iso:
            date_str = since_iso[:10]  # YYYY-MM-DD
            args += ["--starttime", date_str]
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        jobs = []
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 13 or ".batch" in parts[0]:
                continue
            job_id, username, name, state, partition, nodes, cpus, tres, submit, start, end, elapsed_raw, exit_code = parts[:13]
            try:
                state_enum = JobState(state.split(" ")[0])
            except ValueError:
                continue
            gpus = _extract_gpus_from_tres(tres)
            elapsed_s = int(elapsed_raw) if elapsed_raw.isdigit() else 0
            jobs.append(
                SlurmJob(
                    job_id=job_id,
                    scholar_id=username,
                    username=username,
                    name=name,
                    state=state_enum,
                    partition=partition,
                    nodes=int(nodes) if nodes.isdigit() else 1,
                    cpus=int(cpus) if cpus.isdigit() else 1,
                    gpus=gpus,
                    submit_time=submit,
                    start_time=start,
                    end_time=end,
                    elapsed_seconds=elapsed_s,
                    gpu_hours=round(gpus * elapsed_s / 3600, 4),
                    exit_code=exit_code,
                )
            )
        return jobs


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _extract_gpus_from_tres(tres: str) -> int:
    """Parse TRES string like 'billing=1,cpu=4,gres/gpu=2,mem=16G,node=1'."""
    for part in tres.split(","):
        if "gres/gpu=" in part:
            try:
                return int(part.split("=")[1])
            except ValueError:
                return 0
    return 0


def _slurm_time_to_seconds(elapsed: str) -> int:
    """Convert Slurm elapsed time 'D-HH:MM:SS' or 'HH:MM:SS' to seconds."""
    if not elapsed or elapsed in {"N/A", "INVALID"}:
        return 0
    days = 0
    if "-" in elapsed:
        d, elapsed = elapsed.split("-", 1)
        days = int(d)
    parts = elapsed.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return 0
    except ValueError:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s


# ──────────────────────────────────────────────
# Mock data (used when Slurm is absent)
# ──────────────────────────────────────────────

def _mock_active_jobs() -> list[SlurmJob]:
    return [
        SlurmJob("5001", "alice_s", "alice_s", "interp_run_1", JobState.RUNNING, "gpu", 1, 8, 2, _now(), _now(), elapsed_seconds=7200, gpu_hours=4.0),
        SlurmJob("5002", "bob_s",   "bob_s",   "rl_finetune",  JobState.RUNNING, "gpu", 1, 16, 4, _now(), _now(), elapsed_seconds=90000, gpu_hours=100.0),  # stalled
        SlurmJob("5003", "carol_s", "carol_s", "evals_batch",  JobState.PENDING, "cpu", 1, 4, 0, _now()),
        SlurmJob("5004", "dave_s",  "dave_s",  "safety_probe", JobState.RUNNING, "gpu", 2, 16, 8, _now(), _now(), elapsed_seconds=3600, gpu_hours=8.0),
    ]


def _mock_completed_jobs() -> list[SlurmJob]:
    return [
        SlurmJob("4900", "alice_s", "alice_s", "baseline_run", JobState.COMPLETED, "gpu", 1, 8, 2, _now(), elapsed_seconds=14400, gpu_hours=8.0),
        SlurmJob("4901", "bob_s",   "bob_s",   "rlhf_exp",     JobState.FAILED,    "gpu", 1, 16, 4, _now(), elapsed_seconds=600,   gpu_hours=0.67, exit_code="1:0"),
        SlurmJob("4902", "carol_s", "carol_s", "attn_scan",    JobState.COMPLETED, "gpu", 1, 8, 2, _now(), elapsed_seconds=21600,  gpu_hours=12.0),
        SlurmJob("4903", "eve_s",   "eve_s",   "mech_interp",  JobState.TIMEOUT,   "gpu", 2, 16, 8, _now(), elapsed_seconds=86400, gpu_hours=192.0, exit_code="0:15"),
    ]
