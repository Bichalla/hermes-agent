"""Behavior contracts for private medication-intake intent classification."""

from __future__ import annotations

import dataclasses
import logging
import string
from types import MappingProxyType

import pytest

from agent.medication_intake_intent import (
    MAX_ALIAS_CHARS,
    MAX_CATALOG_ALIASES,
    MAX_INPUT_CHARS,
    ClassificationReason,
    MedicationCatalog,
    classify_medication_intake,
    is_confirmed_intake_grammar,
)


KEY_A = "0123456789abcdef" * 4
KEY_B = "fedcba9876543210" * 4
NONCE = "00112233445566778899aabbccddeeff" * 2
CATALOG_DIGEST = "89abcdef01234567" * 4


def _catalog(
    aliases: dict[str, frozenset[str]] | None = None,
) -> MedicationCatalog:
    return MedicationCatalog(
        alias_to_keys=aliases
        or {
            "합성알파": frozenset({KEY_A}),
            "알파정": frozenset({KEY_A}),
            "합성베타": frozenset({KEY_B}),
        },
        retired_keys=frozenset(),
        catalog_nonce=NONCE,
        catalog_digest=CATALOG_DIGEST,
    )


def _assert_private_absent(marker: str, *surfaces: object) -> None:
    haystack = "\n".join(str(surface) for surface in surfaces)
    if marker in haystack:
        raise AssertionError("privacy marker detected")


def test_missing_intent_contract(caplog: pytest.LogCaptureFixture) -> None:
    marker = "합성알파"
    with caplog.at_level(logging.DEBUG):
        result = classify_medication_intake(f"{marker} 복용했어", _catalog())
    _assert_private_absent(marker, result, repr(result), caplog.text)
    assert result.reason is ClassificationReason.SUPPORTED
    assert result.medication_keys == (KEY_A,)


@pytest.mark.parametrize(
    ("text", "reason", "keys"),
    (
        ("합성알파 먹었어", ClassificationReason.SUPPORTED, (KEY_A,)),
        ("알파정 복용했습니다!", ClassificationReason.SUPPORTED, (KEY_A,)),
        ("합성알파 먹엇어", ClassificationReason.SUPPORTED, (KEY_A,)),
        ("합성베타랑 합성알파 복용했어요.", ClassificationReason.SUPPORTED, tuple(sorted((KEY_A, KEY_B)))),
        ("합성알파 안 먹었어", ClassificationReason.NEGATED, ()),
        ("합성알파는 복용하지 않았어", ClassificationReason.NEGATED, ()),
        ("합성알파 먹을게", ClassificationReason.FUTURE, ()),
        ("합성알파 복용할 예정이야", ClassificationReason.FUTURE, ()),
        ("합성알파 먹었어?", ClassificationReason.QUESTION, ()),
        ("합성알파 먹어도 돼?", ClassificationReason.PERMISSION_QUESTION, ()),
        ("> 합성알파 먹었어", ClassificationReason.QUOTE_OR_REPLY, ()),
        ("[reply] 합성알파 복용했어", ClassificationReason.QUOTE_OR_REPLY, ()),
        ("합성알파 먹었지만 합성베타는 안 먹었어", ClassificationReason.MIXED_POLARITY, ()),
        ("친구가 합성알파 먹었어", ClassificationReason.ANOTHER_PERSON, ()),
        ("합성감마 복용했어", ClassificationReason.UNKNOWN_ALIAS, ()),
        ("오늘 산책했어", ClassificationReason.NO_CANDIDATE, ()),
    ),
)
def test_closed_intent_matrix(text: str, reason: ClassificationReason, keys: tuple[str, ...]) -> None:
    result = classify_medication_intake(text, _catalog())
    _assert_private_absent(text, result, repr(result))
    assert result.reason is reason
    assert result.medication_keys == keys


def test_ambiguous_alias_fails_closed_without_disclosing_alias() -> None:
    marker = "합성중복"
    catalog = _catalog({marker: frozenset({KEY_A, KEY_B})})
    result = classify_medication_intake(f"{marker} 먹었어", catalog)
    _assert_private_absent(marker, result, repr(result))
    assert result.reason is ClassificationReason.AMBIGUOUS_ALIAS
    assert result.medication_keys == ()


def test_no_fuzzy_edit_distance() -> None:
    result = classify_medication_intake("합성알파아 먹었어", _catalog())
    assert result.reason is ClassificationReason.UNKNOWN_ALIAS
    assert result.medication_keys == ()


