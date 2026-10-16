"""Configuration file + environment; secrets never appear in repr or public status."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    model: str = ""
    api_key: SecretStr = SecretStr("")
    timezone: str = "Asia/Shanghai"
    database: str = "data/calendar.sqlite3"
    timeout_seconds: float = Field(default=30, ge=0.1, le=120)
    turn_timeout_seconds: float = Field(default=90, ge=0.1, le=300)
    max_iterations: int = Field(default=8, ge=1, le=16)

    @field_validator("endpoint")
    @classmethod
    def normalize_endpoint(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("endpoint 必须是无凭据/查询参数的 HTTP(S) URL")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("非本地 endpoint 必须使用 HTTPS")
        value = value.rstrip("/")
        return value if value.endswith("/chat/completions") else value + "/chat/completions"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @classmethod
    def load(cls, path=None, env=None, dotenv_path=".env"):
        environment = {
            **(dotenv_values(dotenv_path) if dotenv_path else {}),
            **(os.environ if env is None else env),
        }
        path = path or environment.get("APP_CONFIG")
        data = json.loads(Path(path).read_text()) if path else {}
        if data.get("api_key", "").startswith("${"):
            data["api_key"] = environment.get(data["api_key"][2:-1], "")
        names = dict(
            LLM_ENDPOINT="endpoint",
            LLM_MODEL="model",
            LLM_API_KEY="api_key",
            APP_TIMEZONE="timezone",
            APP_DATABASE="database",
        )
        for variable, key in names.items():
            if variable in environment:
                data[key] = environment[variable] or ""
        return cls.model_validate(data)
