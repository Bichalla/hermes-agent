"""Raw-free active-turn envelope and exact lock-guarded lifecycle registry.

This is a pure-stdlib leaf.  It accepts already-classified opaque medication
keys and never imports gateway, plugin, tool, state, or other agent modules.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class MedicationTurnError(RuntimeError):
    """Constant-code error with no private input or envelope rendering."""


class TrustedMedicationSource(StrEnum):
    DIRECT_USER_TEXT = "direct_user_text"


class MedicationIntakeTurnEnvelope:
    __slots__ = (
        "turn_id",
        "medication_keys",
        "trusted_source",
        "scope_digest",
        "authority_digest",
        "catalog_digest",
        "occurred_at",
    )

    def __init__(
        self,
        *,
        turn_id: str,
        medication_keys: tuple[str, ...],
        trusted_source: TrustedMedicationSource,
        scope_digest: str,
        authority_digest: str,
        catalog_digest: str,
        occurred_at: datetime,
    ) -> None:
        _validate_envelope_fields(
            turn_id,
            medication_keys,
            trusted_source,
            scope_digest,
            authority_digest,
            catalog_digest,
            occurred_at,
        )
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "medication_keys", medication_keys)
        object.__setattr__(self, "trusted_source", trusted_source)
        object.__setattr__(self, "scope_digest", scope_digest)
        object.__setattr__(self, "authority_digest", authority_digest)
        object.__setattr__(self, "catalog_digest", catalog_digest)
        object.__setattr__(self, "occurred_at", occurred_at)

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



def _validate_envelope_fields(
    turn_id: object,
    medication_keys: object,
    trusted_source: object,
    scope_digest: object,
    authority_digest: object,
    catalog_digest: object,
    occurred_at: object,
) -> None:
    if not _valid_digest(turn_id):
        raise MedicationTurnError("turn_id_invalid")
    if type(medication_keys) is not tuple or not medication_keys:
        raise MedicationTurnError("medication_keys_invalid")
    if any(not _valid_digest(key) for key in medication_keys):
        raise MedicationTurnError("medication_keys_invalid")
    if medication_keys != tuple(sorted(set(medication_keys))):
        raise MedicationTurnError("medication_keys_invalid")
    if type(trusted_source) is not TrustedMedicationSource:
        raise MedicationTurnError("trusted_source_invalid")
    if not _valid_digest(scope_digest):
        raise MedicationTurnError("scope_digest_invalid")
    if not _valid_digest(authority_digest):
        raise MedicationTurnError("authority_digest_invalid")
    if not _valid_digest(catalog_digest):
        raise MedicationTurnError("catalog_digest_invalid")
    if type(occurred_at) is not datetime or occurred_at.tzinfo is None:
        raise MedicationTurnError("timestamp_invalid")
    timestamp_error = False
    try:
        is_utc = occurred_at.utcoffset() == timezone.utc.utcoffset(occurred_at)
    except Exception:
        timestamp_error = True
        is_utc = False
    if timestamp_error:
        raise MedicationTurnError("timestamp_invalid") from None
    if not is_utc:
        raise MedicationTurnError("timestamp_invalid")



def build_medication_intake_turn_envelope(
    medication_keys: tuple[str, ...],
    *,
    turn_id: str,
    trusted_source: TrustedMedicationSource,
    scope_digest: str,
    authority_digest: str,
    catalog_digest: str,
    occurred_at: datetime,
) -> MedicationIntakeTurnEnvelope:
    """Build an envelope from opaque classifier output and trusted metadata."""

    if type(medication_keys) is not tuple or any(
        not _valid_digest(key) for key in medication_keys
    ):
        raise MedicationTurnError("medication_keys_invalid") from None
    sorted_keys = tuple(sorted(set(medication_keys)))
    return MedicationIntakeTurnEnvelope(
        turn_id=turn_id,
        medication_keys=sorted_keys,
        trusted_source=trusted_source,
        scope_digest=scope_digest,
        authority_digest=authority_digest,
        catalog_digest=catalog_digest,
        occurred_at=occurred_at,
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
        medication_keys=envelope.medication_keys,
        trusted_source=envelope.trusted_source,
        scope_digest=envelope.scope_digest,
        authority_digest=envelope.authority_digest,
        catalog_digest=envelope.catalog_digest,
        occurred_at=envelope.occurred_at,
    )



def _validate_now(now: object) -> float:
    if type(now) is not float or not math.isfinite(now) or now < 0.0:
        raise MedicationTurnError("monotonic_time_invalid")
    return now
