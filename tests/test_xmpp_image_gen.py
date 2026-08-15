import base64
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "hermes-xmpp" / "xmpp_image_gen" / "__init__.py"


class ProviderBase:
    pass


def success_response(**payload):
    return {"success": True, **payload}


def error_response(**payload):
    return {"success": False, **payload}


def save_b64_image(data, **_kwargs):
    destination = Path(os.environ["HERMES_HOME"]) / "image.png"
    destination.write_bytes(base64.b64decode(data))
    return destination


agent = types.ModuleType("agent")
image_gen_provider = types.ModuleType("agent.image_gen_provider")
image_gen_provider.ImageGenProvider = ProviderBase
image_gen_provider.DEFAULT_ASPECT_RATIO = "landscape"
image_gen_provider.error_response = error_response
image_gen_provider.resolve_aspect_ratio = lambda value: value
image_gen_provider.save_b64_image = save_b64_image
image_gen_provider.success_response = success_response
sys.modules.update({"agent": agent, "agent.image_gen_provider": image_gen_provider})

spec = importlib.util.spec_from_file_location("xmpp_image_gen", PLUGIN)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_provider_posts_to_images_generations_and_saves_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_XMPP_AI_API_KEY", "secret")
    seen = {}

    def request(url, payload, token):
        seen.update(url=url, payload=payload, token=token)
        return {"data": [{"b64_json": base64.b64encode(b"png").decode("ascii")}]}

    provider = module.XmppImageGenProvider(
        config_loader=lambda: {
            "providers": {"xmpp-ai": {"api": "https://api.example.test/v1", "key_env": "HERMES_XMPP_AI_API_KEY"}},
            "image_gen": {"model": "image-model"},
        },
        request=request,
    )

    result = provider.generate("a lighthouse", "square")

    assert seen == {
        "url": "https://api.example.test/v1/images/generations",
        "payload": {"model": "image-model", "prompt": "a lighthouse", "size": "1024x1024", "response_format": "b64_json"},
        "token": "secret",
    }
    assert result["success"] is True
    assert Path(result["image"]).read_bytes() == b"png"


def test_provider_returns_configuration_error_without_image_model(monkeypatch):
    monkeypatch.delenv("HERMES_XMPP_AI_API_KEY", raising=False)
    provider = module.XmppImageGenProvider(config_loader=lambda: {"providers": {}, "image_gen": {}}, request=lambda *_: None)

    result = provider.generate("a lighthouse")

    assert result["success"] is False
    assert result["error_type"] == "configuration_error"
