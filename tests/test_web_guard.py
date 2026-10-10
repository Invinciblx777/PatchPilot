"""Local-only protections of the PatchPilot web app, over real HTTP.

The app runs repository test commands, so a page on another origin, or a
DNS-rebinding page, must not be able to drive it. No AgntID runtime is
involved here.
"""

from __future__ import annotations

import httpx
import pytest

from .web_helpers import SESSION_HEADER, make_settings, serve_web


@pytest.fixture
def web():
    with serve_web(make_settings()) as app:
        yield app


def test_api_rejects_requests_without_the_session_token(web):
    bare = httpx.Client(base_url=web.base_url)
    assert bare.get("/api/config").status_code == 403
    assert bare.get("/api/config", headers={SESSION_HEADER: "not-the-token"}).status_code == 403
    assert bare.post("/api/runs", json={"kind": "probe"}).status_code == 403
    assert web.get("/api/config").status_code == 200


def test_session_token_needs_no_token_but_is_per_process(web):
    first = httpx.get(f"{web.base_url}/api/session")
    assert first.status_code == 200
    assert first.json()["token"] == web.token and len(web.token) >= 32
    with serve_web(make_settings()) as other:
        assert other.token != web.token


@pytest.mark.parametrize("path", ["/api/session", "/api/config", "/", "/security"])
def test_foreign_host_header_is_refused(web, path):
    # A DNS-rebinding page reaches 127.0.0.1 with its own name in Host.
    response = web.get(path, headers={"Host": f"attacker.example:{web.port}"})
    assert response.status_code == 400
    assert web.get(path, headers={"Host": f"localhost:{web.port}"}).status_code != 400


def test_cross_site_requests_are_refused(web):
    evil = web.post("/api/runs", {"kind": "probe", "prompt": "x"}, headers={"Origin": "http://attacker.example"})
    assert evil.status_code == 403
    cross = web.get("/api/config", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    same = web.get("/api/config", headers={"Sec-Fetch-Site": "same-origin", "Origin": web.base_url})
    assert same.status_code == 200


def test_post_needs_a_json_content_type(web):
    # text/plain is a CORS "simple request": it would skip the preflight.
    response = web.http.post("/api/runs", content=b'{"kind": "probe"}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_preflight_is_never_approved(web):
    response = web.http.options(
        "/api/runs",
        headers={"Origin": "http://attacker.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code in (403, 405)
    assert "access-control-allow-origin" not in response.headers


def test_responses_carry_security_headers(web):
    for path in ("/api/config", "/"):
        response = web.get(path)
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert web.get("/api/config").headers["cache-control"] == "no-store"


def test_oversized_request_body_is_refused(web):
    body = {"kind": "patch_check", "repoId": "r1", "base": "main", "patch": "x" * (9 * 1024 * 1024)}
    assert web.post("/api/runs", body).status_code == 413


def test_unknown_api_path_is_a_json_404(web):
    response = web.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


PUBLIC_ORIGIN = "https://ui.example.dev"
ACCESS_KEY = "k" * 32


@pytest.fixture
def public_web():
    with serve_web(make_settings(), public_origins=(PUBLIC_ORIGIN,), access_key=ACCESS_KEY) as app:
        yield app


@pytest.mark.parametrize("path", ["/api/session", "/api/config", "/"])
def test_public_mode_needs_the_access_key_everywhere(public_web, path):
    bare = httpx.Client(base_url=public_web.base_url)
    response = bare.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith('Basic realm="PatchPilot"')
    assert bare.get(path, auth=("patchpilot", "wrong-key-" * 4)).status_code == 401
    assert bare.get(path, headers={"Authorization": "Basic !!!not-base64"}).status_code == 401
    assert bare.get(path, headers={"Authorization": f"Bearer {ACCESS_KEY}"}).status_code == 401
    assert bare.get(path, auth=("anyone", ACCESS_KEY)).status_code != 401


def test_public_mode_still_needs_the_session_token(public_web):
    keyed = httpx.Client(base_url=public_web.base_url, auth=("patchpilot", ACCESS_KEY))
    assert keyed.get("/api/config").status_code == 403
    assert public_web.get("/api/config").status_code == 200


def test_public_mode_accepts_only_its_own_public_origin(public_web):
    proxied = {"Origin": PUBLIC_ORIGIN, "Sec-Fetch-Site": "same-origin"}
    assert public_web.post("/api/runs", {"kind": "nope"}, headers=proxied).status_code == 400  # past the guard
    evil = public_web.post("/api/runs", {"kind": "nope"}, headers={"Origin": "https://attacker.example"})
    assert evil.status_code == 403


def test_public_mode_keeps_the_loopback_host_check(public_web):
    response = public_web.get("/api/config", headers={"Host": "ui.example.dev"})
    assert response.status_code == 400


def test_public_origin_without_an_access_key_is_refused():
    from patchpilot.web.guard import LocalGuard

    with pytest.raises(ValueError):
        LocalGuard(None, port=1, token="t", public_origins=(PUBLIC_ORIGIN,))


def test_default_mode_ignores_basic_credentials(web):
    # Without --public-origin there is no access key and nothing changes.
    assert httpx.get(f"{web.base_url}/api/session").status_code == 200
    assert "www-authenticate" not in web.get("/api/config").headers
