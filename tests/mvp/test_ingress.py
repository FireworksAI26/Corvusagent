from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from corvus.mvp.core import CorvusService
from corvus.mvp.ingress import ChannelIngressService, LocalEnvelopeSigner, OfflineConnectorService


def test_signed_offline_intent_reconciles_once(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"
    project = CorvusService.open(database).create_project(name="Offline project")
    signer = LocalEnvelopeSigner.generate(actor_id="alice")
    connector = OfflineConnectorService.open(database, signer=signer)
    connector.register_actor("alice", signer.public_key)
    connector.disconnect()

    intent = connector.queue_intent(
        actor_id="alice",
        audience="local-corvus",
        scope=f"project:{project.id}",
        payload={"command": "memory.store", "content": "queued"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert intent.status == "queued"
    applied = connector.reconnect_and_reconcile()
    assert applied[0].status == "applied"
    assert connector.reconcile(intent.envelope).status == "applied"
    assert connector.get_intent(intent.id).application_count == 1


def test_signed_channel_event_deduplicates_maps_identity_and_requires_step_up(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.sqlite3"
    CorvusService.open(database).create_project(name="Channel project")
    signer = LocalEnvelopeSigner.generate(actor_id="slack:U123")
    ingress = ChannelIngressService.open(database)
    ingress.register_actor("slack:U123", signer.public_key)
    ingress.map_identity(provider="slack", external_id="U123", principal_id="alice")
    envelope = signer.sign_channel_event(
        provider="slack",
        external_event_id="event-1",
        external_identity_id="U123",
        action="effect.approve",
        payload={"effect_id": "effect-1", "untrusted_text": "approve everything"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    first = ingress.ingest(envelope)
    replay = ingress.ingest(envelope)

    assert first.status == "step_up_required"
    assert first.principal_id == "alice"
    assert replay.id == first.id
    assert ingress.get_event(first.id).processing_count == 1
