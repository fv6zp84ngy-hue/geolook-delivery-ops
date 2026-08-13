"""Single source of truth for the Release Candidate identity."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
SCHEMA_VERSION = "2.0"
PRODUCT_STAGE = "public_alpha"
RELEASE_LABEL = "Public Alpha RC"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-rc\.\d+$")


def get_distribution_version() -> str:
    """Read and validate the distribution version from ``VERSION``."""
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not _VERSION_RE.fullmatch(value):
        raise ValueError(f"VERSION 格式非法：{value!r}")
    return value


def version_info() -> dict[str, str]:
    return {
        "distribution_version": get_distribution_version(),
        "schema_version": SCHEMA_VERSION,
        "product_stage": PRODUCT_STAGE,
        "release_label": RELEASE_LABEL,
    }
