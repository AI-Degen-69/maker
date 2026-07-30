"""Network + env config, shared in spirit with the taker repo.

Duplicated deliberately rather than packaged: ~40 lines of stable code, and a
shared library across two repos costs more in version management than the
duplication costs in drift. See docs spec 2026-07-21 section 4.

The maker never constructs a CLOB client and never signs anything, so unlike
the taker this loads NO wallet credentials at all -- there is nothing here to
leak.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class NetConfig:
    clob_host: str = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
    gamma_host: str = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
    series_slug: str = "btc-up-or-down-5m"


def load_net() -> NetConfig:
    return NetConfig()
