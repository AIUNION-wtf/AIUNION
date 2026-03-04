"""
twitter_client.py
X (Twitter) API v2 posting client for AIUNION marketing agent.
- OAuth 1.0a (required for posting on free/pay-per-use tier)
- Write-only: posts to public timeline only, no DM, no read
- Fails closed if any required secret is missing
- Returns machine-readable errors
- Includes Retry-After on 429 responses (rule #7)
"""

import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import urllib.parse
import logging

logger = logging.getLogger(__name__)

POST_URL = "https://api.twitter.com/2/tweets"
MAX_TWEET_CHARS = 280


def _get_credentials() -> dict:
    """Load all 4 OAuth credentials from environment. Fail closed if any missing."""
    keys = {
        "api_key":              os.environ.get("TWITTER_API_KEY", "").strip(),
        "api_secret":           os.environ.get("TWITTER_API_SECRET", "").strip(),
        "access_token":         os.environ.get("TWITTER_ACCESS_TOKEN", "").strip(),
        "access_token_secret":  os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "").strip(),
    }
    missing = [k for k, v in keys.items() if not v]
    if missing:
        raise EnvironmentError(json.dumps({
            "error_code": "MISSING_SECRETS",
            "error": "One or more Twitter credentials not set",
            "details": f"Missing env vars: {', '.join('TWITTER_' + k.upper() for k in missing)}"
        }))
    return keys


def _percent_encode(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def _build_oauth_header(method: str, url: str, creds: dict, params: dict = None) -> str:
    """Build OAuth 1.0a Authorization header."""
    nonce = base64.b64encode(os.urandom(32)).decode("utf-8").rstrip("=").replace("+", "").replace("/", "")
    timestamp = str(int(time.time()))

    oauth_params = {
        "oauth_consumer_key":     creds["api_key"],
        "oauth_nonce":            nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        timestamp,
        "oauth_token":            creds["access_token"],
        "oauth_version":          "1.0",
    }

    # Combine all params for signature base
    all_params = {**oauth_params, **(params or {})}
    sorted_params = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(all_params.items())
    )

    base_string = "&".join([
        _percent_encode(method.upper()),
        _percent_encode(url),
        _percent_encode(sorted_params)
    ])

    signing_key = f"{_percent_encode(creds['api_secret'])}&{_percent_encode(creds['access_token_secret'])}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")

    oauth_params["oauth_signature"] = signature

    header = "OAuth " + ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return header


def post_tweet(text: str) -> dict:
    """
    Post a tweet to the public timeline.
    Returns dict with tweet_id on success.
    Raises RuntimeError with machine-readable JSON on failure.
    """
    # Input validation (rule #5)
    if not text or not text.strip():
        raise ValueError(json.dumps({
            "error_code": "EMPTY_CONTENT",
            "error": "Tweet text cannot be empty",
            "details": "Provide non-empty text"
        }))
    if len(text) > MAX_TWEET_CHARS:
        raise ValueError(json.dumps({
            "error_code": "CONTENT_TOO_LONG",
            "error": f"Tweet exceeds {MAX_TWEET_CHARS} characters",
            "details": f"Got {len(text)} characters"
        }))

    creds = _get_credentials()
    payload = json.dumps({"text": text}).encode("utf-8")
    auth_header = _build_oauth_header("POST", POST_URL, creds)

    req = urllib.request.Request(
        POST_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tweet_id = data.get("data", {}).get("id", "unknown")
            logger.info("Tweet posted successfully (id=%s)", tweet_id)
            return {"success": True, "tweet_id": tweet_id}

    except urllib.error.HTTPError as e:
        # Rule #8: never log credentials or full response body
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", 900))
            raise RuntimeError(json.dumps({
                "error_code": "RATE_LIMITED",
                "error": "X API rate limit hit",
                "details": f"Retry after {retry_after} seconds",
                "retry_after": retry_after  # rule #7
            }))
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(json.dumps({
            "error_code": "TWITTER_API_ERROR",
            "error": f"X API returned HTTP {e.code}",
            "details": body[:200]
        }))

    except urllib.error.URLError as e:
        raise RuntimeError(json.dumps({
            "error_code": "NETWORK_ERROR",
            "error": "Failed to reach X API",
            "details": str(e.reason)
        }))
