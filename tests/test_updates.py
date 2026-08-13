from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.updates import API_URL, HttpResponse, ReleaseChecker, UpdateState


def release(version: str = "1.2.0", *, draft: bool = False, prerelease: bool = False, notes: str = "notes") -> dict:
    tag = f"v{version}"
    base = f"https://github.com/crazy-alert/XMPP-Hermes_bot/releases/download/{tag}/"
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-08-13T00:00:00Z",
        "body": notes,
        "assets": [
            {"name": "update-manifest.json", "size": 300, "browser_download_url": base + "update-manifest.json"},
            {"name": "hermes-xmpp.tar.gz", "size": 1234, "browser_download_url": base + "hermes-xmpp.tar.gz"},
        ],
    }


def manifest(version: str = "1.2.0") -> dict:
    return {
        "repository": "crazy-alert/XMPP-Hermes_bot",
        "version": version,
        "source_commit": "a" * 40,
        "assets": [{"name": "hermes-xmpp.tar.gz", "size": 1234, "sha256": "b" * 64}],
    }


class FakeHttp:
    def __init__(self, releases: list[dict], manifests: dict[str, dict] | None = None, *, etag: str = '"v1"') -> None:
        self.releases = releases
        self.manifests = manifests or {item["tag_name"]: manifest(item["tag_name"].removeprefix("v")) for item in releases}
        self.etag = etag
        self.calls: list[tuple[str, dict[str, str], float, int, bool]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int, allow_redirects: bool) -> HttpResponse:
        self.calls.append((url, headers, timeout, max_bytes, allow_redirects))
        if url == API_URL:
            if headers.get("If-None-Match") == self.etag:
                return HttpResponse(304, {}, b"", final_url=url, redirected=False)
            return HttpResponse(200, {"ETag": self.etag}, json.dumps(self.releases).encode(), final_url=url, redirected=False)
        tag = url.split("/download/", 1)[1].split("/", 1)[0]
        return HttpResponse(200, {}, json.dumps(self.manifests[tag]).encode(), final_url=url, redirected=False)


def test_fixed_api_etag_304_and_deduplicated_state() -> None:
    http = FakeHttp([release()])
    checker = ReleaseChecker(http=http, jitter=lambda: 0.0)
    first = checker.check(UpdateState(current_version="1.0.0"))
    assert http.calls[0][0] == API_URL
    assert http.calls[0][4] is False
    assert set(http.calls[0][1]) <= {"Accept", "If-None-Match"}
    assert first.available is not None and first.notify is True
    notified = first.state.mark_notified()
    second = checker.check(notified)
    assert http.calls[-1][1]["If-None-Match"] == '"v1"'
    assert second.available == first.available and second.notify is False


def test_stable_filter_semver_order_and_prerelease_opt_in() -> None:
    items = [release("1.1.9"), release("1.10.0"), release("2.0.0", draft=True), release("1.11.0", prerelease=True)]
    stable = ReleaseChecker(http=FakeHttp(items), jitter=lambda: 0).check(UpdateState("1.2.0"))
    assert stable.available.version == "1.10.0"
    preview = ReleaseChecker(http=FakeHttp(items), jitter=lambda: 0).check(UpdateState("1.2.0"), allow_prerelease=True)
    assert preview.available.version == "1.11.0"


@pytest.mark.parametrize("version", ["main", "1.2", "01.2.3", "1.2.3+build", "1.2.3-rc1"])
def test_ambiguous_or_non_semver_and_downgrade_are_rejected(version: str) -> None:
    result = ReleaseChecker(http=FakeHttp([release(version)]), jitter=lambda: 0).check(UpdateState("2.0.0"))
    assert result.available is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("repository", "attacker/repo"), ("version", "9.9.9"), ("source_commit", "A" * 40)],
)
def test_manifest_identity_is_strict(field: str, value: str) -> None:
    bad = manifest(); bad[field] = value
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([release()], {"v1.2.0": bad}), jitter=lambda: 0).check(UpdateState("1.0.0"))


@pytest.mark.parametrize("asset", [
    {"name": "../x", "size": 1234, "sha256": "b" * 64},
    {"name": "hermes-xmpp.tar.gz", "size": 999, "sha256": "b" * 64},
    {"name": "hermes-xmpp.tar.gz", "size": 1234, "sha256": "B" * 64},
])
def test_manifest_asset_name_size_and_lowercase_digest_are_strict(asset: dict) -> None:
    bad = manifest(); bad["assets"] = [asset]
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([release()], {"v1.2.0": bad}), jitter=lambda: 0).check(UpdateState("1.0.0"))


