"""Keep local HTTP model traffic off environment-configured external proxies."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def ollama_http_options(host: str) -> dict[str, bool]:
    """Remote/HTTPS clients keep proxy and CA environment behavior unchanged."""
    try:
        parsed = urlsplit(host if "://" in host else f"http://{host}")
        name = (parsed.hostname or "").lower().rstrip(".")
        loopback = name == "localhost"
        if not loopback:
            try:
                loopback = ip_address(name).is_loopback
            except ValueError:
                pass
        return {"trust_env": not (parsed.scheme == "http" and loopback)}
    except ValueError:
        # URL validation belongs to the SDK/configuration boundary, not this policy.
        return {"trust_env": True}
