# -*- coding: utf-8 -*-
"""Local privacy preferences for persistence and external processing.

Only preferences are stored here; user text, evidence, and model payloads must
never be written to this file. Missing or corrupt configuration fails closed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "DATA_CATEGORIES",
    "DEFAULT_PREFERENCES",
    "PrivacyPreferences",
    "PrivacyAuthorizationError",
    "default_path",
    "load_preferences",
    "save_preferences",
]

DATA_CATEGORIES = ("results", "raw_input", "external_cache")
DEFAULT_PREFERENCES: Dict[str, Any] = {
    "schema_version": 1,
    "retention_days": {"results": 90, "raw_input": 30, "external_cache": 7},
    "save_enabled": {"results": False, "raw_input": False, "external_cache": False},
    "external_processing_enabled": False,
    "external_processing_providers": [],
}

_LOCK = threading.RLock()
_PATH_ENV = "MYLOCALAI_PRIVACY_PREFERENCES_PATH"


class PrivacyAuthorizationError(PermissionError):
    """An external provider call was attempted without explicit authorization."""


class PrivacyPreferences:
    """Validated immutable-ish preference snapshot for one operation boundary."""

    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None,
                 warning: str = "") -> None:
        self._data = data
        self.path = path
        self.warning = warning

    def as_dict(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def save_enabled(self, category: str) -> bool:
        self._category(category)
        return bool(self._data["save_enabled"][category])

    def retention_days(self, category: str) -> int:
        self._category(category)
        return int(self._data["retention_days"][category])

    def persistence_decision(self, category: str) -> Dict[str, Any]:
        """Return one write-time decision; callers must not infer policy from defaults."""
        self._category(category)
        return {"allowed": self.save_enabled(category),
                "category": category,
                "retention_days": self.retention_days(category) if self.save_enabled(category) else None}

    def raw_input_allowed(self) -> bool:
        return self.save_enabled("raw_input")

    def result_allowed(self) -> bool:
        return self.save_enabled("results")

    def cache_allowed(self) -> bool:
        return self.save_enabled("external_cache")

    def external_allowed(self, provider: str) -> bool:
        if not isinstance(provider, str) or not provider.strip():
            return False
        return bool(self._data["external_processing_enabled"] and
                    provider in self._data["external_processing_providers"])

    def require_external(self, provider: str) -> None:
        if not self.external_allowed(provider):
            raise PrivacyAuthorizationError(
                f"External processing is disabled for provider {provider!r}; explicit authorization is required.")

    def allows_provider_url(self, provider: str, base_url: str) -> bool:
        """Treat configured loopback model endpoints as local; all others need consent."""
        from urllib.parse import urlsplit
        try:
            host = (urlsplit(str(base_url or "")).hostname or "").lower().rstrip(".")
        except ValueError:
            host = ""
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return self.external_allowed(provider)

    def require_provider(self, provider: str, base_url: str) -> None:
        if not self.allows_provider_url(provider, base_url):
            raise PrivacyAuthorizationError(
                f"External processing is disabled for provider {provider!r}; explicit authorization is required.")

    @staticmethod
    def _category(category: str) -> None:
        if category not in DATA_CATEGORIES:
            raise ValueError(f"unknown data category: {category}")


def default_path() -> Path:
    configured = (os.environ.get(_PATH_ENV) or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "data" / "privacy_preferences.json"


def _validated(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported privacy preference schema")
    enabled = raw.get("save_enabled")
    retention = raw.get("retention_days")
    if not isinstance(enabled, dict) or not isinstance(retention, dict):
        raise ValueError("save_enabled and retention_days must be objects")
    normalized = {
        "schema_version": 1,
        "save_enabled": {},
        "retention_days": {},
        "external_processing_enabled": raw.get("external_processing_enabled"),
        "external_processing_providers": raw.get("external_processing_providers"),
    }
    for category in DATA_CATEGORIES:
        if not isinstance(enabled.get(category), bool):
            raise ValueError(f"save_enabled.{category} must be boolean")
        days = retention.get(category)
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ValueError(f"retention_days.{category} must be a positive integer")
        max_days = 7 if category == "external_cache" else 3650
        if days > max_days:
            raise ValueError(f"retention_days.{category} exceeds {max_days}")
        normalized["save_enabled"][category] = enabled[category]
        normalized["retention_days"][category] = days
    if not isinstance(normalized["external_processing_enabled"], bool):
        raise ValueError("external_processing_enabled must be boolean")
    providers = normalized["external_processing_providers"]
    if not isinstance(providers, list) or any(not isinstance(p, str) or not p.strip() for p in providers):
        raise ValueError("external_processing_providers must be a list of provider IDs")
    if len(providers) != len(set(providers)):
        raise ValueError("external_processing_providers must be unique")
    if not normalized["external_processing_enabled"] and providers:
        raise ValueError("providers cannot be authorized while external processing is disabled")
    normalized["external_processing_providers"] = list(providers)
    return normalized


def load_preferences(path: Optional[object] = None) -> PrivacyPreferences:
    target = Path(path) if path is not None else default_path()
    if not target.exists():
        return PrivacyPreferences(json.loads(json.dumps(DEFAULT_PREFERENCES)), target)
    try:
        with target.open("r", encoding="utf-8") as stream:
            data = _validated(json.load(stream))
        return PrivacyPreferences(data, target)
    except Exception as exc:
        # Corrupt consent/preferences must not turn into implicit external authorization.
        return PrivacyPreferences(json.loads(json.dumps(DEFAULT_PREFERENCES)), target,
                                  f"privacy preferences invalid; fail-closed defaults applied: {type(exc).__name__}")


def save_preferences(raw: Any, path: Optional[object] = None) -> PrivacyPreferences:
    data = _validated(raw)
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return PrivacyPreferences(data, target)
