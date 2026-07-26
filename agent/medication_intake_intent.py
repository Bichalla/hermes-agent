"""Pure, raw-free medication-intake intent classification.

The classifier consumes only caller-supplied direct user text and an immutable
private catalog.  It performs no I/O, logging, fuzzy matching, or state access.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

MAX_INPUT_CHARS = 4096
MAX_CATALOG_ALIASES = 256
MAX_ALIAS_CHARS = 128
CLASSIFICATION_DEADLINE_SECONDS = 0.020

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


@dataclass(frozen=True, slots=True, repr=False)
class MedicationCatalog:
    """Immutable alias index containing only normalized aliases and opaque keys."""

    alias_to_keys: Mapping[str, frozenset[str]]
    retired_keys: frozenset[str]
    catalog_nonce: str
    catalog_digest: str

    def __post_init__(self) -> None:
        error_code: str | None = None
        normalized: dict[str, frozenset[str]] = {}
        try:
            if type(self.alias_to_keys) not in (dict, MappingProxyType):
                raise ValueError("catalog_aliases_invalid")
            if len(self.alias_to_keys) > MAX_CATALOG_ALIASES:
                raise ValueError("catalog_alias_limit")
            for raw_alias, raw_keys in self.alias_to_keys.items():
                alias = _normalize_alias(raw_alias)
                if alias in normalized:
                    raise ValueError("catalog_alias_duplicate")
                if type(raw_keys) is not frozenset or not raw_keys:
                    raise ValueError("catalog_keys_invalid")
                if any(type(key) is not str or not _HEX_64.fullmatch(key) for key in raw_keys):
                    raise ValueError("catalog_keys_invalid")
                normalized[alias] = frozenset(raw_keys)
            if type(self.retired_keys) is not frozenset or any(
                type(key) is not str or not _HEX_64.fullmatch(key)
                for key in self.retired_keys
            ):
                raise ValueError("catalog_retired_invalid")
            if type(self.catalog_nonce) is not str or not _HEX_64.fullmatch(self.catalog_nonce):
                raise ValueError("catalog_nonce_invalid")
            if type(self.catalog_digest) is not str or not _HEX_64.fullmatch(self.catalog_digest):
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
        except Exception:
            error_code = "catalog_invalid"
        if error_code is not None:
            raise ValueError(error_code) from None
        object.__setattr__(self, "alias_to_keys", MappingProxyType(normalized))
        object.__setattr__(self, "retired_keys", frozenset(self.retired_keys))

    def __repr__(self) -> str:
        return "MedicationCatalog(<private>)"


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



def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())



def _normalize_alias(alias: object) -> str:
    if type(alias) is not str:
        raise ValueError("catalog_alias_invalid")
    if not alias or len(alias) > MAX_ALIAS_CHARS:
        raise ValueError("catalog_alias_invalid")
    normalized = _normalize_text(alias).strip()
    if not normalized or len(normalized) > MAX_ALIAS_CHARS:
        raise ValueError("catalog_alias_invalid")
    return normalized



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
        except Exception:
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
        except Exception:
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
