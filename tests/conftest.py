import pytest


# every test sees an empty config folder and no server data folder, never the user's own
@pytest.fixture(autouse=True)
def _no_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SYNCRET_DATA", raising=False)