def test_notes_are_control_cleaned_and_truncated_and_repr_has_no_headers() -> None:
    http = FakeHttp([release(notes="hello\x00\nworld" + "x" * 2000)])
    result = ReleaseChecker(http=http, jitter=lambda: 0).check(UpdateState("1.0.0"))
    assert "\x00" not in result.available.notes and len(result.available.notes) <= 500
    assert "Authorization" not in repr(result) and "Authorization" not in repr(http.calls)


def test_schema_body_count_timeout_and_backoff_are_bounded() -> None:
    class Failing:
        def __init__(self) -> None: self.calls = 0
        def __call__(self, url, headers, timeout, max_bytes, allow_redirects):
            self.calls += 1
            assert timeout <= 10 and max_bytes <= 1_000_000
            raise TimeoutError("secret-header")
    checker = ReleaseChecker(http=Failing(), jitter=lambda: 0.5)
    state = UpdateState("1.0.0")
    delays = []
    for _ in range(10):
        result = checker.check(state); state = result.state; delays.append(result.retry_after_seconds)
        assert "secret-header" not in repr(result)
    assert delays == sorted(delays) and delays[-1] <= 3600.5

    too_many = FakeHttp([release() for _ in range(31)])
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=too_many, jitter=lambda: 0).check(UpdateState("1.0.0"))


def test_duplicate_bool_and_oversize_strings_are_rejected() -> None:
    duplicate = release()
    duplicate["assets"].append(dict(duplicate["assets"][-1]))
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([duplicate]), jitter=lambda: 0).check(UpdateState("1.0.0"))

    bool_size = manifest()
    bool_size["assets"][0]["size"] = True
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([release()], {"v1.2.0": bool_size}), jitter=lambda: 0).check(UpdateState("1.0.0"))

    long_date = release(); long_date["published_at"] = "x" * 129
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([long_date]), jitter=lambda: 0).check(UpdateState("1.0.0"))

    long_etag = FakeHttp([release()], etag='"' + "x" * 256 + '"')
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=long_etag, jitter=lambda: 0).check(UpdateState("1.0.0"))


@pytest.mark.parametrize("etag", ["plain", 'W/"bad\\tag"', '"bad\r\nheader"', "x" * 129, 123])
def test_invalid_persisted_etag_fails_before_http(etag: object) -> None:
    http = FakeHttp([release()])
    with pytest.raises(ValueError, match="invalid update state"):
        ReleaseChecker(http=http, jitter=lambda: 0).check(UpdateState("1.0.0", etag=etag))  # type: ignore[arg-type]
    assert http.calls == []


@pytest.mark.parametrize("etag", ['"bad\x7f"', 'W/"bad\\tag"', '"bad\r\nheader"'])
def test_invalid_response_etag_is_never_persisted(etag: str) -> None:
    http = FakeHttp([release()], etag=etag)
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=http, jitter=lambda: 0).check(UpdateState("1.0.0"))


def test_redirect_or_changed_final_url_is_rejected_before_parsing() -> None:
    class Redirecting:
        def __call__(self, url, headers, timeout, max_bytes, allow_redirects):
            assert allow_redirects is False
            return HttpResponse(200, {}, b"not json", final_url="https://attacker.invalid/x", redirected=True)
    with pytest.raises(ValueError, match="invalid HTTP destination"):
        ReleaseChecker(http=Redirecting(), jitter=lambda: 0).check(UpdateState("1.0.0"))


@pytest.mark.parametrize("payload", [b"{", b"{}", b"[null]", b"[1]"])
def test_malformed_json_and_schema_types_fail_safely(payload: bytes) -> None:
    class Raw:
        def __call__(self, url, headers, timeout, max_bytes, allow_redirects):
            return HttpResponse(200, {}, payload, final_url=url, redirected=False)
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=Raw(), jitter=lambda: 0).check(UpdateState("1.0.0"))


def test_oversize_body_and_tag_are_rejected_before_expensive_parsing() -> None:
    class Oversize:
        def __call__(self, url, headers, timeout, max_bytes, allow_redirects):
            return HttpResponse(200, {}, b" " * (max_bytes + 1), final_url=url, redirected=False)
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=Oversize(), jitter=lambda: 0).check(UpdateState("1.0.0"))
    long_tag = release(); long_tag["tag_name"] = "v" + "1" * 129
    with pytest.raises(ValueError, match="invalid release metadata"):
        ReleaseChecker(http=FakeHttp([long_tag]), jitter=lambda: 0).check(UpdateState("1.0.0"))


def test_notes_remove_unicode_controls_private_and_unassigned() -> None:
    controls = "\x7f\x85\u202e\ue000\ud800\u0378"
    result = ReleaseChecker(http=FakeHttp([release(notes="ok\n\t" + controls)]), jitter=lambda: 0).check(UpdateState("1.0.0"))
    assert result.available.notes == "ok\n\t"
