"""SQLite online backups with retention."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.clock import Clock, SystemClock
from app.core.config import Settings


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    removed: tuple[Path, ...]


class BackupService:
    def __init__(self, settings: Settings, *, clock: Clock | None = None) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()

    def create(self, *, now: datetime | None = None) -> BackupResult:
        source = self._settings.sqlite_path
        if source is None:
            raise BackupError("backup is available only for file-based SQLite databases")
        if not source.exists():
            raise BackupError(f"database file does not exist: {source}")
        stamp = (now or self._clock.now()).strftime("%Y%m%d-%H%M%S")
        target_dir = self._settings.backup_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"investpilot-{stamp}.db"
        if target.exists():
            raise BackupError(f"backup already exists: {target}")
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        removed = self._prune()
        return BackupResult(
            path=target,
            size_bytes=target.stat().st_size,
            removed=tuple(removed),
        )

    def _prune(self) -> list[Path]:
        backups = sorted(
            self._settings.backup_dir.glob("investpilot-*.db"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
        removed: list[Path] = []
        for path in backups[self._settings.backup_keep :]:
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
        return removed
