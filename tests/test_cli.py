from __future__ import annotations

from pathlib import Path

from app.cli import main


def configure(monkeypatch, tmp_path: Path, **env: str) -> None:
    database = tmp_path / "cli.db"
    values = {
        "AUTH_ENABLED": "false",
        "TINVEST_CLIENT": "mock",
        "DATABASE_URL": f"sqlite:///{database}",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "SEED_DEMO_DATA": "true",
        "SCHEDULER_ENABLED": "false",
    }
    values.update(env)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_cli_seed_recommend_backup_status(monkeypatch, tmp_path: Path, capsys) -> None:
    configure(monkeypatch, tmp_path)
    assert main(["seed-demo"]) == 0
    seeded = capsys.readouterr().out
    assert "sync status: OK" in seeded
    assert "demo goal ready" in seeded
    assert main(["recommend"]) == 0
    recommendations = capsys.readouterr().out
    assert "SELL" in recommendations
    assert main(["backup"]) == 0
    backup = capsys.readouterr().out
    assert "backup:" in backup
    assert list((tmp_path / "backups").glob("investpilot-*.db"))
    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert "token_configured" in status
    assert "TINVEST_TOKEN" not in status


def test_cli_refuses_external_bind(monkeypatch, tmp_path: Path, capsys) -> None:
    configure(monkeypatch, tmp_path)
    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert "non-loopback" in capsys.readouterr().err


def test_cli_seed_requires_mock(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path, TINVEST_CLIENT="rest")
    assert main(["seed-demo"]) == 2
