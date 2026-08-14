from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace


API_URL = "https://api.github.com/repos/crazy-alert/XMPP-Hermes_bot/releases"
REPOSITORY = "crazy-alert/XMPP-Hermes_bot"
MAX_BODY_BYTES = 1_000_000
MAX_RELEASES = 30
MAX_NOTES = 500
TIMEOUT_SECONDS = 10.0
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x5b\x5d-\x7e]{1,120}"\Z')


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str
    redirected: bool


@dataclass(frozen=True)
class VerifiedAsset:
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class VerifiedRelease:
    version: str
    source_commit: str
    manifest_url: str
    assets: tuple[VerifiedAsset, ...]
    notes: str


@dataclass(frozen=True)
class UpdateState:
    current_version: str
    available: VerifiedRelease | None = None
    notified_version: str | None = None
    etag: str | None = None
    failures: int = 0

    def mark_notified(self) -> UpdateState:
        return replace(self, notified_version=self.available.version if self.available else self.notified_version)


@dataclass(frozen=True)
class CheckResult:
    state: UpdateState
    available: VerifiedRelease | None
    notify: bool
    retry_after_seconds: float | None = None


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        return None
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid release metadata")
    return value


def _json(response: HttpResponse) -> object:
    if response.status != 200 or len(response.body) > MAX_BODY_BYTES:
        raise ValueError("invalid release metadata")
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid release metadata") from None


def _notes(value: object) -> str:
    if not isinstance(value, str):
        return ""
    clean = "".join(
        character
        for character in value
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
    return clean[:MAX_NOTES]


class ReleaseChecker:
    def __init__(
        self,
        *,
        http: Callable[[str, dict[str, str], float, int, bool], HttpResponse],
        jitter: Callable[[], float],
    ) -> None:
        self._http = http
        self._jitter = jitter

    def _get(self, url: str, headers: dict[str, str] | None = None) -> HttpResponse:
        response = self._http(url, headers or {}, TIMEOUT_SECONDS, MAX_BODY_BYTES, False)
        if response.redirected or response.final_url != url:
            raise ValueError("invalid HTTP destination")
        return response

    def check(self, state: UpdateState, *, allow_prerelease: bool = False) -> CheckResult:
        current = _version(state.current_version)
        if current is None:
            raise ValueError("invalid current version")
        if state.etag is not None and (not isinstance(state.etag, str) or not _ETAG.fullmatch(state.etag)):
            raise ValueError("invalid update state")
        headers = {"Accept": "application/vnd.github+json"}
        if state.etag:
            headers["If-None-Match"] = state.etag
        try:
            response = self._get(API_URL, headers)
            if response.status == 304:
                stable = replace(state, failures=0)
                return CheckResult(stable, stable.available, False)
            raw = _json(response)
            if not isinstance(raw, list) or len(raw) > MAX_RELEASES:
                raise ValueError("invalid release metadata")
            candidates: list[tuple[tuple[int, int, int], dict]] = []
            for item_raw in raw:
                item = _object(item_raw)
                published_at = item.get("published_at")
                if not isinstance(published_at, str) or len(published_at) > 128:
                    raise ValueError("invalid release metadata")
                if item.get("draft") is not False:
                    continue
                if item.get("prerelease") is True and not allow_prerelease:
                    continue
                if item.get("prerelease") not in (True, False):
                    continue
                tag = item.get("tag_name")
                if not isinstance(tag, str) or len(tag) > 128:
                    raise ValueError("invalid release metadata")
                parsed = _version(tag[1:]) if isinstance(tag, str) and tag.startswith("v") else None
                if parsed is not None and parsed > current:
                    candidates.append((parsed, item))
            available = self._verify(max(candidates, key=lambda pair: pair[0])[1]) if candidates else None
            etag = response.headers.get("ETag")
            if etag is not None and (not isinstance(etag, str) or not _ETAG.fullmatch(etag)):
                raise ValueError("invalid release metadata")
            updated = replace(state, available=available, etag=etag if isinstance(etag, str) else None, failures=0)
            notify = available is not None and available.version != state.notified_version
            return CheckResult(updated, available, notify)
        except (TimeoutError, OSError):
            failures = min(state.failures + 1, 16)
            delay = min(60.0 * (2 ** (failures - 1)), 3600.0) + min(max(self._jitter(), 0.0), 1.0)
            failed = replace(state, failures=failures)
            return CheckResult(failed, state.available, False, delay)

    def _verify(self, item: dict) -> VerifiedRelease:
        tag = item["tag_name"]
        version = tag[1:]
        assets_raw = item.get("assets")
        if not isinstance(assets_raw, list) or len(assets_raw) > 20:
            raise ValueError("invalid release metadata")
        release_assets: dict[str, dict] = {}
        for raw in assets_raw:
            asset = _object(raw)
            name, size, url = asset.get("name"), asset.get("size"), asset.get("browser_download_url")
            if not isinstance(name, str) or not _ASSET_NAME.fullmatch(name) or type(size) is not int or size < 0 or not isinstance(url, str) or len(url) > 500 or name in release_assets:
                raise ValueError("invalid release metadata")
            release_assets[name] = asset
        manifest_asset = release_assets.get("update-manifest.json")
        if manifest_asset is None:
            raise ValueError("invalid release metadata")
        manifest_url = manifest_asset["browser_download_url"]
        expected_prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
        if manifest_url != expected_prefix + "update-manifest.json":
            raise ValueError("invalid release metadata")
        manifest = _object(_json(self._get(manifest_url)))
        if set(manifest) != {"repository", "version", "source_commit", "assets"}:
            raise ValueError("invalid release metadata")
        if manifest["repository"] != REPOSITORY or manifest["version"] != version or not isinstance(manifest["source_commit"], str) or not _HEX40.fullmatch(manifest["source_commit"]):
            raise ValueError("invalid release metadata")
        listed = manifest["assets"]
        if not isinstance(listed, list) or not listed or len(listed) > 19:
            raise ValueError("invalid release metadata")
        verified: list[VerifiedAsset] = []
        for raw in listed:
            entry = _object(raw)
            if set(entry) != {"name", "size", "sha256"}:
                raise ValueError("invalid release metadata")
            name, size, digest = entry["name"], entry["size"], entry["sha256"]
            release_asset = release_assets.get(name) if isinstance(name, str) else None
            if not isinstance(name, str) or not _ASSET_NAME.fullmatch(name) or name == "update-manifest.json" or type(size) is not int or size <= 0 or not isinstance(digest, str) or not _SHA256.fullmatch(digest) or release_asset is None or release_asset["size"] != size or release_asset["browser_download_url"] != expected_prefix + name or any(asset.name == name for asset in verified):
                raise ValueError("invalid release metadata")
            verified.append(VerifiedAsset(name, size, digest, release_asset["browser_download_url"]))
        return VerifiedRelease(version, manifest["source_commit"], manifest_url, tuple(verified), _notes(item.get("body")))
