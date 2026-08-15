"""OpenAI-compatible image backend sharing the XMPP-managed AI account."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)


_SIZES = {"landscape": "1536x1024", "square": "1024x1024", "portrait": "1024x1536"}


def _load_config() -> dict:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    return config if isinstance(config, dict) else {}


def _post_json(url: str, payload: dict, token: str) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("image generation request failed") from error
    if not isinstance(decoded, dict):
        raise RuntimeError("image generation response is invalid")
    return decoded


class XmppImageGenProvider(ImageGenProvider):
    """Use the XMPP owner-configured OpenAI-compatible image API."""

    def __init__(self, *, config_loader=_load_config, request=_post_json) -> None:
        self._config_loader = config_loader
        self._request = request

    @property
    def name(self) -> str:
        return "xmpp-ai"

    @property
    def display_name(self) -> str:
        return "XMPP AI"

    def is_available(self) -> bool:
        try:
            config = self._config_loader()
            provider = config.get("providers", {}).get("xmpp-ai", {})
            key_env = provider.get("key_env", "HERMES_XMPP_AI_API_KEY")
            return bool(config.get("image_gen", {}).get("model") and provider.get("api") and os.getenv(key_env))
        except Exception:
            return False

    def generate(self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, **_kwargs: Any) -> dict:
        ratio = resolve_aspect_ratio(aspect_ratio)
        if not isinstance(prompt, str) or not (prompt := prompt.strip()):
            return error_response(error="Image prompt is required", error_type="invalid_input", provider=self.name)
        try:
            config = self._config_loader()
            provider = config.get("providers", {}).get("xmpp-ai", {})
            image = config.get("image_gen", {})
            base_url = provider.get("api")
            model = image.get("model")
            token = os.getenv(provider.get("key_env", "HERMES_XMPP_AI_API_KEY"))
            parsed = urlparse(base_url if isinstance(base_url, str) else "")
            if not model or not token or parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Image generation is not configured")
            response = self._request(
                f"{base_url.rstrip('/')}/images/generations",
                {"model": model, "prompt": prompt, "size": _SIZES[ratio], "response_format": "b64_json"},
                token,
            )
            item = response.get("data", [None])[0]
            if not isinstance(item, dict):
                raise RuntimeError("image generation response is invalid")
            if isinstance(item.get("b64_json"), str):
                image_value = str(save_b64_image(item["b64_json"], prefix="xmpp-ai", extension="png"))
            elif isinstance(item.get("url"), str) and item["url"].startswith("https://"):
                image_value = item["url"]
            else:
                raise RuntimeError("image generation response has no image")
            return success_response(image=image_value, model=model, prompt=prompt, aspect_ratio=ratio, provider=self.name)
        except ValueError as error:
            return error_response(error=str(error), error_type="configuration_error", provider=self.name, prompt=prompt, aspect_ratio=ratio)
        except Exception:
            return error_response(error="Image generation failed", error_type="provider_error", provider=self.name, prompt=prompt, aspect_ratio=ratio)


def register(ctx) -> None:
    ctx.register_image_gen_provider(XmppImageGenProvider())
