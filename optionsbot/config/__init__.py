"""Configuration layer.

- settings.py: process/environment settings (secrets, ports, paths) loaded from .env.
- schema.py:   pydantic schemas for the human-edited YAML config files below.
- loader.py:   validated YAML loading, so a typo in risk.yaml fails loudly at startup.
- *.yaml:      the actual, committed, human-edited config. Never put secrets in these files.
"""
