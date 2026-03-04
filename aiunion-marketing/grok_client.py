"""
grok_client.py
Grok (xAI) API wrapper for AIUNION marketing agent.
- Reads API key from environment only, never from arguments or files
- Never logs key material or prompt content
- Fails closed if key is missing
"""

import os
import json
import logging
from typing import Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
MODEL = "grok-3"
MAX_TOKENS = 512
MAX_PROMPT_CHARS = 4000  # input size limit (rule #5)

SYSTEM_PROMPT = """You are the official announcement voice for AIUNION — an autonomous 
AI treasury where AI agents from Anthropic, OpenAI, Google, xAI, Meta, and Amazon 
collectively govern a shared Bitcoin wallet. Your tone is direct, technically credible, 
and compelling to crypto-native and AI-curious audiences.

Rules:
- Posts must be under 280 characters
- No hashtag spam — maximum 2 relevant hashtags per post
- Never make up treasury balances, vote counts, or bounty amounts — only use data provided
- Never use clickbait or misleading framing
- Always sound like an official project update, not an ad
- Label automated posts with [AUTO] at the end if instructed"""


def _get_api_key() -> str:
    """Fail closed if XAI_API_KEY is missing."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise EnvironmentError(json.dumps({
            "error_code": "MISSING_SECRET",
            "error": "XAI_API_KEY environment variable not set",
            "details": "Set XAI_API_KEY in GitHub Actions secrets or local .env"
        }))
    return key


def generate_post(prompt: str, label_automated: bool = True) -> str:
    """
    Generate a tweet-length post using Grok.
    Returns the post text string.
    Raises on API error.
    """
    # Input size validation (rule #5)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(json.dumps({
            "error_code": "INPUT_TOO_LARGE",
            "error": "Prompt exceeds maximum allowed length",
            "details": f"Max {MAX_PROMPT_CHARS} chars, got {len(prompt)}"
        }))

    api_key = _get_api_key()

    user_content = prompt
    if label_automated:
        user_content += "\n\nEnd the post with [AUTO]"

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        XAI_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            # Enforce 280 char limit
            if len(text) > 280:
                text = text[:277] + "..."
            logger.info("Post generated successfully (length=%d)", len(text))
            return text

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # Rule #8: don't log the key, only log status code
        if e.code == 429:
            raise RuntimeError(json.dumps({
                "error_code": "RATE_LIMITED",
                "error": "xAI API rate limit hit",
                "details": "Retry after 60 seconds",
                "retry_after": 60
            }))
        raise RuntimeError(json.dumps({
            "error_code": "XAI_API_ERROR",
            "error": f"xAI API returned HTTP {e.code}",
            "details": body[:200]  # truncate, never log full response
        }))

    except urllib.error.URLError as e:
        raise RuntimeError(json.dumps({
            "error_code": "NETWORK_ERROR",
            "error": "Failed to reach xAI API",
            "details": str(e.reason)
        }))
