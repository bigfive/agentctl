"""Minimal JSON HTTP client for the OpenCode server.

Standard library only, and deliberately unforgiving: a non-2xx response or an
unreachable server is an error, never a retry.
"""

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__("HTTP %s from %s: %s" % (status, url, body))
        self.status = status
        self.body = body
        self.url = url


def request(method, url, payload=None, timeout=30):
    """Perform one request and return ``(status, parsed body or raw text)``."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.getcode()
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace"), url)
    except urllib.error.URLError as exc:
        raise HttpError(0, str(exc.reason), url)

    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw
