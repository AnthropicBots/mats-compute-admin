# compute_admin/__init__.py
"""
MATS Compute Admin
~~~~~~~~~~~~~~~~~~
Scholar lifecycle management, budget tracking, API key rotation,
HPC cluster monitoring, and security auditing for MATS Research.
"""

from compute_admin.scholar_manager import (
    Scholar, ScholarRegistry, Platform, Role, Status, PlatformAccess,
)
from compute_admin.budget_tracker import BudgetTracker
from compute_admin.api_key_manager import APIKeyManager
from compute_admin.cluster_monitor import ClusterMonitor
from compute_admin.request_handler import RequestHandler, RequestType, Priority
from compute_admin.platform_auditor import PlatformAuditor

__all__ = [
    "Scholar", "ScholarRegistry", "Platform", "Role", "Status", "PlatformAccess",
    "BudgetTracker",
    "APIKeyManager",
    "ClusterMonitor",
    "RequestHandler", "RequestType", "Priority",
    "PlatformAuditor",
]
