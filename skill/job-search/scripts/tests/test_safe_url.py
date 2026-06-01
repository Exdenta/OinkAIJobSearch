"""Tests for the SSRF guard (safe_url.is_safe_url / safe_request)."""
import socket

import pytest
import requests

import safe_url


# ---------- is_safe_url: literal IPs (no DNS) ----------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://127.0.0.1:8000/",                      # loopback (own backend)
    "http://[::1]/",                               # v6 loopback
    "http://10.0.0.5/",                            # private
    "http://192.168.1.1/",                         # private
    "http://172.16.0.1/",                          # private
    "http://0.0.0.0/",                             # unspecified
    "http://[fd00::1]/",                           # v6 unique-local
    "https://[::ffff:127.0.0.1]/",                 # IPv4-mapped loopback
])
def test_blocks_internal_literal_ips(url):
    ok, reason = safe_url.is_safe_url(url)
    assert not ok, f"{url} should be blocked, got reason={reason!r}"
    assert reason.startswith("blocked_ip")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/",
    "ftp://example.com/",
    "data:text/plain,hi",
    "",
    "not-a-url",
])
def test_blocks_bad_scheme(url):
    assert not safe_url.is_safe_url(url)[0]


@pytest.mark.parametrize("url", ["https://8.8.8.8/", "http://93.184.216.34/jobs/123"])
def test_allows_public_literal_ips(url):
    ok, reason = safe_url.is_safe_url(url)
    assert ok, reason


# ---------- is_safe_url: hostname resolution ----------

def test_blocks_host_resolving_to_internal(monkeypatch):
    monkeypatch.setattr(safe_url.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    ok, reason = safe_url.is_safe_url("http://evil.example/")
    assert not ok and reason.startswith("blocked_ip")


def test_allows_host_resolving_to_public(monkeypatch):
    monkeypatch.setattr(safe_url.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert safe_url.is_safe_url("https://jobs.example.com/x")[0]


def test_blocks_if_any_resolved_ip_internal(monkeypatch):
    # Host resolves to one public + one internal IP — must block (no partial trust).
    monkeypatch.setattr(safe_url.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("10.1.2.3", 0)),
    ])
    assert not safe_url.is_safe_url("http://rebind.example/")[0]


def test_dns_failure_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(safe_url.socket, "getaddrinfo", boom)
    ok, reason = safe_url.is_safe_url("http://nonexistent.invalid/")
    assert not ok and reason.startswith("dns_error")


# ---------- safe_request: redirect revalidation ----------

class _FakeResp:
    def __init__(self, status, location=None, url=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_ssrf_blocked_is_a_requests_exception():
    assert issubclass(safe_url.SSRFBlocked, requests.RequestException)


def test_blocks_internal_initial_url_without_fetching(monkeypatch):
    def fake_request(*a, **k):
        raise AssertionError("must NOT issue a request for a blocked URL")
    monkeypatch.setattr(safe_url.requests, "request", fake_request)
    with pytest.raises(safe_url.SSRFBlocked):
        safe_url.safe_request("HEAD", "http://169.254.169.254/", timeout=5)


def test_blocks_redirect_to_internal_and_never_fetches_it(monkeypatch):
    fetched = []

    def fake_request(method, url, **kw):
        fetched.append(url)
        if url == "http://93.184.216.34/":
            return _FakeResp(302, location="http://169.254.169.254/latest/meta-data/")
        raise AssertionError("internal redirect target must not be fetched")

    monkeypatch.setattr(safe_url.requests, "request", fake_request)
    with pytest.raises(safe_url.SSRFBlocked):
        safe_url.safe_request("GET", "http://93.184.216.34/", timeout=5)
    assert fetched == ["http://93.184.216.34/"]  # only the public first hop ran


def test_follows_public_redirect_to_final_response(monkeypatch):
    def fake_request(method, url, **kw):
        if url == "http://93.184.216.34/a":
            return _FakeResp(302, location="http://8.8.8.8/b")
        return _FakeResp(200, url=url)

    monkeypatch.setattr(safe_url.requests, "request", fake_request)
    resp = safe_url.safe_request("GET", "http://93.184.216.34/a", timeout=5)
    assert resp.status_code == 200 and resp.url == "http://8.8.8.8/b"


def test_returns_non_redirect_directly(monkeypatch):
    monkeypatch.setattr(safe_url.requests, "request",
                        lambda method, url, **kw: _FakeResp(404, url=url))
    resp = safe_url.safe_request("HEAD", "https://8.8.8.8/gone", timeout=5)
    assert resp.status_code == 404


def test_redirect_budget_caps_at_max(monkeypatch):
    # Endless public→public redirect loop must terminate (no infinite fetch).
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return _FakeResp(302, location="http://8.8.8.8/next", url=url)

    monkeypatch.setattr(safe_url.requests, "request", fake_request)
    resp = safe_url.safe_request("GET", "http://8.8.8.8/start", timeout=5, max_redirects=3)
    assert resp.status_code == 302
    assert calls["n"] == 4  # initial + 3 redirects, then budget spent
