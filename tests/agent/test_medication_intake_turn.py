"""Focused privacy and lifecycle contracts for the Track U turn envelope."""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
import pickle
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import Barrier, Thread
from types import MappingProxyType

import pytest

from agent.medication_intake_turn import (
    ActiveMedicationIntakeTurnRegistry,
    MedicationIntakeTurnEnvelope,
    MedicationTurnError,
    TrustedMedicationSource,
    build_medication_intake_turn_envelope,
)


KEY = "0123456789abcdef" * 4
DIGEST = "89abcdef01234567" * 4
TURN_ID = "11223344556677889900aabbccddeeff" * 2
AUTHORITY_DIGEST = "fedcba9876543210" * 4
SCOPE_DIGEST = "00112233445566778899aabbccddeeff" * 2


def _envelope() -> MedicationIntakeTurnEnvelope:
    return build_medication_intake_turn_envelope(
        (KEY,),
        turn_id=TURN_ID,
        trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
        scope_digest=SCOPE_DIGEST,
        authority_digest=AUTHORITY_DIGEST,
        catalog_digest=DIGEST,
        occurred_at=datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc),
    )


def _registry_kwargs(now: float) -> dict[str, object]:
    return {
        "turn_id": TURN_ID,
        "trusted_source": TrustedMedicationSource.DIRECT_USER_TEXT,
        "scope_digest": SCOPE_DIGEST,
        "authority_digest": AUTHORITY_DIGEST,
        "catalog_digest": DIGEST,
        "now": now,
    }


def _assert_private_absent(marker: str, *surfaces: object) -> None:
    assert marker not in "\n".join(str(surface) for surface in surfaces)


def test_envelope_is_frozen_sorted_raw_free_and_constant_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "합성알파"
    with caplog.at_level(logging.DEBUG):
        envelope = _envelope()
    _assert_private_absent(marker, envelope, repr(envelope), caplog.text)
    assert envelope.medication_keys == (KEY,)
    assert envelope.trusted_source is TrustedMedicationSource.DIRECT_USER_TEXT
    assert envelope.catalog_digest == DIGEST
    with pytest.raises(AttributeError, match="^envelope_frozen$"):
        envelope.medication_keys = ()  # type: ignore[misc]
    assert repr(envelope) == "MedicationIntakeTurnEnvelope(<opaque>)"


def test_candidate_context_is_immutable_and_carries_no_selected_authority() -> None:
    projection_ref = "med-ref-" + ("a1" * 32)
    source_instant = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
    envelope = build_medication_intake_turn_envelope(
        turn_id=TURN_ID,
        candidate_ref_to_key={projection_ref: KEY},
        trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
        scope_digest=SCOPE_DIGEST,
        authority_digest=AUTHORITY_DIGEST,
        catalog_digest=DIGEST,
        source_instant=source_instant,
    )

    assert envelope.source_instant is source_instant
    assert envelope.candidate_ref_to_key == {projection_ref: KEY}
    assert isinstance(envelope.candidate_ref_to_key, MappingProxyType)
    assert not hasattr(envelope, "selected_target")
    assert not hasattr(envelope, "clinical_occurrence")
    assert not hasattr(envelope, "write_grant")
    with pytest.raises(TypeError):
        envelope.candidate_ref_to_key[projection_ref] = KEY  # type: ignore[index]


def test_envelope_is_explicitly_nonserializable() -> None:
    envelope = _envelope()
    with pytest.raises(MedicationTurnError, match="^serialization_denied$"):
        pickle.dumps(envelope)
    with pytest.raises(TypeError):
        json.dumps(envelope)
    with pytest.raises(MedicationTurnError, match="^serialization_denied$"):
        envelope.to_dict()
    with pytest.raises(TypeError):
        dataclasses.asdict(envelope)


