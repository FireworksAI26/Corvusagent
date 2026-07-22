from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from corvus.application.ports import ProjectAuditEvent
from corvus.domain.deployment import AuthorityCommitIntent, WorkspaceAuthority
from corvus.domain.identity import Project
from corvus.infrastructure.authority_root import AuthorityRootCalculator
from corvus.infrastructure.project_recovery import (
    ProjectAuthorityMutationPlan,
    project_mutation_digest,
)


class ManifestProjectAuthorityPlannerError(RuntimeError):
    pass


class ManifestProjectAuthorityMutationPlanner:
    def __init__(self, database: Path) -> None:
        self.calculator = AuthorityRootCalculator(database)

    def plan(
        self,
        project: Project,
        event: ProjectAuditEvent,
        authority: WorkspaceAuthority,
        intent: AuthorityCommitIntent,
    ) -> ProjectAuthorityMutationPlan:
        self._validate_bindings(project, event, authority, intent)
        target_authority = self._target_authority(authority, intent)
        replacements = {
            "projects": self._upsert(
                self.calculator.project_family_rows(
                    workspace_id=project.workspace_id,
                    family_name="projects",
                ),
                self._project_row(project),
            ),
            "authority_commit_intents": self._upsert(
                self.calculator.project_family_rows(
                    workspace_id=project.workspace_id,
                    family_name="authority_commit_intents",
                ),
                self._intent_row(intent),
            ),
            "workspace_authorities": self._upsert(
                self.calculator.project_family_rows(
                    workspace_id=project.workspace_id,
                    family_name="workspace_authorities",
                ),
                self._authority_row(target_authority),
            ),
        }
        calculation = self.calculator.calculate(
            workspace_id=project.workspace_id,
            authority_generation=intent.next_generation,
            prospective_family_rows=replacements,
            external_proof_digests={
                "audit_anchor_recovery_checkpoints": event.authority_proof_digest,
                "audit_result_bindings": event.authority_proof_digest,
                "authority_registry_freshness_proofs": event.authority_proof_digest,
            },
        )
        return ProjectAuthorityMutationPlan(
            mutation_digest=project_mutation_digest(project),
            calculation=calculation,
        )

    @staticmethod
    def _validate_bindings(
        project: Project,
        event: ProjectAuditEvent,
        authority: WorkspaceAuthority,
        intent: AuthorityCommitIntent,
    ) -> None:
        if (
            event.action != "project.create"
            or event.decision != "allow"
            or event.workspace_id != project.workspace_id
            or event.workspace_id != authority.workspace_id
            or event.workspace_id != intent.workspace_id
            or event.project_id != project.id
            or intent.epoch != authority.epoch
            or intent.deployment_instance_id != authority.deployment_instance_id
            or intent.next_generation != intent.prior_generation + 1
            or intent.mutation_digest != project_mutation_digest(project)
        ):
            raise ManifestProjectAuthorityPlannerError("project_authority_plan_binding_mismatch")

    @staticmethod
    def _target_authority(
        authority: WorkspaceAuthority,
        intent: AuthorityCommitIntent,
    ) -> WorkspaceAuthority:
        prior_matches = (
            authority.authority_generation == intent.prior_generation
            and authority.authority_state_root == intent.prior_state_root
        )
        finalized_matches = (
            authority.authority_generation == intent.next_generation
            and authority.authority_state_root == intent.proposed_state_root
        )
        if prior_matches:
            return authority.model_copy(
                update={
                    "authority_generation": intent.next_generation,
                    "authority_state_root": intent.proposed_state_root,
                    "version": authority.version + 1,
                }
            )
        if finalized_matches:
            return authority
        raise ManifestProjectAuthorityPlannerError("project_authority_plan_prior_state_mismatch")

    @staticmethod
    def _upsert(
        rows: Sequence[Mapping[str, Any]],
        replacement: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        replacement_id = replacement.get("id")
        if replacement_id is None:
            raise ManifestProjectAuthorityPlannerError("project_authority_plan_identity_missing")
        updated = [dict(row) for row in rows if row.get("id") != replacement_id]
        updated.append(dict(replacement))
        return tuple(updated)

    @staticmethod
    def _project_row(project: Project) -> dict[str, object]:
        return {
            "id": str(project.id),
            "workspace_id": str(project.workspace_id),
            "name": project.name,
            "root_locator": project.root_locator,
            "privacy": project.privacy,
            "status": project.status.value,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "version": project.version,
        }

    @staticmethod
    def _intent_row(intent: AuthorityCommitIntent) -> dict[str, object]:
        return {
            "id": str(intent.id),
            "workspace_id": str(intent.workspace_id),
            "epoch": intent.epoch,
            "deployment_instance_id": str(intent.deployment_instance_id),
            "prior_generation": intent.prior_generation,
            "next_generation": intent.next_generation,
            "prior_state_root": intent.prior_state_root,
            "mutation_digest": intent.mutation_digest,
            "proposed_state_root": intent.proposed_state_root,
            "state": intent.state.value,
            "created_at": intent.created_at.isoformat(),
            "payload_json": intent.model_dump_json(),
        }

    @staticmethod
    def _authority_row(authority: WorkspaceAuthority) -> dict[str, object]:
        return {
            "id": str(authority.id),
            "workspace_id": str(authority.workspace_id),
            "deployment_profile_id": str(authority.deployment_profile_id),
            "deployment_instance_id": str(authority.deployment_instance_id),
            "epoch": authority.epoch,
            "authority_generation": authority.authority_generation,
            "authority_state_root": authority.authority_state_root,
            "authority_epoch_credential_id": str(authority.authority_epoch_credential_id),
            "trust_anchor_id": str(authority.trust_anchor_id),
            "active_lease_id": (
                None if authority.active_lease_id is None else str(authority.active_lease_id)
            ),
            "state": authority.state.value,
            "version": authority.version,
            "payload_json": authority.model_dump_json(),
        }
