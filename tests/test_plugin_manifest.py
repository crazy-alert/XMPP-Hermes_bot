import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
import pytest
ROOT = Path(__file__).parents[1]
def load_manifest():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "hermes-xmpp" / "plugin.yaml").read_text(encoding="utf-8"))
def test_manifest_declares_platform_and_environment_contract():
    manifest = load_manifest(); assert manifest["name"] == "xmpp-platform"; assert manifest["kind"] == "platform"
    dependencies = manifest.get("dependencies", manifest.get("requires", [])); assert any("slixmpp>=1.12,<2" in str(dep).lower() for dep in dependencies)
    env = manifest.get("env", manifest.get("environment", {})); assert "XMPP_JID" in env and "XMPP_PASSWORD" in env
    assert "password" in str(env["XMPP_PASSWORD"]).lower() or "secret" in str(env["XMPP_PASSWORD"]).lower()
    for name in ("XMPP_ALLOWED_USERS", "XMPP_STATE_PATH", "XMPP_HOST", "XMPP_PORT", "XMPP_NICK"): assert name in env
def test_models_are_frozen_and_preserve_bare_jids_without_hermes_import():
    sys.modules.pop("hermes", None); sys.path.insert(0, str(ROOT / "hermes-xmpp"))
    try:
        from xmpp_bridge.models import DeliveryTarget, InboundXmppMessage, XmppInvite
        message = InboundXmppMessage("id", "room@example.test", "user@example.test", "Nick", "body", True, "reply")
        assert message.chat_jid == "room@example.test"; assert XmppInvite("room@example.test", "user@example.test", False).room_jid == "room@example.test"; assert DeliveryTarget("user@example.test", False).chat_jid == "user@example.test"
        with pytest.raises(FrozenInstanceError): message.body = "changed"
        assert "hermes" not in sys.modules
    finally: sys.path.pop(0)
def test_adapter_exposes_register_without_importing_slixmpp():
    sys.modules.pop("slixmpp", None); sys.path.insert(0, str(ROOT / "hermes-xmpp"))
    try:
        adapter = __import__("adapter"); assert callable(adapter.register); assert "slixmpp" not in sys.modules
    finally: sys.path.pop(0)
