"""Stage 4.3: deployment files stay consistent (no network, no database)."""

import json
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent


def _requirements_in() -> list[str]:
    lines = (BACKEND / "requirements.in").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def test_pyproject_dependencies_match_requirements_in():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == _requirements_in()


def test_requirements_txt_pins_every_direct_dependency():
    pinned = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    for req in _requirements_in():
        name, version = req.split("==")
        name = name.split("[")[0].lower()
        assert f"{name}=={version}" in pinned, req


def test_python_version_is_supported_by_vercel():
    assert (BACKEND / ".python-version").read_text(encoding="utf-8").strip() in {"3.12", "3.13", "3.14"}


def test_vercel_config_routes_everything_to_the_api():
    config = json.loads((BACKEND / "vercel.json").read_text(encoding="utf-8"))
    fn = config["functions"]["api/index.py"]
    assert fn["maxDuration"] <= 300  # Hobby plan maximum
    assert "config" in fn["includeFiles"] and "app" in fn["includeFiles"]
    assert config["rewrites"] == [{"source": "/(.*)", "destination": "/api/index"}]


def test_no_env_files_are_committed():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored
    assert not (BACKEND / ".env").exists() or ".env" in ignored


@pytest.mark.parametrize(
    "headers, expected",
    [
        ([(b"x-real-ip", b"203.0.113.7")], "203.0.113.7"),
        ([(b"x-forwarded-for", b"198.51.100.2, 10.0.0.1")], "198.51.100.2"),
        ([(b"host", b"example")], None),
    ],
)
def test_vercel_entrypoint_reads_client_ip(headers, expected):
    from api.index import _vercel_client_ip

    assert _vercel_client_ip(headers) == expected


def test_vercel_entrypoint_passes_real_ip_to_the_app(monkeypatch):
    import asyncio

    import api.index as entry

    seen = {}

    async def fake_app(scope, receive, send):
        seen.update(scope)

    monkeypatch.setattr(entry, "_api", fake_app)
    asyncio.run(
        entry.app({"type": "http", "headers": [(b"x-real-ip", b"203.0.113.9")], "client": ("10.0.0.1", 1)}, None, None)
    )
    assert seen["client"] == ("203.0.113.9", 0)
