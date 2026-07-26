"""Privacy and lifecycle contracts for a raw-free active-turn envelope."""

from __future__ import annotations

import dataclasses
import json
import logging
import pickle
from datetime import datetime, timedelta, timezone, tzinfo
from threading import Barrier, Thread
from typing import TypedDict

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


def _assert_private_absent(marker: str, *surfaces: object) -> None:
    haystack = "\n".join(str(surface) for surface in surfaces)
    if marker in haystack:
        raise AssertionError("privacy marker detected")


class _RegistryKwargs(TypedDict):
    turn_id: str
    trusted_source: TrustedMedicationSource
    scope_digest: str
    authority_digest: str
    catalog_digest: str
    now: float


def _registry_kwargs(now: float) -> _RegistryKwargs:
    return {
        "turn_id": TURN_ID,
        "trusted_source": TrustedMedicationSource.DIRECT_USER_TEXT,
        "scope_digest": SCOPE_DIGEST,
        "authority_digest": AUTHORITY_DIGEST,
        "catalog_digest": DIGEST,
        "now": now,
    }


def test_envelope_is_frozen_sorted_raw_free_and_constant_safe(caplog: pytest.LogCaptureFixture) -> None:
    marker = "합성알파"
    with caplog.at_level(logging.DEBUG):
        envelope = _envelope()
    _assert_private_absent(marker, envelope, repr(envelope), caplog.text, envelope.__slots__)
    assert envelope.medication_keys == (KEY,)
    assert envelope.trusted_source is TrustedMedicationSource.DIRECT_USER_TEXT
    assert envelope.catalog_digest == DIGEST
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        envelope.medication_keys = ()  # type: ignore[misc]
    assert repr(envelope) == "MedicationIntakeTurnEnvelope(<opaque>)"


def test_builder_accepts_only_direct_text_and_explicit_trusted_scope() -> None:
    envelope = _envelope()
    assert not hasattr(envelope, "raw_text")
    assert not hasattr(envelope, "assistant_text")
    assert not hasattr(envelope, "retrieved_text")
    assert not hasattr(envelope, "reply_metadata")
    with pytest.raises(TypeError):
        build_medication_intake_turn_envelope(  # type: ignore[call-arg]
            (KEY,),
            turn_id=TURN_ID,
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            occurred_at=datetime.now(timezone.utc),
            assistant_text="합성알파",
        )


def test_envelope_is_explicitly_nonserializable_and_errors_are_raw_free() -> None:
    marker = "합성알파"
    envelope = _envelope()
    for operation in (
        lambda: pickle.dumps(envelope),
        lambda: json.dumps(envelope),
        envelope.to_dict,
    ):
        with pytest.raises((MedicationTurnError, TypeError)) as exc_info:
            operation()
        _assert_private_absent(marker, exc_info.value, repr(exc_info.value))


def test_envelope_denies_dataclass_extractors() -> None:
    envelope = _envelope()
    for operation in (
        lambda: dataclasses.asdict(envelope),
        lambda: dataclasses.astuple(envelope),
    ):
        with pytest.raises(TypeError):
            operation()


