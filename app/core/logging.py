"""Logging setup with a final-output secret masker."""

from __future__ import annotations

import logging
from logging.config import dictConfig

from app.core.security import mask_text


class SecretMaskingFormatter(logging.Formatter):
    def __init__(
        self,
        *args: object,
        extra_secrets: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._extra_secrets = extra_secrets or []

    def format(self, record: logging.LogRecord) -> str:
        return mask_text(super().format(record), self._extra_secrets)


def configure_logging(
    level: str = "INFO",
    *,
    extra_secrets: list[str] | None = None,
) -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "app.core.logging.SecretMaskingFormatter",
                    "fmt": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    "extra_secrets": extra_secrets or [],
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": level,
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "uvicorn.access": {"level": "WARNING"},
            },
        }
    )
