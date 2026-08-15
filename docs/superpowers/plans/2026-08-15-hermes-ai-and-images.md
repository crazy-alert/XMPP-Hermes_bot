# Hermes AI and Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Применять XMPP-настройки к Hermes и доставлять изображения `image_generate` в XMPP.

**Architecture:** Runtime-конфигуратор владеет атомарной синхронизацией state с Hermes. Image backend использует самостоятельный API route. Transport загружает файл через XEP-0363 и публикует OOB URL.

**Tech Stack:** Python 3.11, Hermes Agent plugin API, Slixmpp XEP-0363/XEP-0066, PyYAML, pytest.

## Global Constraints

- Token не возвращается, не логируется и не попадает в fixtures.
- API base URL не может быть leaf endpoint.
- Runtime-изменения атомарны и сохраняют несвязанные настройки Hermes.
- Успешная отправка изображения означает реально отправленную stanza.

---

### Task 1: Runtime-конфигурация Hermes

**Files:**
- Create: `hermes-xmpp/xmpp_bridge/hermes_config.py`
- Modify: `hermes-xmpp/xmpp_bridge/admin_state.py`
- Modify: `hermes-xmpp/xmpp_bridge/commands.py`
- Test: `tests/test_hermes_config.py`

**Interfaces:** `HermesRuntimeConfig(home: Path).apply(config: AdminConfig, token: str | None) -> None`; `validate_api_base_url(value: str) -> str`; `AdminConfig.image_model`.

- [ ] **Step 1: Write failing test**

```python
def test_apply_writes_chat_provider_and_secret_env(tmp_path):
    runtime.apply(config_with(model="chat", endpoint="https://api.example/v1"), "secret")
    assert config_yaml["providers"]["xmpp-ai"]["api"] == "https://api.example/v1"
    assert config_yaml["model"]["default"] == "xmpp-ai/chat"
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_hermes_config.py`
Expected: FAIL because `hermes_config` does not exist.

- [ ] **Step 3: Implement minimum atomic configurator**

```python
document["providers"]["xmpp-ai"] = {"api": config.endpoint, "key_env": "HERMES_XMPP_AI_API_KEY", "transport": "openai_chat"}
document["model"]["default"] = f"xmpp-ai/{config.model}"
self._atomic_write_yaml(document)
self._atomic_replace_env_key("HERMES_XMPP_AI_API_KEY", token)
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_hermes_config.py`
Expected: PASS.

### Task 2: Image backend и команды

**Files:**
- Create: `hermes-xmpp/xmpp_image_gen/__init__.py`
- Create: `hermes-xmpp/xmpp_image_gen/plugin.yaml`
- Modify: `hermes-xmpp/xmpp_bridge/commands.py`
- Test: `tests/test_xmpp_image_gen.py`
- Test: `tests/test_commands.py`

**Interfaces:** `XmppImageGenProvider.generate(prompt, aspect_ratio, **kwargs) -> dict`; config `image_gen.provider=xmpp-ai`.

- [ ] **Step 1: Write failing test**

```python
def test_provider_posts_only_to_images_generations_and_saves_b64(monkeypatch):
    result = provider.generate("a lighthouse", "square")
    assert request.url == "https://api.example/v1/images/generations"
    assert Path(result["image"]).is_file()
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_xmpp_image_gen.py tests/test_commands.py -k image`
Expected: FAIL because backend and command do not exist.

- [ ] **Step 3: Implement backend and command**

```python
url = f"{base_url.rstrip('/')}/images/generations"
payload = {"model": selected_model, "prompt": prompt, "size": size}
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_xmpp_image_gen.py tests/test_commands.py -k image`
Expected: PASS.

### Task 3: XMPP media delivery

**Files:**
- Modify: `hermes-xmpp/xmpp_bridge/client.py`
- Modify: `hermes-xmpp/adapter.py`
- Test: `tests/test_client_events.py`
- Test: `tests/test_adapter.py`

**Interfaces:** `HermesXmppClient.send_media(target: str, source: str, caption: str | None) -> list[str]`; adapter recognises strict `MEDIA:<https-url-or-absolute-path>`.

- [ ] **Step 1: Write failing test**

```python
async def test_local_image_is_uploaded_then_sent_as_oob_url(client):
    ids = await client.send_media("user@example.test", "/tmp/image.png", "done")
    assert client.uploaded == "/tmp/image.png"
    assert client.sent_oob_url == "https://upload.example/image.png"
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_client_events.py tests/test_adapter.py -k media`
Expected: FAIL because `send_media` does not exist.

- [ ] **Step 3: Implement XEP-0363 and OOB delivery**

```python
self.register_plugin("xep_0363")
uploaded_url = await self.plugin["xep_0363"].upload_file(path)
message["oob"]["url"] = uploaded_url
message.send()
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_client_events.py tests/test_adapter.py -k media`
Expected: PASS.

### Task 4: Installer, README и review

**Files:**
- Modify: `deploy/install-on-ubuntu.sh`
- Modify: `README.md`
- Test: `tests/test_deploy_assets.py`

- [ ] **Step 1: Write failing test**

```python
def test_installer_copies_xmpp_image_backend():
    assert "xmpp_image_gen" in deploy_script
```

- [ ] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_deploy_assets.py -k image`
Expected: FAIL until assets are wired.

- [ ] **Step 3: Install backend and document separate APIs**

```bash
cp -R "$REPO_DIR/hermes-xmpp/xmpp_image_gen" "$PLUGIN_PARENT/xmpp_image_gen"
```

- [ ] **Step 4: Verify GREEN and scoped review**

Run: `.venv\\Scripts\\python.exe -m pytest -q tests/test_deploy_assets.py -k image`
Expected: PASS.

Run: `bash -n installer.sh && bash -n deploy/install-on-ubuntu.sh`
Expected: exit code 0.
