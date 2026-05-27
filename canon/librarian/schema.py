"""
Pydantic v2 model for validating raw LLM classifier output.

classify.py adds classified_at and classifier_model after validation,
so those fields are intentionally absent here.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class TaxonomyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book: Literal["I", "II", "III", "IV", "V", "VI"]
    shelf: Literal["doctrine", "instruments"]
    right_claimed: str
    artifact_type: Literal[
        "statute", "dataset", "library", "protocol",
        "schema", "sdk", "tool", "brief", "spec",
    ]
    language_or_jurisdiction: str
    depends_on: List[str] = []
    doctrine_pairing: Optional[str] = None
