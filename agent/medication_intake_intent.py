"""Pure, raw-free medication-intake intent classification.

The classifier consumes only caller-supplied direct user text and an immutable
private catalog.  It performs no I/O, logging, fuzzy matching, or state access.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import math
import re
import unicodedata
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from agent.medication_name_contract import (
    MAX_MEDICATION_NAME_CHARS,
    normalize_medication_name,
)

MAX_INPUT_CHARS = 4096
MAX_CATALOG_ALIASES = 256
MAX_ALIAS_CHARS = MAX_MEDICATION_NAME_CHARS
CLASSIFICATION_DEADLINE_SECONDS = 0.020
MAX_PROJECTION_SPANS = 256
MAX_PROJECTED_CHARS = 8192

_PROJECTION_DOMAIN = b"lifelog-medication-projection-ref/v1\0"
_PROJECTION_REF_PREFIX = "med-ref-"
_PRIVATE_PROJECTION_DENIAL = "[private medication context unavailable]"

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_PUNCTUATION = frozenset(".!！。…")
_CONFIRMED_SUFFIXES = (
    "복용했습니다",
    "복용했어요",
    "복용했어",
    "복용함",
    "먹었습니다",
    "먹었어요",
    "먹었어",
    "먹엇어",
    "먹음",
)
_PERMISSION_MARKERS = ("먹어도 돼", "먹어도 될까", "복용해도 돼", "복용해도 될까")
_FUTURE_MARKERS = ("먹을게", "먹겠다", "먹을 거야", "복용할 예정", "복용하겠다")
_NEGATION_MARKERS = ("안 먹었", "못 먹었", "복용하지 않았", "복용 안 했", "먹지 않았")
_OTHER_PERSON_MARKERS = (
    "친구가 ",
    "엄마가 ",
    "아빠가 ",
    "아이가 ",
    "남편이 ",
    "아내가 ",
    "그가 ",
    "그녀가 ",
)
_QUOTE_MARKERS = (">", "[reply]", "[quote]", "인용:", "답장:", "reply history:")
_ALIAS_FOLLOWERS = (
    "이랑",
    "랑",
    "하고",
    "과",
    "와",
    "을",
    "를",
    "도",
    "은",
    "는",
)
_CATALOG_ERROR_CODES = frozenset(
    {
        "catalog_aliases_invalid",
        "catalog_alias_limit",
        "catalog_alias_duplicate",
        "catalog_keys_invalid",
        "catalog_retired_invalid",
        "catalog_nonce_invalid",
        "catalog_digest_invalid",
        "catalog_alias_invalid",
    }
)
_NON_PERSON_TOPIC_TOKENS = frozenset(
    {"오늘은", "어제는", "아침에는", "점심에는", "저녁에는", "이번에는", "지금은", "방금은"}
)


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


class ClassificationReason(StrEnum):
    SUPPORTED = "supported"
    NO_CANDIDATE = "no_candidate"
    NEGATED = "negated"
    FUTURE = "future"
    QUESTION = "question"
    PERMISSION_QUESTION = "permission_question"
    QUOTE_OR_REPLY = "quote_or_reply"
    MIXED_POLARITY = "mixed_polarity"
    ANOTHER_PERSON = "another_person"
    UNKNOWN_ALIAS = "unknown_alias"
    AMBIGUOUS_ALIAS = "ambiguous_alias"
    INPUT_TOO_LONG = "input_too_long"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class MedicationCatalog:
    """Immutable alias index containing only normalized aliases and opaque keys."""

    __slots__ = (
        "alias_to_keys",
        "retired_keys",
        "catalog_nonce",
        "catalog_digest",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        alias_to_keys: Mapping[str, frozenset[str]],
        retired_keys: frozenset[str],
        catalog_nonce: str,
        catalog_digest: str,
    ) -> None:
        normalized: dict[str, frozenset[str]] = {}
        if type(alias_to_keys) not in (dict, MappingProxyType):
            raise ValueError("catalog_aliases_invalid")

        snapshot_failed = False
        alias_count = 0
        try:
            alias_count = len(alias_to_keys)
        except BaseException as exc:
            _raise_if_control_flow(exc)
            snapshot_failed = True
        if snapshot_failed:
            raise ValueError("catalog_invalid")
        if alias_count > MAX_CATALOG_ALIASES:
            raise ValueError("catalog_alias_limit")

        alias_items: tuple[object, ...] = ()
        try:
            alias_items = tuple(alias_to_keys.items())
        except BaseException as exc:
            _raise_if_control_flow(exc)
            snapshot_failed = True
        if snapshot_failed:
            raise ValueError("catalog_invalid")
        if len(alias_items) != alias_count:
            raise ValueError("catalog_invalid")

        error_code: str | None = None
        try:
            for item in alias_items:
                if type(item) is not tuple or len(item) != 2:
                    raise ValueError("catalog_invalid")
                raw_alias, raw_keys = item
                alias_failed = False
                try:
                    alias = normalize_medication_name(raw_alias)
                except BaseException as exc:
                    _raise_if_control_flow(exc)
                    alias_failed = True
                    alias = ""
                if alias_failed:
                    raise ValueError("catalog_alias_invalid")
                if alias in normalized:
                    raise ValueError("catalog_alias_duplicate")
                if type(raw_keys) is not frozenset or not raw_keys:
                    raise ValueError("catalog_keys_invalid")
                if any(
                    type(key) is not str or not _HEX_64.fullmatch(key)
                    for key in raw_keys
                ):
                    raise ValueError("catalog_keys_invalid")
                normalized[alias] = frozenset(raw_keys)
            if type(retired_keys) is not frozenset or any(
                type(key) is not str or not _HEX_64.fullmatch(key)
                for key in retired_keys
            ):
                raise ValueError("catalog_retired_invalid")
            if type(catalog_nonce) is not str or not _HEX_64.fullmatch(catalog_nonce):
                raise ValueError("catalog_nonce_invalid")
            if type(catalog_digest) is not str or not _HEX_64.fullmatch(catalog_digest):
                raise ValueError("catalog_digest_invalid")
        except ValueError as exc:
            candidate = (
                exc.args[0]
                if type(exc) is ValueError
                and len(exc.args) == 1
                and type(exc.args[0]) is str
                else None
            )
            error_code = (
                candidate if candidate in _CATALOG_ERROR_CODES else "catalog_invalid"
            )
        if error_code is not None:
            raise ValueError(error_code) from None
        object.__setattr__(self, "alias_to_keys", MappingProxyType(normalized))
        object.__setattr__(self, "retired_keys", frozenset(retired_keys))
        object.__setattr__(self, "catalog_nonce", catalog_nonce)
        object.__setattr__(self, "catalog_digest", catalog_digest)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("catalog_frozen")

    def __repr__(self) -> str:
        return "MedicationCatalog(<private>)"

    def __reduce_ex__(self, _protocol: int) -> object:
        raise ValueError("serialization_denied")

    def to_dict(self) -> dict[str, object]:
        raise ValueError("serialization_denied")


def _make_catalog_validation_gate():
    seals: dict[int, tuple[weakref.ReferenceType[MedicationCatalog], str]] = {}

    def binding_digest(catalog: MedicationCatalog) -> str:
        canonical = {
            "aliases": [
                [alias, sorted(keys)]
                for alias, keys in sorted(catalog.alias_to_keys.items())
            ],
            "retired_keys": sorted(catalog.retired_keys),
            "catalog_nonce": catalog.catalog_nonce,
            "catalog_digest": catalog.catalog_digest,
        }
        payload = json.dumps(
            canonical,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(
            b"lifelog-medication-validated-catalog-binding/v1\0" + payload
        ).hexdigest()

    def seal(catalog: MedicationCatalog, expected_catalog_digest: str) -> MedicationCatalog:
        if (
            type(catalog) is not MedicationCatalog
            or type(expected_catalog_digest) is not str
            or catalog.catalog_digest != expected_catalog_digest
        ):
            raise ValueError("catalog_invalid") from None
        digest: str | None = None
        try:
            digest = binding_digest(catalog)
        except BaseException as exc:
            _raise_if_control_flow(exc)
        if digest is None:
            raise ValueError("catalog_invalid") from None
        identity = id(catalog)
        seals[identity] = (
            weakref.ref(catalog, lambda _ref: seals.pop(identity, None)),
            digest,
        )
        return catalog

    def is_validated(catalog: object) -> bool:
        if type(catalog) is not MedicationCatalog:
            return False
        entry = seals.get(id(catalog))
        if entry is None or entry[0]() is not catalog:
            return False
        try:
            return hmac.compare_digest(entry[1], binding_digest(catalog))
        except BaseException as exc:
            _raise_if_control_flow(exc)
            return False

    return seal, is_validated


(
    _seal_validated_medication_catalog,
    _is_validated_medication_catalog,
) = _make_catalog_validation_gate()


@dataclass(frozen=True, slots=True, repr=False)
class MedicationIntakeClassification:
    reason: ClassificationReason
    medication_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.reason) is not ClassificationReason:
            raise TypeError("classification_reason_invalid")
        if type(self.medication_keys) is not tuple or any(
            type(key) is not str or not _HEX_64.fullmatch(key)
            for key in self.medication_keys
        ):
            raise ValueError("classification_keys_invalid")
        if self.medication_keys != tuple(sorted(set(self.medication_keys))):
            raise ValueError("classification_keys_invalid")
        if self.reason is not ClassificationReason.SUPPORTED and self.medication_keys:
            raise ValueError("classification_keys_invalid")

    def __repr__(self) -> str:
        return "MedicationIntakeClassification(<opaque>)"


class MedicationCatalogProjection:
    """Raw-free projection plus a process-local candidate-ref binding."""

    __slots__ = ("projected_text", "ref_to_key", "denial_code")

    def __init__(
        self,
        *,
        projected_text: str,
        ref_to_key: Mapping[str, str],
        denial_code: str | None,
    ) -> None:
        object.__setattr__(self, "projected_text", projected_text)
        object.__setattr__(self, "ref_to_key", MappingProxyType(dict(ref_to_key)))
        object.__setattr__(self, "denial_code", denial_code)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("projection_frozen")

    def __repr__(self) -> str:
        return "MedicationCatalogProjection(<opaque>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol: int) -> object:
        raise ValueError("serialization_denied")

    def to_dict(self) -> dict[str, object]:
        raise ValueError("serialization_denied")


def _projection_denial(code: str) -> MedicationCatalogProjection:
    return MedicationCatalogProjection(
        projected_text=_PRIVATE_PROJECTION_DENIAL,
        ref_to_key={},
        denial_code=code,
    )


def _projection_ref(
    *,
    catalog_nonce: str,
    stable_session_identity: str,
    medication_key: str,
) -> str:
    digest = hmac.new(
        bytes.fromhex(catalog_nonce),
        _PROJECTION_DOMAIN
        + stable_session_identity.encode("utf-8")
        + b"\0"
        + bytes.fromhex(medication_key),
        hashlib.sha256,
    ).hexdigest()
    return _PROJECTION_REF_PREFIX + digest


def _marker_positions(text: str, marker: str):
    start = 0
    while True:
        position = text.find(marker, start)
        if position < 0:
            return
        end = position + len(marker)
        if (
            (position == 0 or not text[position - 1].isalnum())
            and is_alias_end_boundary(text, end)
        ):
            yield position, end
        start = position + 1


def project_medication_catalog_text(
    text: str,
    catalog: MedicationCatalog,
    *,
    stable_session_identity: str,
) -> MedicationCatalogProjection:
    """Replace every exact catalog-known span without deciding intake semantics."""

    if type(text) is not str or len(text) > MAX_INPUT_CHARS:
        return _projection_denial("projection_input_overflow")
    if (
        type(stable_session_identity) is not str
        or not stable_session_identity
        or len(stable_session_identity) > 256
        or "\0" in stable_session_identity
    ):
        return _projection_denial("projection_session_invalid")
    try:
        catalog_is_validated = _is_validated_medication_catalog(catalog)
    except BaseException as exc:
        _raise_if_control_flow(exc)
        catalog_is_validated = False
    if not catalog_is_validated:
        return _projection_denial("projection_catalog_invalid")

    try:
        normalized = _normalize_text(text)
    except BaseException as exc:
        _raise_if_control_flow(exc)
        return _projection_denial("projection_invalid")
    try:
        catalog_items = tuple(sorted(catalog.alias_to_keys.items()))
    except BaseException as exc:
        _raise_if_control_flow(exc)
        return _projection_denial("projection_invalid")

    span_keys: dict[tuple[int, int], set[str]] = {}
    span_count = 0
    for marker, keys in catalog_items:
        try:
            marker_spans = tuple(_marker_positions(normalized, marker))
        except BaseException as exc:
            _raise_if_control_flow(exc)
            return _projection_denial("projection_invalid")
        for start, end in marker_spans:
            span_count += 1
            if span_count > MAX_PROJECTION_SPANS:
                return _projection_denial("projection_candidate_overflow")
            span_keys.setdefault((start, end), set()).update(keys)
    if not span_keys:
        return MedicationCatalogProjection(
            projected_text=text,
            ref_to_key={},
            denial_code=None,
        )

    candidates: list[tuple[int, int, str]] = []
    for (start, end), keys in sorted(span_keys.items()):
        if len(keys) != 1:
            return _projection_denial("projection_ambiguous")
        key = next(iter(keys))
        if key in catalog.retired_keys:
            return _projection_denial("projection_retired")
        candidates.append((start, end, key))

    for index, first in enumerate(candidates):
        first_start, first_end, first_key = first
        for second_start, second_end, second_key in candidates[index + 1 :]:
            if first_end <= second_start or second_end <= first_start:
                continue
            if first_key != second_key:
                return _projection_denial("projection_overlap")
            first_contains = first_start <= second_start and first_end >= second_end
            second_contains = second_start <= first_start and second_end >= first_end
            if not first_contains and not second_contains:
                return _projection_denial("projection_overlap")
            if first_end - first_start == second_end - second_start:
                return _projection_denial("projection_ambiguous")

    selected: list[tuple[int, int, str]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-(item[1] - item[0]), item[0], item[1], item[2]),
    ):
        if all(
            candidate[1] <= chosen[0] or chosen[1] <= candidate[0]
            for chosen in selected
        ):
            selected.append(candidate)

    for start, end, key in candidates:
        if not any(
            chosen_key == key and chosen_start <= start and chosen_end >= end
            for chosen_start, chosen_end, chosen_key in selected
        ):
            return _projection_denial("projection_incomplete")

    replacements: list[str] = []
    ref_to_key: dict[str, str] = {}
    cursor = 0
    for start, end, key in sorted(selected):
        try:
            projection_ref = _projection_ref(
                catalog_nonce=catalog.catalog_nonce,
                stable_session_identity=stable_session_identity,
                medication_key=key,
            )
        except BaseException as exc:
            _raise_if_control_flow(exc)
            return _projection_denial("projection_invalid")
        existing_key = ref_to_key.get(projection_ref)
        if existing_key is not None and existing_key != key:
            return _projection_denial("projection_ambiguous")
        ref_to_key[projection_ref] = key
        replacements.extend((normalized[cursor:start], projection_ref))
        cursor = end
    replacements.append(normalized[cursor:])
    projected_text = "".join(replacements)
    if len(projected_text) > MAX_PROJECTED_CHARS:
        return _projection_denial("projection_output_overflow")
    return MedicationCatalogProjection(
        projected_text=projected_text,
        ref_to_key=ref_to_key,
        denial_code=None,
    )



def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())



def _without_terminal_punctuation(text: str) -> str:
    return text.rstrip("".join(_TERMINAL_PUNCTUATION)).rstrip()



def is_confirmed_intake_grammar(text: str) -> bool:
    """Return whether direct text has a supported past-tense intake suffix."""

    if type(text) is not str or len(text) > MAX_INPUT_CHARS:
        return False
    normalized = _without_terminal_punctuation(_normalize_text(text))
    return any(normalized.endswith(suffix) for suffix in _CONFIRMED_SUFFIXES)



def is_alias_end_boundary(text: str, end_position: int) -> bool:
    if end_position == len(text):
        return True
    next_character = text[end_position]
    return not next_character.isalnum() or any(
        text.startswith(follower, end_position) for follower in _ALIAS_FOLLOWERS
    )


def find_alias_position(text: str, alias: str) -> int:
    """Return the first boundary-valid literal alias position, or ``-1``."""

    start = 0
    while True:
        position = text.find(alias, start)
        if position < 0:
            return -1
        before_ok = position == 0 or not text[position - 1].isalnum()
        after_position = position + len(alias)
        after_ok = is_alias_end_boundary(text, after_position)
        if before_ok and after_ok:
            return position
        start = position + len(alias)


def _has_explicit_other_person_subject(text: str, alias_position: int) -> bool:
    prefix = text[:alias_position].rstrip()
    if not prefix:
        return False
    for subject in prefix.split():
        if subject in {"내가", "제가", "나는", "저는", "난"}:
            return False
        if subject in _NON_PERSON_TOPIC_TOKENS:
            continue
        if len(subject) > 1 and subject.endswith(("이", "가", "은", "는")):
            return True
    return False



def classify_medication_intake(
    text: str,
    catalog: MedicationCatalog,
    *,
    monotonic: Callable[[], float] | None = None,
) -> MedicationIntakeClassification:
    """Classify direct foreground text into an immutable opaque result."""

    if monotonic is None:
        start = None
    else:
        try:
            start = monotonic()
        except BaseException as exc:
            _raise_if_control_flow(exc)
            return MedicationIntakeClassification(
                ClassificationReason.DEADLINE_EXCEEDED
            )
        if type(start) is not float or not math.isfinite(start):
            return MedicationIntakeClassification(
                ClassificationReason.DEADLINE_EXCEEDED
            )

    def deadline_exceeded() -> bool:
        if monotonic is None or start is None:
            return False
        try:
            current = monotonic()
        except BaseException as exc:
            _raise_if_control_flow(exc)
            return True
        return (
            type(current) is not float
            or not math.isfinite(current)
            or current < start
            or current - start > CLASSIFICATION_DEADLINE_SECONDS
        )

    def finish(
        reason: ClassificationReason,
        keys: tuple[str, ...] = (),
    ) -> MedicationIntakeClassification:
        if deadline_exceeded():
            return MedicationIntakeClassification(
                ClassificationReason.DEADLINE_EXCEEDED
            )
        return MedicationIntakeClassification(reason, keys)

    if type(text) is not str or type(catalog) is not MedicationCatalog:
        return finish(ClassificationReason.NO_CANDIDATE)
    if len(text) > MAX_INPUT_CHARS:
        return finish(ClassificationReason.INPUT_TOO_LONG)

    normalized = _normalize_text(text)
    if not normalized:
        return finish(ClassificationReason.NO_CANDIDATE)
    if any(marker in normalized for marker in _QUOTE_MARKERS):
        return finish(ClassificationReason.QUOTE_OR_REPLY)
    if any(marker in normalized for marker in _PERMISSION_MARKERS):
        return finish(ClassificationReason.PERMISSION_QUESTION)
    if any(marker in normalized for marker in _OTHER_PERSON_MARKERS):
        return finish(ClassificationReason.ANOTHER_PERSON)
    has_negation = any(marker in normalized for marker in _NEGATION_MARKERS)
    has_confirmed = is_confirmed_intake_grammar(normalized)
    if has_negation and ("지만" in normalized or normalized.count("먹었") > 1):
        return finish(ClassificationReason.MIXED_POLARITY)
    if has_negation:
        return finish(ClassificationReason.NEGATED)
    if any(marker in normalized for marker in _FUTURE_MARKERS):
        return finish(ClassificationReason.FUTURE)
    if normalized.endswith(("?", "？")):
        return finish(ClassificationReason.QUESTION)
    if not has_confirmed:
        return finish(ClassificationReason.NO_CANDIDATE)

    matched_keys: set[str] = set()
    matched_positions: list[int] = []
    ambiguous = False
    for alias, keys in catalog.alias_to_keys.items():
        if deadline_exceeded():
            return MedicationIntakeClassification(
                ClassificationReason.DEADLINE_EXCEEDED
            )
        position = find_alias_position(normalized, alias)
        if position >= 0:
            matched_positions.append(position)
            if len(keys) != 1:
                ambiguous = True
            matched_keys.update(keys)
    if ambiguous:
        return finish(ClassificationReason.AMBIGUOUS_ALIAS)
    if not matched_keys:
        return finish(ClassificationReason.UNKNOWN_ALIAS)
    if " 말고 " in normalized and len(matched_keys) > 1:
        return finish(ClassificationReason.MIXED_POLARITY)
    if _has_explicit_other_person_subject(normalized, min(matched_positions)):
        return finish(ClassificationReason.ANOTHER_PERSON)
    if matched_keys.intersection(catalog.retired_keys):
        return finish(ClassificationReason.UNKNOWN_ALIAS)
    return finish(ClassificationReason.SUPPORTED, tuple(sorted(matched_keys)))
