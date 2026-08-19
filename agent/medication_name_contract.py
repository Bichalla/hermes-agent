"""Single pure canonicalizer for medication names and aliases."""

from __future__ import annotations

import unicodedata
from typing import NoReturn

MAX_MEDICATION_NAME_CHARS = 128
_ERROR_CODE = "medication_name_invalid"


def _deny() -> NoReturn:
    raise ValueError(_ERROR_CODE) from None


def normalize_medication_name(value: object) -> str:
    """Return the bounded NFKC/casefold/Unicode-whitespace canonical form.

    The boundary accepts exact built-in strings only and emits one raw-free
    constant denial for every invalid input or normalization failure.
    """

    if type(value) is not str or not value or len(value) > MAX_MEDICATION_NAME_CHARS:
        _deny()

    failed = False
    normalized: str | None = None
    try:
        normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    except BaseException:
        failed = True

    if (
        failed
        or type(normalized) is not str
        or not normalized
        or len(normalized) > MAX_MEDICATION_NAME_CHARS
    ):
        _deny()
    return normalized
