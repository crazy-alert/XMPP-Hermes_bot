import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.hermes_config import HermesRuntimeConfig, RuntimeConfigError, validate_api_base_url


def configured(*, model="chat-model", endpoint="https://api.example.test/v1"):
    return SimpleNamespace(model=model, endpoint=endpoint, image_model=None)


def test_apply_writes_chat_provider_model_and_secret_without_losing_other_settings(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")
    (home / ".env").write_text("OTHER_KEY=keep\n", encoding="utf-8")

    HermesRuntimeConfig(home).apply(configured(), "secret-token")

    document = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert document["terminal"] == {"backend": "docker"}
    assert document["providers"]["xmpp-ai"] == {
        "api": "https://api.example.test/v1",
        "key_env": "HERMES_XMPP_AI_API_KEY",
        "transport": "openai_chat",
    }
    assert document["model"]["default"] == "xmpp-ai/chat-model"
    assert (home / ".env").read_text(encoding="utf-8") == "OTHER_KEY=keep\nHERMES_XMPP_AI_API_KEY=secret-token\n"


@pytest.mark.parametrize("value", [
    "https://api.example.test/v1/chat/completions",
    "https://api.example.test/v1/images/generations",
    "https://api.example.test/v1/responses",
    "https://api.example.test/v1/messages",
])
def test_api_base_url_rejects_concrete_api_routes(value):
    with pytest.raises(RuntimeConfigError):
        validate_api_base_url(value)


def test_api_base_url_accepts_https_or_loopback_http_and_normalizes_trailing_slash():
    assert validate_api_base_url("https://api.example.test/v1/") == "https://api.example.test/v1"
    assert validate_api_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"


def test_apply_does_not_change_config_when_environment_path_is_unsafe(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    original = "terminal:\n  backend: docker\n"
    config_path.write_text(original, encoding="utf-8")
    (home / ".env").mkdir()

    with pytest.raises(RuntimeConfigError):
        HermesRuntimeConfig(home).apply(configured(), "secret-token")

    assert config_path.read_text(encoding="utf-8") == original
