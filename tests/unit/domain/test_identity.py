from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.identity import AgentIdentity, AgentStatus


def test_agent_identity_cannot_embed_credentials_or_capabilities() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentIdentity.model_validate(
            {
                "workspace_id": str(uuid4()),
                "name": "reviewer",
                "role": "security-reviewer",
                "model_route": "review",
                "skill_set_digest": "a" * 64,
                "status": AgentStatus.ACTIVE,
                "credential_value": "must-not-exist",
                "capabilities": ["workspace.admin"],
            }
        )

    forbidden = {
        tuple(error["loc"])
        for error in exc_info.value.errors()
        if error["type"] == "extra_forbidden"
    }
    assert forbidden == {("capabilities",), ("credential_value",)}
