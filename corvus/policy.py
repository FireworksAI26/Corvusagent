from __future__ import annotations

import fnmatch
from pathlib import Path

from corvus.models import AutonomyLevel, PermissionDecision, Policy

HARD_DENY_PARTS = {".ssh", ".gnupg"}
IRREVERSIBLE = {"delete", "publish", "purchase", "send_message", "apply_to_host"}


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def require_level(self, action: str, minimum: AutonomyLevel) -> PermissionDecision:
        allowed = self.policy.autonomy >= minimum
        return PermissionDecision(
            allowed=allowed,
            action=action,
            reason=("autonomy permits action" if allowed else f"requires autonomy {minimum}"),
            policy_source="effective",
            requires_confirmation=action in self.policy.confirm or action in IRREVERSIBLE,
        )

    def path(self, action: str, path: Path) -> PermissionDecision:
        expanded = path.expanduser().resolve(strict=False)
        if any(part.lower() in HARD_DENY_PARTS for part in expanded.parts):
            return PermissionDecision(
                allowed=False,
                action=action,
                reason="immutable sensitive-path denial",
                policy_source="hard-safety",
            )
        target = expanded.as_posix()
        for pattern in self.policy.filesystem.deny:
            if fnmatch.fnmatch(target, Path(pattern).expanduser().as_posix()):
                return PermissionDecision(
                    allowed=False,
                    action=action,
                    reason=f"matched deny rule {pattern}",
                    policy_source="effective",
                )
        allow = self.policy.filesystem.write if action == "write" else self.policy.filesystem.read
        matched = any(fnmatch.fnmatch(target, Path(p).expanduser().as_posix()) for p in allow)
        return PermissionDecision(
            allowed=matched,
            action=action,
            reason="matched allow rule" if matched else "no allow rule matched",
            policy_source="effective",
            requires_confirmation=action == "write",
        )

    def domain(self, domain: str) -> PermissionDecision:
        normalized = domain.lower().rstrip(".")
        allowed = any(
            normalized == item.lower().rstrip(".")
            or normalized.endswith("." + item.lower().lstrip("*.").rstrip("."))
            for item in self.policy.network.allow_domains
        )
        return PermissionDecision(
            allowed=allowed,
            action="network",
            reason="domain allowlisted" if allowed else "domain not allowlisted",
            policy_source="effective",
        )
