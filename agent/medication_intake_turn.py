"""Raw-free active-turn envelope and exact lock-guarded lifecycle registry.

This is a pure-stdlib leaf.  It accepts already-classified opaque medication
keys and never imports gateway, plugin, tool, state, or other agent modules.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import cast

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_REF = re.compile(r"^(?:med-ref|legacy-ref)-[0-9a-f]{64}$")


def _raise_if_control_flow(exc: BaseException) -> None:
    cancellation_names = {"Cancelled", "CancelledError", "CancellationError"}
    if isinstance(
        exc,
        (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
            MemoryError,
            asyncio.CancelledError,
            concurrent.futures.CancelledError,
        ),
    ) or any(cls.__name__ in cancellation_names for cls in type(exc).__mro__):
        raise exc


class MedicationTurnError(RuntimeError):
    """Constant-code error with no private input or envelope rendering."""


class TrustedMedicationSource(StrEnum):
    DIRECT_USER_TEXT = "direct_user_text"


class MedicationIntakeTurnEnvelope:
    __slots__ = (
        "turn_id",
        "candidate_ref_to_key",
        "trusted_source",
        "scope_digest",
        "authority_digest",
        "catalog_digest",
        "source_instant",
    )

    def __init__(
        self,
        *,
        turn_id: str,
        candidate_ref_to_key: Mapping[str, str],
        trusted_source: TrustedMedicationSource,
        scope_digest: str,
        authority_digest: str,
        catalog_digest: str,
        source_instant: datetime,
    ) -> None:
        candidate_mapping = _snapshot_candidate_mapping(candidate_ref_to_key)
        _validate_envelope_fields(
            turn_id,
            candidate_mapping,
            trusted_source,
            scope_digest,
            authority_digest,
            catalog_digest,
            source_instant,
        )
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(
            self,
            "candidate_ref_to_key",
            MappingProxyType(candidate_mapping),
        )
        object.__setattr__(self, "trusted_source", trusted_source)
        object.__setattr__(self, "scope_digest", scope_digest)
        object.__setattr__(self, "authority_digest", authority_digest)
        object.__setattr__(self, "catalog_digest", catalog_digest)
        object.__setattr__(self, "source_instant", source_instant)

    @property
    def medication_keys(self) -> tuple[str, ...]:
        """Compatibility view for pre-Task-1C consumers; not a selected target."""

        return tuple(sorted(set(self.candidate_ref_to_key.values())))

    @property
    def occurred_at(self) -> datetime:
        """Temporary pre-Task-1C compatibility alias for the source instant."""

        return self.source_instant

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("envelope_frozen")

    def __repr__(self) -> str:
        return "MedicationIntakeTurnEnvelope(<opaque>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol: int) -> object:
        raise MedicationTurnError("serialization_denied")

    def to_dict(self) -> dict[str, object]:
        raise MedicationTurnError("serialization_denied")


@dataclass(frozen=True, slots=True)
class _ActiveTurn:
    envelope: MedicationIntakeTurnEnvelope
    registered_at: float



def _valid_digest(value: object) -> bool:
    return type(value) is str and _HEX_64.fullmatch(value) is not None


def _snapshot_candidate_mapping(value: object) -> dict[str, str]:
    """Return one exact built-in snapshot or a constant raw-free denial."""

    failed = False
    items: tuple[tuple[object, object], ...] = ()
    if type(value) not in (dict, MappingProxyType):
        failed = True
    else:
        try:
            mapping = cast(Mapping[object, object], value)
            if not mapping:
                failed = True
            else:
                items = tuple(mapping.items())
        except BaseException as exc:
            _raise_if_control_flow(exc)
            failed = True
    if failed:
        raise MedicationTurnError("candidate_mapping_invalid")
    snapshot: dict[str, str] = {}
    for ref, key in items:
        if (
            type(ref) is not str
            or _PROJECTION_REF.fullmatch(ref) is None
            or not _valid_digest(key)
        ):
            raise MedicationTurnError("candidate_mapping_invalid")
        snapshot[cast(str, ref)] = cast(str, key)
    return snapshot



def _validate_envelope_fields(
    turn_id: object,
    candidate_ref_to_key: object,
    trusted_source: object,
    scope_digest: object,
    authority_digest: object,
    catalog_digest: object,
    source_instant: object,
) -> None:
    if not _valid_digest(turn_id):
        raise MedicationTurnError("turn_id_invalid")
    if type(candidate_ref_to_key) is not dict or not candidate_ref_to_key:
        raise MedicationTurnError("candidate_mapping_invalid")
    if any(
        type(ref) is not str
        or _PROJECTION_REF.fullmatch(ref) is None
        or not _valid_digest(key)
        for ref, key in candidate_ref_to_key.items()
    ):
        raise MedicationTurnError("candidate_mapping_invalid")
    if type(trusted_source) is not TrustedMedicationSource:
        raise MedicationTurnError("trusted_source_invalid")
    if not _valid_digest(scope_digest):
        raise MedicationTurnError("scope_digest_invalid")
    if not _valid_digest(authority_digest):
        raise MedicationTurnError("authority_digest_invalid")
    if not _valid_digest(catalog_digest):
        raise MedicationTurnError("catalog_digest_invalid")
    if type(source_instant) is not datetime or source_instant.tzinfo is None:
        raise MedicationTurnError("timestamp_invalid")
    timestamp_error = False
    try:
        is_utc = source_instant.utcoffset() == timezone.utc.utcoffset(source_instant)
    except BaseException as exc:
        _raise_if_control_flow(exc)
        timestamp_error = True
        is_utc = False
    if timestamp_error:
        raise MedicationTurnError("timestamp_invalid") from None
    if not is_utc:
        raise MedicationTurnError("timestamp_invalid")



def build_medication_intake_turn_envelope(
    medication_keys: tuple[str, ...] | None = None,
    *,
    turn_id: str,
    candidate_ref_to_key: Mapping[str, str] | None = None,
    trusted_source: TrustedMedicationSource,
    scope_digest: str,
    authority_digest: str,
    catalog_digest: str,
    occurred_at: datetime | None = None,
    source_instant: datetime | None = None,
) -> MedicationIntakeTurnEnvelope:
    """Build candidate context while retaining the old call shape temporarily."""

    if candidate_ref_to_key is None:
        if type(medication_keys) is not tuple or not medication_keys or any(
            not _valid_digest(key) for key in medication_keys
        ):
            raise MedicationTurnError("medication_keys_invalid") from None
        sorted_keys = tuple(sorted(set(medication_keys)))
        candidate_ref_to_key = {
            "legacy-ref-" + hashlib.sha256(key.encode("ascii")).hexdigest(): key
            for key in sorted_keys
        }
    elif medication_keys is not None:
        raise MedicationTurnError("candidate_mapping_invalid") from None
    instant = source_instant if source_instant is not None else occurred_at
    if source_instant is not None and occurred_at is not None:
        raise MedicationTurnError("timestamp_invalid") from None
    return MedicationIntakeTurnEnvelope(
        turn_id=turn_id,
        candidate_ref_to_key=candidate_ref_to_key,
        trusted_source=trusted_source,
        scope_digest=scope_digest,
        authority_digest=authority_digest,
        catalog_digest=catalog_digest,
        source_instant=instant,  # type: ignore[arg-type]
    )


class ActiveMedicationIntakeTurnRegistry:
    """In-memory exact active-turn registry with lock-guarded expiry."""

    def __init__(self, *, max_active_seconds: float) -> None:
        if (
            type(max_active_seconds) is not float
            or not math.isfinite(max_active_seconds)
            or max_active_seconds <= 0.0
        ):
            raise MedicationTurnError("active_window_invalid")
        self._max_active_seconds = max_active_seconds
        self._active: dict[str, _ActiveTurn] = {}
        self._lock = RLock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def register(
        self,
        envelope: MedicationIntakeTurnEnvelope,
        *,
        now: float,
    ) -> None:
        if type(envelope) is not MedicationIntakeTurnEnvelope:
            raise MedicationTurnError("envelope_invalid")
        current = _validate_now(now)
        with self._lock:
            if envelope.turn_id in self._active:
                raise MedicationTurnError("duplicate_turn")
            self._active[envelope.turn_id] = _ActiveTurn(
                _copy_envelope(envelope),
                current,
            )

    def read(
        self,
        *,
        turn_id: str,
        trusted_source: TrustedMedicationSource,
        scope_digest: str,
        authority_digest: str,
        catalog_digest: str,
        now: float,
    ) -> MedicationIntakeTurnEnvelope:
        return self._access(
            close=False,
            turn_id=turn_id,
            trusted_source=trusted_source,
            scope_digest=scope_digest,
            authority_digest=authority_digest,
            catalog_digest=catalog_digest,
            now=now,
        )

    def close(
        self,
        *,
        turn_id: str,
        trusted_source: TrustedMedicationSource,
        scope_digest: str,
        authority_digest: str,
        catalog_digest: str,
        now: float,
    ) -> MedicationIntakeTurnEnvelope:
        return self._access(
            close=True,
            turn_id=turn_id,
            trusted_source=trusted_source,
            scope_digest=scope_digest,
            authority_digest=authority_digest,
            catalog_digest=catalog_digest,
            now=now,
        )

    def _access(
        self,
        *,
        close: bool,
        turn_id: str,
        trusted_source: TrustedMedicationSource,
        scope_digest: str,
        authority_digest: str,
        catalog_digest: str,
        now: float,
    ) -> MedicationIntakeTurnEnvelope:
        current = _validate_now(now)
        if not _valid_digest(turn_id):
            raise MedicationTurnError("inactive_turn")
        with self._lock:
            active = self._active.get(turn_id)
            if active is None:
                raise MedicationTurnError("inactive_turn")
            if current < active.registered_at:
                raise MedicationTurnError("monotonic_time_invalid")
            if current - active.registered_at > self._max_active_seconds:
                del self._active[turn_id]
                raise MedicationTurnError("late_turn")
            envelope = active.envelope
            if (
                type(trusted_source) is not TrustedMedicationSource
                or not _valid_digest(scope_digest)
                or not _valid_digest(authority_digest)
                or not _valid_digest(catalog_digest)
            ):
                raise MedicationTurnError("foreign_turn")
            if (
                trusted_source is not envelope.trusted_source
                or scope_digest != envelope.scope_digest
                or authority_digest != envelope.authority_digest
                or catalog_digest != envelope.catalog_digest
            ):
                raise MedicationTurnError("foreign_turn")
            if close:
                del self._active[turn_id]
            return _copy_envelope(envelope)

    def cleanup_expired(self, *, now: float) -> int:
        current = _validate_now(now)
        with self._lock:
            if any(current < active.registered_at for active in self._active.values()):
                raise MedicationTurnError("monotonic_time_invalid")
            expired = [
                turn_id
                for turn_id, active in self._active.items()
                if current - active.registered_at > self._max_active_seconds
            ]
            for turn_id in expired:
                del self._active[turn_id]
            return len(expired)


def _copy_envelope(
    envelope: MedicationIntakeTurnEnvelope,
) -> MedicationIntakeTurnEnvelope:
    return MedicationIntakeTurnEnvelope(
        turn_id=envelope.turn_id,
        candidate_ref_to_key=envelope.candidate_ref_to_key,
        trusted_source=envelope.trusted_source,
        scope_digest=envelope.scope_digest,
        authority_digest=envelope.authority_digest,
        catalog_digest=envelope.catalog_digest,
        source_instant=envelope.source_instant,
    )



def _validate_now(now: object) -> float:
    if type(now) is not float or not math.isfinite(now) or now < 0.0:
        raise MedicationTurnError("monotonic_time_invalid")
    return now
