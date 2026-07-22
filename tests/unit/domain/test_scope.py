from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.scope import AudiencePolicySnapshot, ConversationScope, ProjectScope


def test_conversation_scope_rejects_cross_workspace_parent() -> None:
    parent = ProjectScope(workspace_id=uuid4(), project_id=uuid4())

    with pytest.raises(ValidationError) as exc_info:
        ConversationScope(
            workspace_id=uuid4(),
            conversation_id=uuid4(),
            parent=parent,
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == "cross_workspace_scope_parent"


def test_personal_audience_requires_owner() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AudiencePolicySnapshot(
            workspace_id=uuid4(),
            visibility="personal",
            scope_digest="a" * 64,
            policy_version=1,
            policy_digest="b" * 64,
            created_by=uuid4(),
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "personal_visibility_requires_owner"
    )
