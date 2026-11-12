# -*- coding: utf-8 -*-
"""User-managed local persistence and external-processing preferences."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mylocalai.core.privacy import PrivacyPreferences, load_preferences, save_preferences
from mylocalai.llm import llm_client

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


class PreferencesIn(BaseModel):
    save_enabled: Dict[str, bool]
    retention_days: Dict[str, int]
    external_processing_enabled: bool = False
    external_processing_providers: List[str] = Field(default_factory=list)


def _view(prefs: PrivacyPreferences) -> Dict[str, Any]:
    return prefs.as_dict() | {"warning": prefs.warning}


@router.get("/preferences")
def get_preferences() -> Dict[str, Any]:
    return _view(load_preferences())


@router.put("/preferences")
def put_preferences(body: PreferencesIn) -> Dict[str, Any]:
    try:
        prefs = save_preferences({
            "schema_version": 1,
            "save_enabled": body.save_enabled,
            "retention_days": body.retention_days,
            "external_processing_enabled": body.external_processing_enabled,
            "external_processing_providers": body.external_processing_providers,
        })
    except (TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    llm_client.reset_cache()
    return _view(prefs)