def test_result_and_catalog_are_deeply_immutable_and_safe_repr() -> None:
    catalog = _catalog()
    result = classify_medication_intake("합성알파 복용했어", catalog)
    assert dataclasses.is_dataclass(result)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        result.medication_keys = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        catalog.alias_to_keys["추가"] = frozenset({KEY_A})  # type: ignore[index]
    assert isinstance(catalog.alias_to_keys, MappingProxyType)
    assert repr(result) == "MedicationIntakeClassification(<opaque>)"
    assert repr(catalog) == "MedicationCatalog(<private>)"


def test_hostile_catalog_mapping_error_is_constant_with_empty_chain() -> None:
    marker = "PRIVATE-MAPPING"

    class HostileDict(dict[str, frozenset[str]]):
        def items(self):
            raise ValueError(marker)

    with pytest.raises(ValueError) as exc_info:
        MedicationCatalog(
            alias_to_keys=MappingProxyType(HostileDict()),
            retired_keys=frozenset(),
            catalog_nonce=NONCE,
            catalog_digest=CATALOG_DIGEST,
        )
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "catalog_invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_hostile_catalog_exception_stringifier_is_never_called() -> None:
    marker = "PRIVATE-STR"

    class HostileValueError(ValueError):
        def __str__(self) -> str:
            raise RuntimeError(marker)

    class HostileDict(dict[str, frozenset[str]]):
        def items(self):
            raise HostileValueError()

    with pytest.raises(ValueError) as exc_info:
        MedicationCatalog(
            alias_to_keys=MappingProxyType(HostileDict()),
            retired_keys=frozenset(),
            catalog_nonce=NONCE,
            catalog_digest=CATALOG_DIGEST,
        )
    assert str(exc_info.value) == "catalog_invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_input_and_catalog_bounds_fail_closed_with_constant_safe_results() -> None:
    too_long = "민감" + ("x" * MAX_INPUT_CHARS)
    result = classify_medication_intake(too_long, _catalog())
    _assert_private_absent(too_long, result, repr(result))
    assert result.reason is ClassificationReason.INPUT_TOO_LONG

    aliases = {
        f"합성{i:03d}": frozenset({KEY_A})
        for i in range(MAX_CATALOG_ALIASES + 1)
    }
    with pytest.raises(ValueError) as exc_info:
        _catalog(aliases)
    _assert_private_absent("합성000", exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "catalog_alias_limit"

    with pytest.raises(ValueError) as exc_info:
        _catalog({"x" * (MAX_ALIAS_CHARS + 1): frozenset({KEY_A})})
    assert str(exc_info.value) == "catalog_alias_invalid"


def test_deadline_is_injected_and_fail_closed_at_twenty_ms() -> None:
    ticks = iter((10.0, 10.021))
    result = classify_medication_intake(
        "합성알파 복용했어",
        _catalog(),
        monotonic=lambda: next(ticks),
    )
    assert result.reason is ClassificationReason.DEADLINE_EXCEEDED
    assert result.medication_keys == ()


def test_initial_monotonic_exception_is_constant_raw_free() -> None:
    marker = "PRIVATE-MONOTONIC"

    def hostile_clock() -> float:
        raise RuntimeError(marker)

    result = classify_medication_intake(
        "합성알파 복용했어",
        _catalog(),
        monotonic=hostile_clock,
    )
    _assert_private_absent(marker, result, repr(result))
    assert result.reason is ClassificationReason.DEADLINE_EXCEEDED


def test_deadline_is_checked_during_alias_scan() -> None:
    aliases = {
        f"합성{index:03d}": frozenset({KEY_A})
        for index in range(MAX_CATALOG_ALIASES)
    }
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 1.021 if calls >= 5 else 1.0

    result = classify_medication_intake(
        "합성미상 복용했어",
        _catalog(aliases),
        monotonic=clock,
    )
    assert result.reason is ClassificationReason.DEADLINE_EXCEEDED
    assert calls == 5


@pytest.mark.parametrize(
    ("text", "reason"),
    (
        ("남편이 합성알파 먹었어", ClassificationReason.ANOTHER_PERSON),
        ("아내가 합성알파 먹었어", ClassificationReason.ANOTHER_PERSON),
        ("이전 답장: 합성알파 먹었어", ClassificationReason.QUOTE_OR_REPLY),
        ("reply history: 합성알파 먹었어", ClassificationReason.QUOTE_OR_REPLY),
    ),
)
def test_other_person_and_embedded_reply_history_never_return_supported(
    text: str,
    reason: ClassificationReason,
) -> None:
    result = classify_medication_intake(text, _catalog())
    assert result.reason is reason
    assert result.medication_keys == ()


def test_unlisted_other_person_subject_is_closed_without_blocking_explicit_self() -> None:
    other = classify_medication_intake("동생이 합성알파 먹었어", _catalog())
    assert other.reason is ClassificationReason.ANOTHER_PERSON
    assert other.medication_keys == ()

    for text in ("내가 합성알파 먹었어", "제가 합성알파 먹었어"):
        own = classify_medication_intake(text, _catalog())
        assert own.reason is ClassificationReason.SUPPORTED
        assert own.medication_keys == (KEY_A,)


def test_topic_particle_other_person_and_exclusion_phrase_fail_closed() -> None:
    other = classify_medication_intake("동생은 합성알파 먹었어", _catalog())
    assert other.reason is ClassificationReason.ANOTHER_PERSON
    assert other.medication_keys == ()

    exclusion = classify_medication_intake(
        "합성알파 말고 합성베타 먹었어",
        _catalog(),
    )
    assert exclusion.reason is ClassificationReason.MIXED_POLARITY
    assert exclusion.medication_keys == ()

    embedded = classify_medication_intake(
        "동생은 오늘 합성알파 먹었어",
        _catalog(),
    )
    assert embedded.reason is ClassificationReason.ANOTHER_PERSON
    assert embedded.medication_keys == ()

    temporal_self = classify_medication_intake("오늘은 합성알파 먹었어", _catalog())
    assert temporal_self.reason is ClassificationReason.SUPPORTED


def test_monotonic_regression_and_nonfinite_values_fail_closed() -> None:
    for ticks in ((10.0, 9.0), (10.0, float("nan"))):
        iterator = iter(ticks)
        result = classify_medication_intake(
            "합성알파 먹었어",
            _catalog(),
            monotonic=lambda: next(iterator),
        )
        assert result.reason is ClassificationReason.DEADLINE_EXCEEDED


def test_stage_one_contains_every_stage_two_confirmed_outcome_across_generated_cases() -> None:
    punctuation = ("", ".", "!", "！", "…")
    aliases = ("합성알파", "ＡＬＰＨＡ")
    catalog = _catalog(
        {
            "합성알파": frozenset({KEY_A}),
            "alpha": frozenset({KEY_B}),
            "합성중복": frozenset({KEY_A, KEY_B}),
        }
    )
    samples = []
    for alias in aliases:
        for suffix in ("먹었어", "복용했어", "먹엇어"):
            for mark in punctuation:
                samples.append(f"{alias} {suffix}{mark}")
    samples.extend(("합성미상 먹었어", "합성중복 복용했어"))

    covered = set()
    for text in samples:
        result = classify_medication_intake(text, catalog)
        if result.reason in {
            ClassificationReason.SUPPORTED,
            ClassificationReason.UNKNOWN_ALIAS,
            ClassificationReason.AMBIGUOUS_ALIAS,
        }:
            covered.add(result.reason)
            assert is_confirmed_intake_grammar(text)
    assert covered == {
        ClassificationReason.SUPPORTED,
        ClassificationReason.UNKNOWN_ALIAS,
        ClassificationReason.AMBIGUOUS_ALIAS,
    }


def test_unicode_normalization_and_punctuation_are_deterministic() -> None:
    catalog = _catalog({"alpha": frozenset({KEY_A})})
    cases = ("ＡＬＰＨＡ 복용했어", "alpha　복용했어！", "Alpha 복용했어...")
    for text in cases:
        result = classify_medication_intake(text, catalog)
        assert result.reason is ClassificationReason.SUPPORTED
        assert result.medication_keys == (KEY_A,)


def test_matching_uses_bounded_non_regex_alias_scanning() -> None:
    # Adversarial punctuation has no regex semantics because aliases are literal.
    aliases = {
        f"합성{char}{index}": frozenset({KEY_A})
        for index, char in enumerate(string.punctuation)
    }
    catalog = _catalog(aliases)
    result = classify_medication_intake("합성*9 복용했어", catalog)
    assert result.reason is ClassificationReason.SUPPORTED
