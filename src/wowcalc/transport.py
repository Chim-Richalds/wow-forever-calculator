"""Single HTTP access point (stdlib only).

Every network call in the package goes through `get`. A hosted deployment can
replace this module (proxy, caching, rate limiting) without touching callers.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "wow-forever-calculator/0.1"


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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:  # must precede OSError (subclass)
            exc.close()
            raise
        except OSError as exc:
            last = exc
    assert last is not None
    raise last
