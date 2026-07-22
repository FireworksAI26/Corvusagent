from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.deployment import (
    AuthorityStateRootCalculation,
    AuthorityStateRootLeafCommitment,
    AuthorityStateRootManifestVersion,
    canonical_authority_leaf_digest,
    canonical_authority_root_digest,
)


def test_authority_leaf_digest_is_order_stable_and_family_bound() -> None:
    first = canonical_authority_leaf_digest(
        family_name="projects",
        canonicalization_version=1,
        records=[{"b": 2, "a": 1}],
    )
    reordered = canonical_authority_leaf_digest(
        family_name="projects",
        canonicalization_version=1,
        records=[{"a": 1, "b": 2}],
    )
    other_family = canonical_authority_leaf_digest(
        family_name="principals",
        canonicalization_version=1,
        records=[{"a": 1, "b": 2}],
    )

    assert first == reordered
    assert first != other_family


def test_authority_root_binds_workspace_manifest_generation_and_ordered_leaves() -> None:
    workspace_id = uuid4()
    manifest = AuthorityStateRootManifestVersion(
        manifest_digest="a" * 64,
        schema_version=5,
        canonicalization_version=1,
    )
    commitments = [
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=3,
            ordinal=1,
            family_name="projects",
            record_version=1,
            leaf_digest="b" * 64,
        ),
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=3,
            ordinal=2,
            family_name="workspace_authorities",
            record_version=2,
            leaf_digest="c" * 64,
        ),
    ]

    root = canonical_authority_root_digest(
        workspace_id=workspace_id,
        manifest=manifest,
        authority_generation=3,
        commitments=list(reversed(commitments)),
    )

    assert root == canonical_authority_root_digest(
        workspace_id=workspace_id,
        manifest=manifest,
        authority_generation=3,
        commitments=commitments,
    )
    assert root != canonical_authority_root_digest(
        workspace_id=uuid4(),
        manifest=manifest,
        authority_generation=3,
        commitments=commitments,
    )
    assert root != canonical_authority_root_digest(
        workspace_id=workspace_id,
        manifest=manifest,
        authority_generation=4,
        commitments=[
            commitment.model_copy(update={"authority_generation": 4}) for commitment in commitments
        ],
    )

    calculation = AuthorityStateRootCalculation(
        workspace_id=workspace_id,
        manifest=manifest,
        authority_generation=3,
        commitments=tuple(commitments),
        observed_leaf_digests={item.family_name: item.leaf_digest for item in commitments},
        root_digest=root,
    )
    assert calculation.root_digest == root
    with pytest.raises(ValidationError) as exc_info:
        AuthorityStateRootCalculation.model_validate(
            {**calculation.model_dump(mode="json"), "root_digest": "f" * 64}
        )
    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == "authority_root_digest_mismatch"


def test_authority_root_rejects_non_exhaustive_ordering() -> None:
    manifest = AuthorityStateRootManifestVersion(
        manifest_digest="a" * 64,
        schema_version=5,
        canonicalization_version=1,
    )
    commitment = AuthorityStateRootLeafCommitment(
        manifest_version_id=manifest.id,
        authority_generation=1,
        ordinal=2,
        family_name="projects",
        record_version=1,
        leaf_digest="b" * 64,
    )

    with pytest.raises(ValueError, match="exhaustive ordered generation"):
        canonical_authority_root_digest(
            workspace_id=uuid4(),
            manifest=manifest,
            authority_generation=1,
            commitments=[commitment],
        )
    with pytest.raises(ValueError, match="exhaustive ordered generation"):
        canonical_authority_root_digest(
            workspace_id=uuid4(),
            manifest=manifest,
            authority_generation=1,
            commitments=[],
        )
