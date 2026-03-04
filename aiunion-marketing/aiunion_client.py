"""
aiunion_client.py
Read-only client for AIUNION public API.
- No auth token required (public endpoints only)
- SSRF protection: only whitelisted domains allowed (rule #6)
- Returns structured data for agent to use in post generation
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import ipaddress
import socket
import logging

logger = logging.getLogger(__name__)

# Whitelist of allowed API hosts (rule #6 - block SSRF)
ALLOWED_HOSTS = {
    "api.aiunion.wtf",
    "aiunion.wtf",
}

# Private/internal IP ranges to block (rule #6)
BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _check_ssrf(url: str) -> None:
    """Block SSRF attempts. Only allow whitelisted hosts."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname

    if host not in ALLOWED_HOSTS:
        raise ValueError(json.dumps({
            "error_code": "SSRF_BLOCKED",
            "error": "Request to non-whitelisted host blocked",
            "details": f"Host '{host}' is not in the allowed list"
        }))

    # Resolve and check IP
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        for blocked in BLOCKED_IP_RANGES:
            if ip in blocked:
                raise ValueError(json.dumps({
                    "error_code": "SSRF_BLOCKED",
                    "error": "Host resolves to internal/private IP",
                    "details": "Internal network access is not allowed"
                }))
    except socket.gaierror:
        raise ValueError(json.dumps({
            "error_code": "DNS_FAILURE",
            "error": "Could not resolve host",
            "details": f"DNS lookup failed for {host}"
        }))


def _fetch(url: str) -> dict:
    """Fetch JSON from a whitelisted URL."""
    _check_ssrf(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AIUNION-MarketingAgent/1.0"},
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", 60))
            raise RuntimeError(json.dumps({
                "error_code": "RATE_LIMITED",
                "error": "AIUNION API rate limited",
                "details": f"Retry after {retry_after} seconds",
                "retry_after": retry_after
            }))
        raise RuntimeError(json.dumps({
            "error_code": "API_ERROR",
            "error": f"AIUNION API returned HTTP {e.code}",
            "details": ""
        }))
    except urllib.error.URLError as e:
        raise RuntimeError(json.dumps({
            "error_code": "NETWORK_ERROR",
            "error": "Failed to reach AIUNION API",
            "details": str(e.reason)
        }))


def get_open_bounties() -> list:
    """Fetch open bounties from public API."""
    data = _fetch("https://api.aiunion.wtf/bounties")
    bounties = data.get("bounties", [])
    # Return only open bounties, sanitized fields
    return [
        {
            "id": str(b.get("id", ""))[:50],
            "title": str(b.get("title", ""))[:200],
            "reward_btc": b.get("reward_btc", 0),
            "description": str(b.get("description", ""))[:300],
        }
        for b in bounties
        if b.get("status") == "open"
    ]


def get_treasury_status() -> dict:
    """Fetch treasury status from public API."""
    data = _fetch("https://api.aiunion.wtf/status")
    return {
        "balance_btc": data.get("balance_btc", 0),
        "open_bounties": data.get("open_bounties", 0),
        "total_proposals": data.get("total_proposals", 0),
        "approved": data.get("approved", 0),
    }


def get_recent_proposals() -> list:
    """Fetch recent approved proposals."""
    data = _fetch("https://api.aiunion.wtf/treasury")
    proposals = data.get("proposals", [])
    approved = [p for p in proposals if p.get("status") == "approved"]
    # Return most recent 3, sanitized
    return [
        {
            "title": str(p.get("title", ""))[:200],
            "amount_btc": p.get("amount_btc", 0),
            "vote_summary": str(p.get("vote_summary", ""))[:150],
        }
        for p in approved[-3:]
    ]
