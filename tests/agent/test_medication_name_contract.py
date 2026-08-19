"""Tests for the sole medication-name canonicalization contract."""

from __future__ import annotations

import unicodedata
from types import SimpleNamespace

import pytest

import agent.medication_intake_intent as intent_module
import agent.medication_name_contract as contract_module
import tools.medication_intake_config as config_module
from agent.medication_intake_intent import MedicationCatalog
from agent.medication_name_contract import (
    MAX_MEDICATION_NAME_CHARS,
    normalize_medication_name,
)


class _StringSubclass(str):
    pass


@pytest.mark.parametrize("value", [None, 1, "", " \t\n ", _StringSubclass("concerta")])
def test_normalizer_rejects_non_exact_or_empty_values(value: object) -> None:
    with pytest.raises(ValueError, match="^medication_name_invalid$"):
        normalize_medication_name(value)


def test_normalizer_is_nfkc_casefold_and_unicode_whitespace_exact() -> None:
    value = "  ＣＯＮＣＥＲＴＡ\u2003콘서타\u00a0 "
    expected = " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    assert normalize_medication_name(value) == expected == "concerta 콘서타"


def test_normalizer_enforces_raw_and_normalized_character_bounds() -> None:
    with pytest.raises(ValueError, match="^medication_name_invalid$"):
        normalize_medication_name("a" * (MAX_MEDICATION_NAME_CHARS + 1))

    expanding = "\ufdfa" * 8
    assert len(expanding) <= MAX_MEDICATION_NAME_CHARS
    assert len(unicodedata.normalize("NFKC", expanding).casefold()) > MAX_MEDICATION_NAME_CHARS
    with pytest.raises(ValueError, match="^medication_name_invalid$"):
        normalize_medication_name(expanding)


def test_normalization_failure_is_constant_and_context_free(monkeypatch: pytest.MonkeyPatch) -> None:
    class HostileError(RuntimeError):
        def __str__(self) -> str:
            return "secret-name"

    def fail_normalization(_form: str, _value: str) -> str:
        raise HostileError()

    monkeypatch.setattr(
        contract_module,
        "unicodedata",
        SimpleNamespace(normalize=fail_normalization),
    )

    with pytest.raises(ValueError) as captured:
        normalize_medication_name("sensitive-name")

    assert captured.value.args == ("medication_name_invalid",)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "sensitive-name" not in repr(captured.value)
    assert "secret-name" not in repr(captured.value)


def test_intent_and_config_remove_private_alias_normalizers() -> None:
    assert not hasattr(intent_module, "_normalize_alias")
    assert not hasattr(config_module, "_normalize_alias")


def test_catalog_uses_the_same_canonicalizer_bytes() -> None:
    key = "1" * 64
    catalog = MedicationCatalog(
        alias_to_keys={"  ＣＯＮＣＥＲＴＡ\u2003콘서타  ": frozenset({key})},
        retired_keys=frozenset(),
        catalog_nonce="2" * 64,
        catalog_digest="3" * 64,
    )

    assert tuple(catalog.alias_to_keys) == (
        normalize_medication_name("  ＣＯＮＣＥＲＴＡ\u2003콘서타  "),
    )
