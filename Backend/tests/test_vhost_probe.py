"""Probing a name-based virtual host.

A shared nginx routes on `server_name`. Probed by IP it sees `Host: <ip>`, matches
no vhost, and answers from its DEFAULT server - typically 404. The probe then
reports "unexpected status 404" and emails an outage while the site is healthy,
which is precisely the false "Frontend Staging HTTP DOWN" alert.
"""
import socket
import threading

import config
import healthcheck


def _one_shot_server(response: bytes):
    """A socket that answers one request and records what it received."""
    seen = {}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve():
        conn, _ = sock.accept()
        seen["raw"] = conn.recv(4096).decode("latin1")
        conn.sendall(response)
        conn.close()
        sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], seen


# --- the header actually reaches the server ---------------------------------
def test_probe_sends_the_supplied_host_header():
    port, seen = _one_shot_server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    up, ms, detail, err = healthcheck._http_probe(
        f"http://127.0.0.1:{port}/", [200], 5, "test-cloud-monitoring.kpndomain.com")
    assert up is True and err is None
    assert "Host: test-cloud-monitoring.kpndomain.com" in seen["raw"]


def test_probe_without_a_host_header_sends_the_ip():
    port, seen = _one_shot_server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    healthcheck._http_probe(f"http://127.0.0.1:{port}/", [200], 5)
    assert "Host: 127.0.0.1" in seen["raw"]


def test_404_is_still_down_with_a_host_header():
    """The fix must not paper over a genuinely broken vhost."""
    port, _ = _one_shot_server(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    up, ms, detail, err = healthcheck._http_probe(
        f"http://127.0.0.1:{port}/", [200, 301, 302], 5, "app.example.com")
    assert up is False
    assert err == "unexpected status 404"


# --- which vhost do we ask for ---------------------------------------------
def test_host_header_defaults_to_the_frontend_url_hostname(monkeypatch):
    monkeypatch.delenv("PROBE_FE_HTTP_HOST", raising=False)
    monkeypatch.setattr(config, "FRONTEND_URL", "http://test-cloud-monitoring.kpndomain.com")
    assert config.probe_http_host() == "test-cloud-monitoring.kpndomain.com"


def test_host_header_ignores_scheme_and_port(monkeypatch):
    monkeypatch.delenv("PROBE_FE_HTTP_HOST", raising=False)
    monkeypatch.setattr(config, "FRONTEND_URL", "https://monitor.example.com:8443")
    assert config.probe_http_host() == "monitor.example.com"


def test_host_header_override(monkeypatch):
    monkeypatch.setenv("PROBE_FE_HTTP_HOST", "other.example.com")
    assert config.probe_http_host() == "other.example.com"


def test_empty_override_means_probe_by_ip(monkeypatch):
    monkeypatch.setenv("PROBE_FE_HTTP_HOST", "")
    assert config.probe_http_host() == ""


# --- only the frontend role carries a vhost --------------------------------
def _checks(role):
    return {c["name"]: c for c in config.build_service_checks(
        [{"id": "i-x", "name": "box", "role": role, "host": "10.0.0.9", "group": "G"}])}


def test_frontend_check_carries_a_host_header():
    assert _checks("frontend")["HTTP"]["host_header"]


def test_other_roles_probe_by_ip():
    """The "web" role fronts someone else's site - we do not know its vhost."""
    assert _checks("web")["HTTP"]["host_header"] is None


def test_target_names_the_vhost_that_was_asked_for(monkeypatch):
    monkeypatch.setattr(config, "MOCK_MODE", False)   # exercise the real probe
    port, _ = _one_shot_server(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    check = {"key": "k", "group": "G", "instance_id": "i-x", "instance_name": "FE",
             "name": "HTTP", "type": "http", "host": "127.0.0.1", "port": port,
             "scheme": "http", "path": "/", "expect": [200],
             "host_header": "app.example.com"}
    result = healthcheck.check_one(check)
    assert result["up"] is False
    # "404 at this IP" and "404 for this vhost" are different diagnoses.
    assert "Host: app.example.com" in result["target"]