def test_hostile_timezone_callback_is_timestamp_invalid_and_raw_free() -> None:
    marker = "PRIVATE-TZ"

    class HostileTimezone(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta | None:
            raise RuntimeError(marker)

        def dst(self, _dt: datetime | None) -> timedelta | None:
            return None

    with pytest.raises(MedicationTurnError) as exc_info:
        build_medication_intake_turn_envelope(
            (KEY,),
            turn_id=TURN_ID,
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            occurred_at=datetime(2026, 7, 27, tzinfo=HostileTimezone()),
        )
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "timestamp_invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_nonopaque_key_error_is_constant_safe() -> None:
    marker = "합성알파"
    with pytest.raises(MedicationTurnError) as exc_info:
        build_medication_intake_turn_envelope(
            (marker,),
            turn_id=TURN_ID,
            trusted_source=TrustedMedicationSource.DIRECT_USER_TEXT,
            scope_digest=SCOPE_DIGEST,
            authority_digest=AUTHORITY_DIGEST,
            catalog_digest=DIGEST,
            occurred_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "medication_keys_invalid"


def test_registry_exact_register_read_close_and_cleanup() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    envelope = _envelope()
    registry.register(envelope, now=10.0)
    assert registry.active_count == 1
    readback = registry.read(**_registry_kwargs(10.5))
    assert readback is not envelope
    assert readback.medication_keys == envelope.medication_keys
    assert readback.authority_digest == envelope.authority_digest
    closed = registry.close(**_registry_kwargs(10.6))
    assert closed is not envelope
    assert closed.medication_keys == envelope.medication_keys
    assert registry.active_count == 0
    with pytest.raises(MedicationTurnError, match="^inactive_turn$"):
        registry.read(**_registry_kwargs(10.7))

    registry.register(envelope, now=20.0)
    assert registry.cleanup_expired(now=21.1) == 1
    assert registry.active_count == 0


def test_registry_denies_duplicate_foreign_and_late_access() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    envelope = _envelope()
    registry.register(envelope, now=1.0)
    with pytest.raises(MedicationTurnError, match="^duplicate_turn$"):
        registry.register(envelope, now=1.1)

    foreign = _registry_kwargs(1.2)
    foreign["authority_digest"] = "abcdef0123456789" * 4
    with pytest.raises(MedicationTurnError, match="^foreign_turn$"):
        registry.read(**foreign)
    assert registry.active_count == 1

    with pytest.raises(MedicationTurnError, match="^late_turn$"):
        registry.close(**_registry_kwargs(2.01))
    assert registry.active_count == 0


def test_hostile_scope_comparison_is_foreign_and_raw_free() -> None:
    marker = "PRIVATE-EQUALITY"

    class HostileDigest(str):
        def __ne__(self, other: object) -> bool:
            raise RuntimeError(f"{marker}:{other}")

    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    registry.register(_envelope(), now=1.0)
    hostile = _registry_kwargs(1.1)
    hostile["scope_digest"] = HostileDigest(SCOPE_DIGEST)
    with pytest.raises(MedicationTurnError) as exc_info:
        registry.read(**hostile)  # type: ignore[arg-type]
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value))
    _assert_private_absent(SCOPE_DIGEST, exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "foreign_turn"


def test_registry_snapshots_envelope_and_denies_retained_object_rebinding() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=1.0)
    envelope = _envelope()
    registry.register(envelope, now=1.0)
    foreign_digest = "abcdef0123456789" * 4
    object.__setattr__(envelope, "authority_digest", foreign_digest)

    original = registry.read(**_registry_kwargs(1.1))  # type: ignore[arg-type]
    assert original.authority_digest == AUTHORITY_DIGEST
    foreign = _registry_kwargs(1.1)
    foreign["authority_digest"] = foreign_digest
    with pytest.raises(MedicationTurnError, match="^foreign_turn$"):
        registry.read(**foreign)  # type: ignore[arg-type]


def test_registry_rejects_monotonic_time_regression() -> None:
    registry = ActiveMedicationIntakeTurnRegistry(max_active_seconds=10.0)
    registry.register(_envelope(), now=10.0)
    with pytest.raises(MedicationTurnError, match="^monotonic_time_invalid$"):
        registry.read(**_registry_kwargs(9.0))  # type: ignore[arg-type]


def test_registry_lock_allows_only_one_concurrent_registration() -> None:
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
    assert registry.active_count == 1


def test_leaf_module_has_no_runtime_layer_imports() -> None:
    import ast
    from pathlib import Path

    path = Path(__file__).parents[2] / "agent" / "medication_intake_turn.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("gateway", "plugins", "tools", "state", "agent.")
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [name for name in imported if name.startswith(forbidden)]
