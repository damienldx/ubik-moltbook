"""MoltbookClient unit tests. Mock the HTTP layer so we never hit the
real Moltbook API during CI."""

from __future__ import annotations

import io
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from moltbook_client import Credentials, MoltbookClient, MoltbookError


class _FakeResp(io.BytesIO):
    """Stand-in for the context-manager urlopen returns."""

    def __init__(self, payload: bytes):
        super().__init__(payload)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(api_key: str = "moltbook_test_key") -> MoltbookClient:
    return MoltbookClient(Credentials(api_key=api_key, agent_name="Fidele"))


def test_register_does_not_send_authorization(monkeypatch):
    sent: dict = {}

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.headers)
        return _FakeResp(b'{"agent": {"api_key": "moltbook_xxx"}}')

    monkeypatch.setattr("moltbook_client.urllib.request.urlopen", fake_urlopen)
    c = _client()
    result = c.register("Fidele", "UBIK lead")
    assert result["agent"]["api_key"] == "moltbook_xxx"
    assert "Authorization" not in sent["headers"]


def test_create_post_attaches_bearer_token(monkeypatch):
    sent: dict = {}

    def fake_urlopen(req, timeout):
        sent["headers"] = dict(req.headers)
        return _FakeResp(b'{"id": "post_abc"}')

    monkeypatch.setattr("moltbook_client.urllib.request.urlopen", fake_urlopen)
    c = _client()
    c.create_post(submolt="general", title="hi")
    # Note: urllib lower-cases header keys when they go through Request.add_header.
    auth = sent["headers"].get("Authorization") or sent["headers"].get("authorization")
    assert auth == "Bearer moltbook_test_key"


def test_image_post_type_refused():
    c = _client()
    with pytest.raises(MoltbookError, match="not supported"):
        c.create_post(submolt="general", title="hi", type="image")


def test_authed_call_without_creds_raises():
    c = MoltbookClient(None)
    with pytest.raises(MoltbookError, match="credentials required"):
        c.get_me()


def test_credentials_save_uses_0600_mode(tmp_path):
    target = tmp_path / "creds.json"
    Credentials("moltbook_xxx", "Fidele").save(str(target))
    mode = oct(target.stat().st_mode)[-3:]
    assert mode == "600"
    loaded = Credentials.load(str(target))
    assert loaded.api_key == "moltbook_xxx"
    assert loaded.agent_name == "Fidele"


def test_get_feed_passes_query_params(monkeypatch):
    sent: dict = {}

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        return _FakeResp(b'{"items": []}')

    monkeypatch.setattr("moltbook_client.urllib.request.urlopen", fake_urlopen)
    c = _client()
    c.get_feed(sort="new", limit=10, submolt="general", cursor="abc")
    assert "sort=new" in sent["url"]
    assert "limit=10" in sent["url"]
    assert "submolt=general" in sent["url"]
    assert "cursor=abc" in sent["url"]


def test_request_refuses_non_moltbook_host(monkeypatch):
    # Simulate a tampered base URL — the URL-host defense-in-depth check
    # should fire before we ever call urlopen.
    monkeypatch.setattr("moltbook_client.MOLTBOOK_BASE", "https://attacker.example/api/v1")
    c = _client()
    with pytest.raises(MoltbookError, match="refusing to send api_key"):
        c.get_me()
