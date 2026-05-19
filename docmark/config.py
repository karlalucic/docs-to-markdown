"""Settings for DocMark. API key in OS keychain; non-secrets in a JSON file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

from platformdirs import user_cache_dir, user_config_dir

from docmark.utils import secrets

SETTINGS_FILENAME = "settings.json"


def _config_path() -> Path:
    return Path(user_config_dir("docmark")) / SETTINGS_FILENAME


def cache_dir() -> Path:
    path = Path(user_cache_dir("docmark"))
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_TECHNICAL_PATTERNS: List[str] = [
    r"\d+\.?\d*\s*mm\b",
    r"\d+\.?\d*\s*μm\b",
    r"\d+\.?\d*\s*°C\b",
    r"\d+\.?\d*\s*MPa\b",
    r"\d+\.?\d*\s*kPa\b",
    r"\d+\.?\d*\s*%",
    r"layer\s+height",
    r"nozzle\s+temp",
]


@dataclass
class Settings:
    """Everything the app and pipeline need.

    The OpenAI API key is stored separately in the OS keychain — never in this
    object on disk. `openai_api_key` is populated only at runtime via `load()`.
    """

    model: str = "gpt-4.1-mini"
    dpi: int = 200
    detail: str = "high"  # "low" | "high" | "auto"
    force_vision: bool = False

    # PDF routing thresholds (carried over from the original pipeline)
    pdf_confidence_threshold: float = 0.92
    table_confidence_threshold: float = 0.88
    picture_confidence_threshold: float = 0.85
    picture_description_min_length: int = 20
    technical_param_threshold: int = 5
    min_content_threshold: int = 50
    technical_param_patterns: List[str] = field(
        default_factory=lambda: list(DEFAULT_TECHNICAL_PATTERNS)
    )

    # Office vision toggles (off by default; vision for Office is a cost trap)
    office_vision_pptx_enabled: bool = False
    docx_image_description_enabled: bool = False

    # Caching of per-page Docling/vision results across runs
    cache_enabled: bool = True

    # Runtime only
    openai_api_key: str | None = None

    @classmethod
    def load(cls) -> "Settings":
        path = _config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}
        data.pop("openai_api_key", None)
        instance = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        instance.openai_api_key = secrets.get_openai_key()
        return instance

    def save(self) -> None:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if k != "openai_api_key"}
        path.write_text(json.dumps(payload, indent=2))

    def to_public_dict(self) -> dict:
        """Settings as seen by the UI — API key replaced with a boolean."""
        d = {k: v for k, v in asdict(self).items() if k != "openai_api_key"}
        d["has_api_key"] = bool(self.openai_api_key)
        return d
