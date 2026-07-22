from __future__ import annotations

from corvus.mvp.openapi import build_openapi_document


def test_openapi_document_contains_connected_operator_routes() -> None:
    document = build_openapi_document()
    paths = document["paths"]

    assert "/api/projects" in paths
    assert "/api/projects/{project_id}/outcomes" in paths
    assert "/api/outcomes/{outcome_id}/workflows" in paths
    assert "/api/workflows/{workflow_id}/events" in paths


def test_unavailable_v2_openapi_keeps_typed_requests_responses_and_errors() -> None:
    document = build_openapi_document()
    schemas = document["components"]["schemas"]
    paths = document["paths"]

    assert "OnboardingUpdate" in schemas
    assert "WorkspaceCreate" in schemas
    assert "SyncMutationBatch" in schemas
    assert "SessionResponse" in schemas
    assert "Workspace" in schemas
    assert "SyncPage" in schemas
    assert "ApiErrorResponse" in schemas
    assert paths["/api/v2/session"]["get"]["responses"]["401"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/ApiErrorResponse")
    assert paths["/api/v2/workspaces/{workspace_id}/sync"]["get"]["responses"]["503"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/ApiErrorResponse")
