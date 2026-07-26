"""Validation contracts for the private medication catalog adapter."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from pathlib import Path

import pytest
import tools.medication_intake_config as medication_config_module

from tools.medication_intake_config import (
    MedicationIntakeConfigError,
    load_medication_intake_catalog,
)


PROFILE = "synthetic-profile"
USER = "100000000000000001"
CHAT = "100000000000000002"
THREAD = "100000000000000003"


NONCE = "00112233445566778899aabbccddeeff" * 2
KEY_A = "0123456789abcdef" * 4
KEY_B = "fedcba9876543210" * 4


def _random_material() -> tuple[str, str, str]:
    return NONCE, KEY_A, KEY_B


def _config() -> tuple[dict[str, object], str, str, str]:
    nonce, key_a, key_b = _random_material()
    config: dict[str, object] = {
        "registered_workflow": {
            "medication_intake": {
                "enabled": True,
                "profile_name": PROFILE,
                "allowed_platform": "discord",
                "allowed_user_ids": [USER],
                "allowed_chat_ids": [CHAT],
                "allowed_thread_ids": [THREAD],
                "catalog_nonce": nonce,
                "retired_keys": [],
                "entries": [
                    {
                        "medication_key": key_a,
                        "canonical_name": "합성알파",
                        "aliases": ["알파정"],
                    },
                    {
                        "medication_key": key_b,
                        "canonical_name": "합성베타",
                        "aliases": ["베타정"],
                    },
                ],
            }
        }
    }
    return config, nonce, key_a, key_b


def _load(config: dict[str, object], **overrides: object):
    scope: dict[str, object] = {
        "profile_name": PROFILE,
        "platform": "discord",
        "user_id": USER,
        "chat_id": CHAT,
        "thread_id": THREAD,
    }
    scope.update(overrides)
    return load_medication_intake_catalog(config, **scope)


def _assert_private_absent(marker: str, *surfaces: object) -> None:
    haystack = "\n".join(str(surface) for surface in surfaces)
    if marker in haystack:
        raise AssertionError("privacy marker detected")


def test_loads_immutable_alias_to_random_key_catalog_and_nonce_salted_digest() -> None:
    config, nonce, key_a, key_b = _config()
    catalog = _load(config)
    canonical = {
        "allowed_chat_ids": [CHAT],
        "allowed_platform": "discord",
        "allowed_thread_ids": [THREAD],
        "allowed_user_ids": [USER],
        "enabled": True,
        "entries": [
            {
                "aliases": ["알파정"],
                "canonical_name": "합성알파",
                "medication_key": key_a,
            },
            {
                "aliases": ["베타정"],
                "canonical_name": "합성베타",
                "medication_key": key_b,
            },
        ],
        "profile_name": PROFILE,
        "retired_keys": [],
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    expected = hashlib.sha256(
        b"lifelog-medication-catalog/v3\0" + bytes.fromhex(nonce) + payload
    ).hexdigest()

    _assert_private_absent("합성알파", catalog, repr(catalog))
    assert catalog.alias_to_keys["합성알파"] == frozenset({key_a})
    assert catalog.alias_to_keys["베타정"] == frozenset({key_b})
    assert catalog.retired_keys == frozenset()
    assert catalog.catalog_digest == expected
    assert len(nonce) == len(key_a) == len(key_b) == 64
    assert key_a != key_b != nonce
    assert "합성" not in key_a and "합성" not in key_b


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"profile_name": "wrong"}, "profile_denied"),
        ({"platform": "telegram"}, "platform_denied"),
        ({"user_id": "wrong"}, "user_denied"),
        ({"chat_id": "wrong"}, "chat_denied"),
        ({"thread_id": "wrong"}, "thread_denied"),
    ),
)
def test_exact_scope_denials_are_constant(overrides: dict[str, object], code: str) -> None:
    config, *_ = _config()
    with pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config, **overrides)
    assert str(exc_info.value) == code


def test_scope_str_subclasses_cannot_bypass_exact_identity() -> None:
    class FakePlatform(str):
        def __ne__(self, _other: object) -> bool:
            return False

    config, *_ = _config()
    with pytest.raises(MedicationIntakeConfigError, match="^platform_denied$"):
        _load(config, platform=FakePlatform("telegram"))


@pytest.mark.parametrize("mutation", ("missing", "disabled"))
def test_missing_or_disabled_config_fails_closed(mutation: str) -> None:
    config, *_ = _config()
    workflow = config["registered_workflow"]
    assert isinstance(workflow, dict)
    medication = workflow["medication_intake"]
    assert isinstance(medication, dict)
    if mutation == "missing":
        del workflow["medication_intake"]
    else:
        medication["enabled"] = False
    with pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config)
    assert str(exc_info.value) == f"config_{mutation}"


def test_malformed_duplicate_or_colliding_key_and_nonce_are_rejected() -> None:
    config, nonce, key_a, _ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    assert isinstance(medication, dict)

    cases = []
    malformed_key = json.loads(json.dumps(config))
    malformed_key["registered_workflow"]["medication_intake"]["entries"][0]["medication_key"] = "A" * 64
    cases.append(malformed_key)
    malformed_nonce = json.loads(json.dumps(config))
    malformed_nonce["registered_workflow"]["medication_intake"]["catalog_nonce"] = "A" * 64
    cases.append(malformed_nonce)
    duplicate_key = json.loads(json.dumps(config))
    duplicate_key["registered_workflow"]["medication_intake"]["entries"][1]["medication_key"] = key_a
    cases.append(duplicate_key)
    colliding_nonce = json.loads(json.dumps(config))
    colliding_nonce["registered_workflow"]["medication_intake"]["catalog_nonce"] = key_a
    cases.append(colliding_nonce)

    for candidate in cases:
        with pytest.raises(MedicationIntakeConfigError) as exc_info:
            _load(candidate)
        _assert_private_absent(nonce, exc_info.value, repr(exc_info.value))
        assert str(exc_info.value) in {"catalog_key_invalid", "catalog_nonce_invalid", "catalog_key_duplicate"}


def test_active_retired_intersection_and_retired_key_reactivation_are_denied() -> None:
    config, _, key_a, key_b = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    assert isinstance(medication, dict)
    medication["retired_keys"] = [key_a]
    with pytest.raises(MedicationIntakeConfigError, match="^retired_key_active$"):
        _load(config)

    config, _, _, _ = _config()
    with pytest.raises(MedicationIntakeConfigError, match="^retired_key_reactivated$"):
        _load(config, previous_retired_keys=frozenset({key_b}))


def test_retired_keys_are_append_only_and_immutable() -> None:
    config, _, _, key_b = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    assert isinstance(medication, dict)
    medication["retired_keys"] = [key_b]
    entries = medication["entries"]
    assert isinstance(entries, list)
    entries.pop()
    catalog = _load(config, previous_retired_keys=frozenset({key_b}))
    assert catalog.retired_keys == frozenset({key_b})
    with pytest.raises(AttributeError):
        catalog.retired_keys.add("abcdef0123456789" * 4)  # type: ignore[attr-defined]


def test_particle_boundary_alias_overlap_is_rejected() -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    entries = medication["entries"]  # type: ignore[index]
    entries[0]["canonical_name"] = "alpha"  # type: ignore[index]
    entries[0]["aliases"] = []  # type: ignore[index]
    entries[1]["canonical_name"] = "alpha랑"  # type: ignore[index]
    entries[1]["aliases"] = []  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_alias_overlap$"):
        _load(config)


def test_punctuation_boundary_alias_overlap_is_rejected() -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    entries = medication["entries"]  # type: ignore[index]
    entries[0]["canonical_name"] = "alpha"  # type: ignore[index]
    entries[0]["aliases"] = []  # type: ignore[index]
    entries[1]["canonical_name"] = "alpha!"  # type: ignore[index]
    entries[1]["aliases"] = []  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_alias_overlap$"):
        _load(config)


def test_embedded_boundary_alias_overlap_is_rejected() -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    entries = medication["entries"]  # type: ignore[index]
    entries[0]["canonical_name"] = "alpha"  # type: ignore[index]
    entries[0]["aliases"] = []  # type: ignore[index]
    entries[1]["canonical_name"] = "x alpha"  # type: ignore[index]
    entries[1]["aliases"] = []  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_alias_overlap$"):
        _load(config)


def test_catalog_digest_uses_exact_validated_config_excluding_only_nonce() -> None:
    config, nonce, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["allowed_user_ids"] = ["z-user", USER]  # type: ignore[index]
    medication["entries"].reverse()  # type: ignore[index]
    validated_without_nonce = dict(medication)  # type: ignore[arg-type]
    del validated_without_nonce["catalog_nonce"]
    expected_payload = json.dumps(
        validated_without_nonce,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    expected = hashlib.sha256(
        b"lifelog-medication-catalog/v3\0"
        + bytes.fromhex(nonce)
        + expected_payload
    ).hexdigest()
    catalog = _load(config)
    assert catalog.catalog_digest == expected


def test_state_visible_digest_resists_name_dictionary_without_private_nonce() -> None:
    config, _, *_ = _config()
    catalog = _load(config)
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    without_nonce = dict(medication)  # type: ignore[arg-type]
    del without_nonce["catalog_nonce"]
    public_shape = json.dumps(
        without_nonce,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    dictionary_candidates = (
        b"lifelog-medication-catalog/v3\0" + public_shape,
        b"lifelog-medication-catalog/v3\0" + "합성알파".encode(),
        b"lifelog-medication-catalog/v3\0" + "알파정".encode(),
    )
    guessed_digests = {hashlib.sha256(value).hexdigest() for value in dictionary_candidates}
    assert catalog.catalog_digest not in guessed_digests


def test_catalog_nonce_cannot_equal_retired_key() -> None:
    config, nonce, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["retired_keys"] = [nonce]  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_nonce_conflict$"):
        _load(config)


def test_active_key_cannot_be_reassigned_between_catalog_entries() -> None:
    config, _, key_a, _ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["entries"][0]["canonical_name"] = "새합성명"  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_key_reassigned$"):
        _load(config, previous_key_bindings={key_a: "합성알파"})


def test_key_rotation_and_missing_retirement_are_denied() -> None:
    config, _, key_a, key_b = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["retired_keys"] = [key_a]  # type: ignore[index]
    medication["entries"] = [  # type: ignore[index]
        {
            "medication_key": key_b,
            "canonical_name": "합성알파",
            "aliases": [],
        }
    ]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_key_rotated$"):
        _load(config, previous_key_bindings={key_a: "합성알파"})

    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["entries"].pop(0)  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_key_retirement_missing$"):
        _load(config, previous_key_bindings={key_a: "합성알파"})


def test_catalog_nonce_rotation_is_denied() -> None:
    config, nonce, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["catalog_nonce"] = "abcdef0123456789" * 4  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_nonce_rotated$"):
        _load(config, previous_catalog_nonce=nonce)


def test_format_validation_does_not_guess_entropy() -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    low_diversity_key = "a" * 64
    medication["entries"][0]["medication_key"] = low_diversity_key  # type: ignore[index]
    catalog = _load(config)
    assert catalog.alias_to_keys["합성알파"] == frozenset({low_diversity_key})


def test_alias_count_and_raw_length_bounds_run_before_normalization() -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["entries"][0]["aliases"] = [  # type: ignore[index]
        *(f"alias-{index}" for index in range(256)),
        object(),
    ]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_alias_limit$"):
        _load(config)

    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["entries"][0]["aliases"] = ["x" * 129]  # type: ignore[index]
    with pytest.raises(MedicationIntakeConfigError, match="^catalog_alias_invalid$"):
        _load(config)


def test_hostile_config_exception_chain_is_constant_and_empty() -> None:
    marker = "PRIVATE-BINDINGS"

    class HostileBindings(dict[str, str]):
        def items(self):
            raise RuntimeError(marker)

    config, *_ = _config()
    with pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config, previous_key_bindings=HostileBindings())
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value))
    assert str(exc_info.value) == "previous_key_bindings_invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_hostile_config_exception_stringifier_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileConfigError(MedicationIntakeConfigError):
        def __str__(self) -> str:
            raise RuntimeError("PRIVATE-CONFIG-STR")

    def hostile_impl(*_args: object, **_kwargs: object) -> object:
        raise HostileConfigError()

    monkeypatch.setattr(
        medication_config_module,
        "_load_medication_intake_catalog_impl",
        hostile_impl,
    )
    config, *_ = _config()
    with pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config)
    assert str(exc_info.value) == "config_invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    "second_alias",
    ("합성알파", "  합성알파  ", "ＡＬＰＨＡ", "alpha extended"),
)
def test_duplicate_and_overlap_aliases_are_rejected(second_alias: str) -> None:
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    assert isinstance(medication, dict)
    entries = medication["entries"]
    assert isinstance(entries, list)
    first, second = entries
    assert isinstance(first, dict) and isinstance(second, dict)
    first["aliases"] = ["alpha"]
    second["aliases"] = [second_alias]
    if "합성" in second_alias:
        first["canonical_name"] = "합성알파"
    with pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config)
    assert str(exc_info.value) in {"catalog_alias_duplicate", "catalog_alias_overlap"}


def test_raw_values_never_appear_in_errors_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    marker = "PRIVATE-SYNTHETIC-MEDICATION-MARKER"
    config, *_ = _config()
    medication = config["registered_workflow"]["medication_intake"]  # type: ignore[index]
    medication["entries"][0]["canonical_name"] = marker  # type: ignore[index]
    medication["entries"][0]["aliases"] = [marker]  # type: ignore[index]
    with caplog.at_level(logging.DEBUG), pytest.raises(MedicationIntakeConfigError) as exc_info:
        _load(config)
    _assert_private_absent(marker, exc_info.value, repr(exc_info.value), caplog.text)
    assert str(exc_info.value) == "catalog_alias_duplicate"


def test_adapter_never_generates_ids_or_reads_environment_or_credentials() -> None:
    path = Path(__file__).parents[2] / "tools" / "medication_intake_config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_imports = {"os", "secrets", "random", "uuid", "keyring"}
    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert not imported.intersection(forbidden_imports)
    assert not called.intersection({"getenv", "urandom", "token_hex", "uuid4"})
