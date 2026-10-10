"""Company name normalization for the (industry, normalized_name) unique key."""

import re
import unicodedata

_SUFFIXES = (
    " incorporated",
    " corporation",
    " company",
    " limited",
    " corp",
    " inc",
    " llc",
    " ltd",
    " co",
)
_SKIP_NAMES = {"unknown", "n/a", "na", "none", "not specified", "unspecified"}


def display_company_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()[:255]


def normalize_company_name(name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", name).casefold()
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    trimmed = True
    while trimmed:
        trimmed = False
        for suffix in _SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                trimmed = True
    return cleaned[:255]


def is_usable_company_name(name: str) -> bool:
    normalized = normalize_company_name(name)
    return bool(normalized) and normalized not in _SKIP_NAMES
