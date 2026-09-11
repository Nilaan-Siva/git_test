"""Validated YAML loading for the config files in this package."""
from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def load_yaml_config(path: Path, model: type[T]) -> T:
    """Load and validate a YAML file against a pydantic model.

    Raises FileNotFoundError if the file is missing, or ValueError (chaining the original
    ValidationError) with the file path in the message if the contents don't validate --
    a bad or missing config fails loudly at startup rather than silently taking defaults.
    """
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid config in {path}:\n{exc}") from exc
