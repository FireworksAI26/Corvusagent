from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from corvus.domain.identity import (
    AgentIdentity,
    Principal,
    PrincipalKind,
    Workspace,
    WorkspaceMembership,
)
from corvus.domain.scope import ConversationScope, ProjectScope, ThreadScope
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.identity_scope import IdentityScopeRepository
from corvus.store import TraceStore


def _repository(tmp_path: Path) -> IdentityScopeRepository:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return IdentityScopeRepository(database)


def test_identity_versions_and_membership_are_workspace_scoped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = Workspace(name="Corvus")
    principal = Principal(
        kind=PrincipalKind.USER,
        external_provider="local",
        external_subject="lucas",
        display_name="Lucas",
    )
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        principal_id=principal.id,
        role="owner",
    )
    agent = AgentIdentity(
        workspace_id=workspace.id,
        name="Corvus",
        role="assistant",
        model_route="openai/gpt",
        skill_set_digest="1" * 64,
    )

    repository.append_workspace(workspace)
    repository.append_principal(principal)
    repository.append_membership(membership)
    repository.append_agent(agent)

    assert repository.get_workspace(workspace.id) == workspace
    assert repository.get_principal(principal.id) == principal
    assert repository.get_membership(workspace.id, principal.id) == membership
    assert repository.get_membership(uuid4(), principal.id) is None
    assert repository.get_agent(workspace.id, agent.id) == agent
    assert repository.get_agent(uuid4(), agent.id) is None


def test_discriminated_scope_round_trip_preserves_parent_and_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace_id = uuid4()
    project = ProjectScope(workspace_id=workspace_id, project_id=uuid4())
    thread = ThreadScope(
        workspace_id=workspace_id,
        project_id=project.project_id,
        channel_id=uuid4(),
        thread_id=uuid4(),
    )
    conversation = ConversationScope(
        workspace_id=workspace_id,
        conversation_id=uuid4(),
        parent=thread,
    )

    for scope in (project, thread, conversation):
        repository.append_scope(scope)

    assert repository.get_scope(workspace_id, "project", project.project_id) == project
    assert repository.get_scope(workspace_id, "thread", thread.thread_id) == thread
    assert (
        repository.get_scope(workspace_id, "conversation", conversation.conversation_id)
        == conversation
    )
    assert repository.get_scope(uuid4(), "conversation", conversation.conversation_id) is None
