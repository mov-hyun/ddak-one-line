from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    web_dir: Path = ROOT / "web"
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
    computer_model: str = os.getenv("DDAK_COMPUTER_MODEL", "gpt-5.6")
    browser_mode: str = os.getenv("DDAK_BROWSER_MODE", "live")
    browser_headless: bool = os.getenv("DDAK_BROWSER_HEADLESS", "true").lower() == "true"
    live_step_delay_ms: int = int(os.getenv("DDAK_LIVE_STEP_DELAY_MS", "650"))
    max_spend_usd: float = float(os.getenv("DDAK_MAX_SPEND_USD", "35"))
    host: str = os.getenv("DDAK_HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", os.getenv("DDAK_PORT", "8000")))

    @property
    def has_openai_key(self) -> bool:
        value = os.getenv("OPENAI_API_KEY", "")
        return value.startswith("sk-") and len(value) > 20


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
