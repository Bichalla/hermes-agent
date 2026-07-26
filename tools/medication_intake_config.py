"""Validated adapter for a private medication-intake catalog.

The adapter accepts an already-loaded config mapping.  It performs no file,
environment, credential, random-ID, logging, or live-state access.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import NoReturn

from agent.medication_intake_intent import (
    MAX_ALIAS_CHARS,
    MAX_CATALOG_ALIASES,
    MedicationCatalog,
    find_alias_position,
)

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_FIELDS = frozenset(
    {
        "enabled",
        "profile_name",
        "allowed_platform",
        "allowed_user_ids",
        "allowed_chat_ids",
        "allowed_thread_ids",
        "catalog_nonce",
        "retired_keys",
        "entries",
    }
)
_ENTRY_FIELDS = frozenset({"medication_key", "canonical_name", "aliases"})
_CONFIG_ERROR_CODES = frozenset(
    {
        "config_invalid", "config_missing", "config_disabled",
        "profile_denied", "platform_denied", "user_denied", "chat_denied",
        "thread_denied", "user_allowlist_invalid", "chat_allowlist_invalid",
        "thread_allowlist_invalid", "catalog_nonce_invalid",
        "previous_catalog_nonce_invalid", "catalog_nonce_rotated",
        "previous_retired_invalid", "previous_key_bindings_invalid",
        "retired_keys_invalid", "retired_keys_duplicate", "retired_key_reactivated",
        "catalog_nonce_conflict", "catalog_entries_invalid", "catalog_entry_invalid",
        "catalog_alias_invalid", "catalog_alias_limit", "catalog_alias_duplicate",
        "catalog_alias_overlap", "catalog_key_invalid", "catalog_key_duplicate",
        "catalog_key_reassigned", "catalog_key_rotated",
        "catalog_key_retirement_missing", "retired_key_active",
    }
)



class MedicationIntakeConfigError(ValueError):
    """Constant-code configuration denial safe for logs and tracebacks."""



def _fail(code: str) -> NoReturn:
    raise MedicationIntakeConfigError(code) from None



def _normalize_alias(value: object) -> str:
    if type(value) is not str:
        _fail("catalog_alias_invalid")
    if not value or len(value) > MAX_ALIAS_CHARS:
        _fail("catalog_alias_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > MAX_ALIAS_CHARS:
        _fail("catalog_alias_invalid")
    return normalized



def _is_random_hex(value: object) -> bool:
    return type(value) is str and _HEX_64.fullmatch(value) is not None



def _string_allowlist(value: object, *, code: str) -> tuple[str, ...]:
    if type(value) is not list or not value:
        _fail(code)
    if any(type(item) is not str or not item for item in value):
        _fail(code)
    result = tuple(value)
    if len(set(result)) != len(result):
        _fail(code)
    return result



def _overlaps(first: str, second: str) -> bool:
    return (
        find_alias_position(first, second) >= 0
        or find_alias_position(second, first) >= 0
    )



def _load_medication_intake_catalog_impl(
    config: Mapping[str, object],
    *,
    profile_name: str,
    platform: str,
    user_id: str,
    chat_id: str,
    thread_id: str,
    previous_retired_keys: frozenset[str] = frozenset(),
    previous_key_bindings: Mapping[str, str] | None = None,
    previous_catalog_nonce: str | None = None,
) -> MedicationCatalog:
    """Validate exact authority scope and return an immutable private catalog."""

    try:
        if type(config) is not dict:
            _fail("config_invalid")
        registered = config.get("registered_workflow")
        if type(registered) is not dict:
            _fail("config_missing")
        medication = registered.get("medication_intake")
        if medication is None:
            _fail("config_missing")
        if type(medication) is not dict or frozenset(medication) != _CONFIG_FIELDS:
            _fail("config_invalid")
        if medication["enabled"] is not True:
            _fail("config_disabled")
        configured_profile = medication["profile_name"]
        if (
            type(configured_profile) is not str
            or type(profile_name) is not str
            or profile_name != configured_profile
        ):
            _fail("profile_denied")
        configured_platform = medication["allowed_platform"]
        if (
            type(configured_platform) is not str
            or configured_platform != "discord"
            or type(platform) is not str
            or platform != "discord"
        ):
            _fail("platform_denied")
        allowed_users = _string_allowlist(medication["allowed_user_ids"], code="user_allowlist_invalid")
        allowed_chats = _string_allowlist(medication["allowed_chat_ids"], code="chat_allowlist_invalid")
        allowed_threads = _string_allowlist(medication["allowed_thread_ids"], code="thread_allowlist_invalid")
        if type(user_id) is not str or user_id not in allowed_users:
            _fail("user_denied")
        if type(chat_id) is not str or chat_id not in allowed_chats:
            _fail("chat_denied")
        if type(thread_id) is not str or thread_id not in allowed_threads:
            _fail("thread_denied")

        nonce = medication["catalog_nonce"]
        if not _is_random_hex(nonce):
            _fail("catalog_nonce_invalid")
        if previous_catalog_nonce is not None:
            if not _is_random_hex(previous_catalog_nonce):
                _fail("previous_catalog_nonce_invalid")
            if previous_catalog_nonce != nonce:
                _fail("catalog_nonce_rotated")
        if type(previous_retired_keys) is not frozenset or any(
            not _is_random_hex(key) for key in previous_retired_keys
        ):
            _fail("previous_retired_invalid")
        if previous_key_bindings is None:
            normalized_previous_bindings: dict[str, str] = {}
        elif type(previous_key_bindings) is dict:
            normalized_previous_bindings = {}
            for previous_key, previous_name in previous_key_bindings.items():
                if not _is_random_hex(previous_key):
                    _fail("previous_key_bindings_invalid")
                normalized_previous_bindings[previous_key] = _normalize_alias(previous_name)
        else:
            _fail("previous_key_bindings_invalid")

        raw_retired = medication["retired_keys"]
        if type(raw_retired) is not list or any(not _is_random_hex(key) for key in raw_retired):
            _fail("retired_keys_invalid")
        retired_keys = frozenset(raw_retired)
        if len(retired_keys) != len(raw_retired):
            _fail("retired_keys_duplicate")
        if not previous_retired_keys.issubset(retired_keys):
            _fail("retired_key_reactivated")
        if nonce in retired_keys:
            _fail("catalog_nonce_conflict")

        entries = medication["entries"]
        if type(entries) is not list or not entries:
            _fail("catalog_entries_invalid")
        alias_count = len(entries)
        for entry in entries:
            if type(entry) is not dict or frozenset(entry) != _ENTRY_FIELDS:
                _fail("catalog_entry_invalid")
            raw_aliases = entry["aliases"]
            if type(raw_aliases) is not list:
                _fail("catalog_alias_invalid")
            alias_count += len(raw_aliases)
            if alias_count > MAX_CATALOG_ALIASES:
                _fail("catalog_alias_limit")
            raw_names = [entry["canonical_name"], *raw_aliases]
            if any(
                type(raw_name) is not str
                or not raw_name
                or len(raw_name) > MAX_ALIAS_CHARS
                for raw_name in raw_names
            ):
                _fail("catalog_alias_invalid")
        alias_to_key: dict[str, str] = {}
        active_keys: set[str] = set()
        current_key_bindings: dict[str, str] = {}
        for entry in entries:
            medication_key = entry["medication_key"]
            if not _is_random_hex(medication_key) or medication_key == nonce:
                _fail("catalog_key_invalid")
            if medication_key in active_keys:
                _fail("catalog_key_duplicate")
            active_keys.add(medication_key)
            canonical_name = _normalize_alias(entry["canonical_name"])
            previous_name = normalized_previous_bindings.get(medication_key)
            if previous_name is not None and previous_name != canonical_name:
                _fail("catalog_key_reassigned")
            current_key_bindings[medication_key] = canonical_name
            raw_aliases = entry["aliases"]
            normalized_aliases = [_normalize_alias(alias) for alias in raw_aliases]
            aliases = [canonical_name, *normalized_aliases]
            for alias in aliases:
                if alias in alias_to_key:
                    _fail("catalog_alias_duplicate")
                for existing in alias_to_key:
                    if _overlaps(alias, existing):
                        _fail("catalog_alias_overlap")
                alias_to_key[alias] = medication_key


        if len(alias_to_key) > MAX_CATALOG_ALIASES:
            _fail("catalog_alias_limit")
        if active_keys.intersection(retired_keys):
            _fail("retired_key_active")
        current_name_to_key = {
            canonical_name: medication_key
            for medication_key, canonical_name in current_key_bindings.items()
        }
        for previous_key, previous_name in normalized_previous_bindings.items():
            current_key = current_name_to_key.get(previous_name)
            if current_key is not None and current_key != previous_key:
                _fail("catalog_key_rotated")
            if previous_key not in active_keys and previous_key not in retired_keys:
                _fail("catalog_key_retirement_missing")
        canonical = {
            key: medication[key]
            for key in _CONFIG_FIELDS
            if key != "catalog_nonce"
        }
        payload = json.dumps(
            canonical,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        digest = hashlib.sha256(
            b"lifelog-medication-catalog/v3\0"
            + bytes.fromhex(nonce)
            + payload
        ).hexdigest()
        return MedicationCatalog(
            alias_to_keys={
                alias: frozenset({key}) for alias, key in alias_to_key.items()
            },
            retired_keys=retired_keys,
            catalog_nonce=nonce,
            catalog_digest=digest,
        )
    except MedicationIntakeConfigError:
        raise
    except Exception:
        raise MedicationIntakeConfigError("config_invalid") from None


def load_medication_intake_catalog(
    config: Mapping[str, object],
    *,
    profile_name: str,
    platform: str,
    user_id: str,
    chat_id: str,
    thread_id: str,
    previous_retired_keys: frozenset[str] = frozenset(),
    previous_key_bindings: Mapping[str, str] | None = None,
    previous_catalog_nonce: str | None = None,
) -> MedicationCatalog:
    """Sanitize every adapter-boundary failure to one closed constant code."""

    error_code: str | None = None
    try:
        return _load_medication_intake_catalog_impl(
            config,
            profile_name=profile_name,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            previous_retired_keys=previous_retired_keys,
            previous_key_bindings=previous_key_bindings,
            previous_catalog_nonce=previous_catalog_nonce,
        )
    except MedicationIntakeConfigError as exc:
        candidate = (
            exc.args[0]
            if type(exc) is MedicationIntakeConfigError
            and len(exc.args) == 1
            and type(exc.args[0]) is str
            else None
        )
        error_code = candidate if candidate in _CONFIG_ERROR_CODES else "config_invalid"
    except Exception:
        error_code = "config_invalid"
    raise MedicationIntakeConfigError(error_code or "config_invalid") from None
