"""Load the YAML configuration and software catalog."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.yaml"
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "software_catalog.yaml"
DEFAULT_OUTREACH = PROJECT_ROOT / "config" / "outreach.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Config:
    raw: dict
    catalog: dict
    base_dir: Path = field(default=PROJECT_ROOT)
    outreach: dict = field(default_factory=dict)

    # --- convenience accessors -------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        value = Path(self.get(dotted))
        return value if value.is_absolute() else self.base_dir / value

    @property
    def categories(self) -> dict[str, dict]:
        return self.raw.get("categories", {})

    @property
    def software(self) -> dict[str, dict]:
        return self.catalog.get("software", {})

    @property
    def no_software(self) -> dict[str, dict]:
        return self.catalog.get("no_software", {})

    @property
    def usd_to_cad(self) -> float:
        if self.catalog.get("currency", "USD").upper() == "CAD":
            return 1.0
        return float(self.catalog.get("usd_to_cad", 1.38))

    @property
    def franchise_brands(self) -> list[str]:
        return [b.lower() for b in self.raw.get("franchise_brands", [])]

    @property
    def platform_hosts(self) -> list[str]:
        return self.catalog.get("platform_hosts", [])

    def software_label(self, key: str | None) -> str:
        if not key:
            key = "unknown"
        if key in self.software:
            return self.software[key].get("label", key)
        if key in self.no_software:
            return self.no_software[key].get("label", key)
        return key

    def google_api_key(self) -> str | None:
        env = self.get("sources.google_places.api_key_env", "GOOGLE_MAPS_API_KEY")
        return os.environ.get(env) or self.get("sources.google_places.api_key")


def load_config(
    config_path: str | os.PathLike | None = None,
    catalog_path: str | os.PathLike | None = None,
    overrides: dict | None = None,
) -> Config:
    """Load default config, optionally layering a user config on top.

    A user config only needs the keys it changes; everything else falls back
    to config/default.yaml.
    """
    with open(DEFAULT_CONFIG, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    base_dir = PROJECT_ROOT
    if config_path and Path(config_path).resolve() != DEFAULT_CONFIG:
        with open(config_path, encoding="utf-8") as fh:
            raw = _deep_merge(raw, yaml.safe_load(fh) or {})
        base_dir = Path(config_path).resolve().parent
        if base_dir.name == "config":
            base_dir = base_dir.parent
    if overrides:
        raw = _deep_merge(raw, overrides)

    with open(catalog_path or DEFAULT_CATALOG, encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh) or {}
    with open(DEFAULT_OUTREACH, encoding="utf-8") as fh:
        outreach = yaml.safe_load(fh) or {}
    # A user config may override any part of it under an `outreach:` key
    # (typically just `sender:`).
    outreach = _deep_merge(outreach, raw.get("outreach") or {})
    return Config(raw=raw, catalog=catalog, base_dir=base_dir, outreach=outreach)
