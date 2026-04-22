"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Scrapes the OpenRouter rankings page (openrouter.ai/rankings) to find the
#1 most-used model per provider. Rankings are based on real weekly token
usage across millions of OpenRouter users — the best available signal for
"which model should I actually be using right now."

Providers resolved:
  claude  -> anthropic/
  gpt     -> openai/
  gemini  -> google/
  grok    -> x-ai/
  llama   -> meta-llama/

How it works:
  1. Fetch openrouter.ai/rankings as HTML (no auth needed)
  2. Parse all model hrefs in the leaderboard (e.g. /anthropic/claude-sonnet-4.6)
  3. Take the first (highest-ranked) model found per provider prefix
  4. Cache results for 24h to avoid hammering the page

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import re
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

RANKINGS_URL = "https://openrouter.ai/rankings"

# Cache file — avoids re-scraping on every script run
CACHE_FILE = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24

# -- Provider prefixes -------------------------------------------------------
# Maps agent key -> OpenRouter provider prefix.
# First model found in the rankings page with this prefix wins.
PROVIDER_PREFIXES = {
    "claude": "anthropic/",
    "gpt":    "openai/",
    "gemini": "google/",
    "grok":   "x-ai/",
    "llama":  "meta-llama/",
}

# -- Provider prefix stripping -----------------------------------------------
# True  -> strip "provider/" prefix before passing to the native SDK
# False -> keep full "provider/model" path (Together.ai needs it)
STRIP_PREFIX = {
    "claude": True,   # anthropic SDK: "claude-sonnet-4.6"
    "gpt":    True,   # openai SDK:    "gpt-5.4"
    "gemini": True,   # google SDK:    "gemini-3-flash-preview"
    "grok":   True,   # xAI SDK:       "grok-4"
    "llama":  False,  # Together SDK:  keep full "meta-llama/..." path
}

# -- Cache helpers ------------------------------------------------------------
def load_cache():
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours > CACHE_TTL_HOURS:
            return None
        return data["resolved"]
    except Exception:
        return None


def save_cache(resolved: dict):
    try:
        CACHE_FILE.write_text(json.dumps({
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "resolved": resolved,
        }, indent=2))
    except Exception:
        pass  # cache write failure is non-fatal


# -- Scraper -----------------------------------------------------------------
def fetch_rankings_html() -> str:
    """Fetch the OpenRouter rankings page HTML."""
    req = urllib.request.Request(
        RANKINGS_URL,
        headers={"User-Agent": "AIUNION-model-resolver/3.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_rankings(html: str) -> dict:
    """
    Extract the top-ranked model per provider from the rankings HTML.
    Looks for hrefs like /anthropic/claude-sonnet-4.6 in document order
    (which matches ranking order) and takes the first match per provider.
    """
    # Match all model page links: /provider/model-slug
    # Must have two path segments (provider + model), no trailing slash
    pattern = re.compile(r'href="/([w-]+)/([w.-]+)"' )
    resolved = {}

    for match in pattern.finditer(html):
        provider_slug = match.group(1)
        model_slug    = match.group(2)
        full_id       = f"{provider_slug}/{model_slug}"

        for agent, prefix in PROVIDER_PREFIXES.items():
            if agent in resolved:
                continue  # already found this provider
            if full_id.startswith(prefix):
                if STRIP_PREFIX.get(agent, True):
                    resolved[agent] = model_slug
                else:
                    resolved[agent] = full_id

        if len(resolved) == len(PROVIDER_PREFIXES):
            break  # found all providers, stop scanning

    return resolved


# -- Resolver ----------------------------------------------------------------
def resolve_models(verbose: bool = False) -> dict:
    """
    Returns a dict of agent_key -> model_name ready to pass to each native API.

    Example (reflects live rankings, will change as models are released):
        {
            "claude": "claude-sonnet-4.6",
            "gpt":    "gpt-5.4",
            "gemini": "gemini-3-flash-preview",
            "grok":   "grok-4.1-fast",
            "llama":  "meta-llama/llama-4-maverick",
        }
    """
    # Try cache first
    cached = load_cache()
    if cached:
        if verbose:
            print("  [cache hit]")
            for agent, model in cached.items():
                print(f"  {agent:8s} -> {model}")
        return cached

    # Scrape live rankings
    try:
        html = fetch_rankings_html()
        resolved = parse_rankings(html)
    except Exception as e:
        raise RuntimeError(f"[model_resolver] Failed to fetch rankings: {e}") from e

    # Validate all providers were found
    missing = [a for a in PROVIDER_PREFIXES if a not in resolved]
    if missing:
        raise RuntimeError(
            f"[model_resolver] Could not find ranking for: {missing}. "
            f"Rankings page may have changed structure."
        )

    save_cache(resolved)

    if verbose:
        for agent, model in resolved.items():
            print(f"  {agent:8s} -> {model}")

    return resolved


# -- Standalone check --------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  AIUNION Model Resolver v3 \u2014 Rankings-Based      \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"  Source: {RANKINGS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache: {CACHE_FILE}")
    print(f"  TTL:   {CACHE_TTL_HOURS}h\n")
