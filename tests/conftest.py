import pytest


@pytest.fixture(autouse=True)
def _open_admin(monkeypatch):
    """Tests drive operator endpoints directly; the real .env may carry an ADMIN_TOKEN."""
    from app import console_app
    monkeypatch.setattr(console_app, "ADMIN_TOKEN", "")
    monkeypatch.setattr(console_app, "LIVE", False)