def test_builder_accepts_only_opaque_keys_and_direct_trusted_source() -> None:
    marker = "합성알파"
    with pytest.raises(MedicationTurnError, match="^medication_keys_invalid$") as exc:
        build_medication_intake_turn_envelope(
            (marker,),
            turn_id=TURN_ID,
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            occurred_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
    _assert_private_absent(marker, exc.value, repr(exc.value))

    with pytest.raises(MedicationTurnError, match="^trusted_source_invalid$"):
        build_medication_intake_turn_envelope(
            (KEY,),
            turn_id=TURN_ID,
            trusted_source="retrieved_text",  # type: ignore[arg-type]
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            occurred_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )


def test_hostile_timezone_error_is_constant_and_raw_free() -> None:
    marker = "PRIVATE-TZ"

    class HostileTimezone(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta | None:
            raise RuntimeError(marker)

        def dst(self, _dt: datetime | None) -> timedelta | None:
            return None

    with pytest.raises(MedicationTurnError, match="^timestamp_invalid$") as exc:
        build_medication_intake_turn_envelope(
            (KEY,),
            turn_id=TURN_ID,
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            occurred_at=datetime(2026, 7, 27, tzinfo=HostileTimezone()),
        )
    _assert_private_absent(marker, exc.value, repr(exc.value))
    assert exc.value.__cause__ is None


def test_hostile_candidate_mapping_error_is_constant_and_raw_free() -> None:
    marker = "PRIVATE-CANDIDATE-MAPPING"

    class HostileMapping(Mapping[str, str]):
        def __len__(self) -> int:
            return 1

        def __iter__(self):
            raise RuntimeError(marker)

        def __getitem__(self, _key: str) -> str:
            raise RuntimeError(marker)

    with pytest.raises(MedicationTurnError, match="^candidate_mapping_invalid$") as exc:
        build_medication_intake_turn_envelope(
            turn_id=TURN_ID,
            candidate_ref_to_key=MappingProxyType(HostileMapping()),
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            source_instant=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
    _assert_private_absent(marker, exc.value, repr(exc.value))


def test_registry_register_read_close_and_expiry() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    envelope = _envelope()
    registry.register(envelope, now=10.0)
    assert registry.active_count == 1
    readback = registry.read(**_registry_kwargs(10.5))  # type: ignore[arg-type]
    assert readback is not envelope
    assert readback.medication_keys == envelope.medication_keys
    closed = registry.close(**_registry_kwargs(10.6))  # type: ignore[arg-type]
    assert closed.medication_keys == envelope.medication_keys
    assert registry.active_count == 0

    registry.register(envelope, now=20.0)
    assert registry.cleanup_expired(now=21.1) == 1
    assert registry.active_count == 0


def test_registry_denies_duplicate_foreign_late_and_time_regression() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    envelope = _envelope()
    registry.register(envelope, now=1.0)
    with pytest.raises(MedicationTurnError, match="^duplicate_turn$"):
        registry.register(envelope, now=1.1)

    foreign = _registry_kwargs(1.2)
    foreign["authority_digest"] = "abcdef0123456789" * 4
    with pytest.raises(MedicationTurnError, match="^foreign_turn$"):
        registry.read(**foreign)  # type: ignore[arg-type]
    with pytest.raises(MedicationTurnError, match="^monotonic_time_invalid$"):
        registry.read(**_registry_kwargs(0.9))  # type: ignore[arg-type]
    with pytest.raises(MedicationTurnError, match="^late_turn$"):
        registry.close(**_registry_kwargs(2.01))  # type: ignore[arg-type]
    assert registry.active_count == 0


def test_registry_snapshots_input_and_allows_only_one_concurrent_registration() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=10.0)
    envelope = _envelope()
    barrier = Barrier(3)
    outcomes: list[str] = []

    def register() -> None:
        barrier.wait()
        try:
            registry.register(envelope, now=1.0)
        except MedicationTurnError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("registered")

    threads = [Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["duplicate_turn", "registered"]
    object.__setattr__(envelope, "authority_digest", "abcdef0123456789" * 4)
    readback = registry.read(**_registry_kwargs(1.1))  # type: ignore[arg-type]
    assert readback.authority_digest == AUTHORITY_DIGEST


def test_leaf_module_has_no_runtime_layer_imports() -> None:
    path = Path(__file__).parents[2] / "agent" / "medication_intake_turn.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("gateway", "plugins", "tools", "state", "agent.")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [name for name in imported if name.startswith(forbidden)]
