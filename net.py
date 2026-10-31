"""
Verified-TLS HTTP layer for Iran Market AI data collection.

- Domestic hosts (TSETMC, Codal) are reached through the physical LAN interface so a
  running VPN/TUN adapter does not block them.
- TLS certificates are always verified (certifi bundle).
- Failures raise FetchError with the host/path; nothing is swallowed here.
"""

import http.client
import json
import socket
import ssl
import time
import urllib.request
from typing import Any, Optional

import certifi

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


class FetchError(RuntimeError):
    pass


def detect_physical_ipv4(probe_host: str = "cdn.tsetmc.com") -> Optional[str]:
    """Returns the first LAN IPv4 (192.168.x.x / 10.x / 172.16-31.x) that can reach probe_host."""
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return None

    def is_lan(ip: str) -> bool:
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 192 and b == 168 or a == 10 or a == 172 and 16 <= b <= 31

    candidates = sorted((ip for ip in ips if is_lan(ip)), key=lambda ip: not ip.startswith("192.168."))
    for ip in candidates:
        try:
            conn = http.client.HTTPSConnection(probe_host, source_address=(ip, 0), timeout=4, context=SSL_CTX)
            conn.request("GET", "/api/StaticData/GetTime", headers=HEADERS)
            conn.getresponse().read()
            conn.close()
            return ip
        except OSError:
            continue
    return None


class HttpClient:
    def __init__(self, source_ip: Optional[str] = None, retries: int = 3, backoff_s: float = 1.5):
        self.source_ip = source_ip if source_ip is not None else detect_physical_ipv4()
        self.retries = retries
        self.backoff_s = backoff_s
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=SSL_CTX)
        )

    def _get_direct(self, host: str, path: str, timeout: float, body: Optional[Any] = None) -> str:
        kwargs = {"source_address": (self.source_ip, 0)} if self.source_ip else {}
        conn = http.client.HTTPSConnection(host, timeout=timeout, context=SSL_CTX, **kwargs)
        try:
            if body is None:
                conn.request("GET", path, headers=HEADERS)
            else:
                headers = {**HEADERS, "Content-Type": "application/json"}
                conn.request("POST", path, body=json.dumps(body), headers=headers)
            res = conn.getresponse()
            body = res.read().decode("utf-8", errors="replace")
            if res.status != 200:
                raise FetchError(f"HTTP {res.status} for https://{host}{path}")
            return body
        finally:
            conn.close()

    def _get_url(self, url: str, timeout: float) -> str:
        req = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")

    def get_text(
        self, host: str, path: str, timeout: float = 15, direct: bool = True, body: Optional[Any] = None
    ) -> str:
        """GET (or POST with a JSON body) with retries. POST is only supported on the direct route."""
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                if direct or body is not None:
                    return self._get_direct(host, path, timeout, body)
                return self._get_url(f"https://{host}{path}", timeout)
            except (OSError, FetchError, http.client.HTTPException) as e:
                last_err = e
                if isinstance(e, FetchError) and " 404 " in str(e):
                    break
                time.sleep(self.backoff_s * (attempt + 1))
        raise FetchError(f"{host}{path}: {last_err}")

    def get_json(
        self, host: str, path: str, timeout: float = 15, direct: bool = True, body: Optional[Any] = None
    ) -> Any:
        text = self.get_text(host, path, timeout=timeout, direct=direct, body=body)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"Invalid JSON from {host}{path}: {e}; body starts {text[:120]!r}")
