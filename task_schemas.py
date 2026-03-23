"""Shared Pydantic models for API task payloads (personality traits, etc.)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PersonalityTraitItem(BaseModel):
    trait_id: int
    label: str

    model_config = ConfigDict(from_attributes=True)
