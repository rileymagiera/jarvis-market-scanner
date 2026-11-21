"""Phone/Codespaces token bootstrap keeps SDK logs outside the repo checkout."""
import os
from pathlib import Path
import sys
from types import ModuleType

import scripts.webull_authorize_local as bootstrap


def test_bootstrap_uses_private_cwd_and_no_market_quote(monkeypatch, tmp_path, capsys):
    original_cwd = tmp_path / "repo"
    original_cwd.mkdir()
    monkeypatch.chdir(original_cwd)
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda _prompt: "dummy-credential")

    class FakeApiClient:
        def __init__(self, key, secret, region, token_check_duration_seconds):
            assert key == "dummy-credential"
            assert secret == "dummy-credential"
            assert region == "us"
            assert token_check_duration_seconds == 300

        def add_endpoint(self, region, endpoint):
            assert (region, endpoint) == ("us", "api.webull.com")

    class FakeDataClient:
        def __init__(self, api):
            token_dir = Path(os.environ["WEBULL_OPENAPI_TOKEN_DIR"])
            assert Path.cwd() == token_dir
            # The SDK logs to CWD; write one to verify cleanup.
            (Path.cwd() / "webull_data_sdk.log").write_text("fake sdk message")
            (token_dir / "token.txt").write_text(
                "placeholderTokenForTesting123\n4102444800000\nNORMAL\n"
            )

    api_module = ModuleType("webull.core.client")
    data_module = ModuleType("webull.data.data_client")
    api_module.ApiClient = FakeApiClient
    data_module.DataClient = FakeDataClient
    monkeypatch.setitem(sys.modules, "webull.core.client", api_module)
    monkeypatch.setitem(sys.modules, "webull.data.data_client", data_module)
    private_paths = []

    def fake_save(path):
        assert path.name == "token.txt"
        assert path.parent == Path.cwd()
        private_paths.append(path.parent)

    monkeypatch.setattr(bootstrap, "save_approved_token_with_gh", fake_save)

    assert bootstrap.main() == 0
    assert Path.cwd() == original_cwd
    assert len(private_paths) == 1
    assert not private_paths[0].exists()
    assert not (original_cwd / "webull_data_sdk.log").exists()
    assert "OpenAPI" in capsys.readouterr().out
