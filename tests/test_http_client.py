"""Unit tests for HttpTermixClient, mocking urllib at the transport level."""

import io
import json
from urllib import error

import pytest

from termix_aws_sync.http_client import HttpTermixClient


class FakeHTTPResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def test_list_hosts_sends_bearer_auth_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        return FakeHTTPResponse(json.dumps([{"id": 1}]).encode())

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    result = client.list_hosts()

    assert result == [{"id": 1}]
    assert captured["url"] == "http://termix.example.com/host/db/host"
    assert captured["method"] == "GET"
    assert captured["auth"] == "Bearer tmx_secret"


def test_list_hosts_rejects_non_list_response(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return FakeHTTPResponse(json.dumps({"unexpected": "shape"}).encode())

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    with pytest.raises(RuntimeError, match="unexpected JSON"):
        client.list_hosts()


def test_create_host_posts_json_body(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["content_type"] = req.get_header("Content-type")
        return FakeHTTPResponse(json.dumps({"id": 42}).encode())

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com/", "tmx_secret")
    result = client.create_host({"name": "web-1", "authType": "credential"})

    assert result == {"id": 42}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://termix.example.com/host/db/host"
    assert captured["body"] == {"name": "web-1", "authType": "credential"}
    assert captured["content_type"] == "application/json"


def test_update_host_puts_to_id_path(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return FakeHTTPResponse(json.dumps({"id": 7}).encode())

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    client.update_host(7, {"name": "web-1"})

    assert captured["method"] == "PUT"
    assert captured["url"] == "http://termix.example.com/host/db/host/7"


def test_delete_host_sends_delete(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return FakeHTTPResponse(b"")

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    client.delete_host(7)

    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://termix.example.com/host/db/host/7"


def test_http_error_raises_runtimeerror_with_status_and_body(monkeypatch):
    def fake_urlopen(req, timeout=None):
        body = io.BytesIO(b'{"error":"Failed to save SSH data"}')
        raise error.HTTPError(req.full_url, 500, "Internal Server Error", {}, body)

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    with pytest.raises(RuntimeError, match="500"):
        client.create_host({"name": "x"})


def test_connection_error_raises_runtimeerror(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise error.URLError("Connection refused")

    monkeypatch.setattr("termix_aws_sync.http_client.request.urlopen", fake_urlopen)

    client = HttpTermixClient("http://termix.example.com", "tmx_secret")
    with pytest.raises(RuntimeError, match="could not reach termix API"):
        client.list_hosts()
