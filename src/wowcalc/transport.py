"""Single HTTP access point (stdlib only).

Every network call in the package goes through `get`. A hosted deployment can
replace this module (proxy, caching, rate limiting) without touching callers.

IPv6 check: some networks publish IPv6 but never answer on it, so each connect
would hang for the full timeout on every IPv6 address before trying IPv4. The
first connection to a host therefore tries IPv6 with a short timeout. If that
works, IPv6 stays in use for that host; if it fails and IPv4 works, the host is
IPv4-only for the rest of the process.
"""

from __future__ import annotations

import http.client
import logging
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "wow-forever-calculator/0.1"
IPV6_PROBE_TIMEOUT = 5.0

log = logging.getLogger(__name__)

# host -> True (IPv6 answers) / False (use IPv4 only). Missing = not yet checked.
_ipv6_ok: dict[str, bool] = {}
_ipv6_lock = threading.Lock()


def reset_ipv6_cache() -> None:
    """Forget every per-host IPv6 decision (for tests)."""
    with _ipv6_lock:
        _ipv6_ok.clear()


def _connect_any(infos, timeout, source_address) -> socket.socket:
    """Connect to the first address in `infos` that answers (as socket.create_connection)."""
    last: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last if last is not None else OSError("no addresses to connect to")


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *_, **__):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    v6 = [i for i in infos if i[0] == socket.AF_INET6]
    v4 = [i for i in infos if i[0] == socket.AF_INET]
    if not (v6 and v4):  # only one family: nothing to choose
        return _connect_any(infos, timeout, source_address)

    with _ipv6_lock:
        state = _ipv6_ok.get(host)
    if state is True:
        return _connect_any(v6 + v4, timeout, source_address)
    if state is False:
        return _connect_any(v4, timeout, source_address)

    # First connection to this host: check IPv6 with a short timeout.
    probe = IPV6_PROBE_TIMEOUT
    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT and timeout is not None:
        probe = min(probe, timeout)
    try:
        sock = _connect_any(v6, probe, source_address)
    except OSError:
        sock = _connect_any(v4, timeout, source_address)  # raises if offline: decide nothing
        with _ipv6_lock:
            _ipv6_ok[host] = False
        log.warning("IPv6 to %s did not answer; using IPv4 for it from now on.", host)
        return sock
    sock.settimeout(socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout)
    with _ipv6_lock:
        _ipv6_ok[host] = True
    return sock


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


_opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)


@dataclass(frozen=True)
class Response:
    body: bytes
    content_type: str = ""


def get(url: str, timeout: float = 20.0, retries: int = 1) -> Response:
    """GET `url`. Retries on connection errors and timeouts, not on HTTP errors.

    An HTTP error status means the server answered, so retrying will not change
    it; the HTTPError is closed (releasing its response body) and re-raised with
    its code and headers still readable.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: OSError | None = None
    for _ in range(retries + 1):
        try:
            with _opener.open(req, timeout=timeout) as resp:
                return Response(resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:  # must precede OSError (subclass)
            exc.close()
            raise
        except OSError as exc:
            last = exc
    assert last is not None
    raise last
