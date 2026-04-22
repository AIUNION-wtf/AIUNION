"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Uses two data sources from OpenRouter — both free, no auth required:

  1. openrouter.ai/rankings  (HTML scrape)
     Weekly token-usage rankings across millions of users.
     Best signal for "which model are people actually using right now."
     Used for: claude, gpt, gemini, grok

  2. openrouter.ai/api/v1/models  (JSON API)
     Full model metadata including canonical slugs.
     Used to: resolve ranking hrefs -> real API ids, and as fallback for llama
     (Llama models rarely appear in top rankings — too niche for this leaderboard)

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
MODELS_URL   = "https://openrouter.ai/api/v1/models"

# Cache file
CACHE_FILE     = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24

# -- Provider config ---------------------------------------------------------
# Agents that use rankings as source of truth (appear in top-10 reliably)
# Maps agent key -> provider slug prefix in the rankings href
RANKINGS_PROVIDERS = {
    "claude": "anthropic/",
    "gpt":    "openai/",
    "gemini": "google/",
    "grok":   "x-ai/",
}

# Agents resolved from /api/v1/models instead (not reliably in rankings)
# Maps agent key -> provider prefix, pick newest non-specialty text model
MODELS_API_PROVIDERS = {
    "llama": "meta-llama/",
}

# Llama: keep full "meta-llama/..." path for Together.ai
# All others: strip "provider/" prefix for native SDKs
STRIP_PREFIX = {
    "claude": True,
    "gpt":    True,
    "gemini": True,
    "grok":   True,
    "llama":  False,
}

# Substrings that identify non-chat Llama variants to skip
LLAMA_EXCLUDE = ["guard", "vision", "embed", ":free"]

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
        pass


# -- HTTP helper -------------------------------------------------------------
def _get(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "AIUNION-model-resolver/3.1"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


# -- Rankings scraper --------------------------------------------------------
def resolve_from_rankings(canonical_slug_map: dict) -> dict:
    """
    Scrape openrouter.ai/rankings HTML, find first model href per provider,
    then map the canonical slug back to the real OpenRouter API model id.
    
    canonical_slug_map: dict of canonical_slug -> api_id from /api/v1/models
    """
    html = _get(RANKINGS_URL)
    # Rankings hrefs look like: /anthropic/claude-4.6-sonnet-20260217
    pattern = re.compile(r'href="/([w-]+)/([w.-]+)"' )
    resolved = {}

    for match in pattern.finditer(html):
        provider_slug = match.group(1)
        model_slug    = match.group(2)
        canonical     = f"{provider_slug}/{model_slug}"

        for agent, prefix in RANKINGS_PROVIDERS.items():
            if agent in resolved:
                continue
            if not canonical.startswith(prefix):
                continue
            # Map canonical slug -> real API id
            api_id = canonical_slug_map.get(canonical)
            if not api_id:
                continue  # skip if we cannot resolve to a known API id
            model_name = api_id.split("/", 1)[-1] if STRIP_PREFIX.get(agent, True) else api_id
            resolved[agent] = model_name

        if len(resolved) == len(RANKINGS_PROVIDERS):
            break

    return resolved


# -- Models API fallback -----------------------------------------------------
def resolve_from_models_api(all_models: list) -> dict:
    """
    For providers not in rankings (llama), pick the newest text-only
    non-specialty model from /api/v1/models.
    """
    resolved = {}
    for agent, prefix in MODELS_API_PROVIDERS.items():
        candidates = []
        for m in all_models:
            mid = m.get("id", "")
            if not mid.startswith(prefix):
                continue
            if any(x in mid for x in LLAMA_EXCLUDE):
                continue
            out_mods = m.get("architecture", {}).get("output_modalities", [])
            if "text" not in out_mods or "image" in out_mods:
                continue
            try:
                price = float(m.get("pricing", {}).get("completion", "0"))
            except (ValueError, TypeError):
                price = 0.0
            if price <= 0:
                continue
            created = m.get("created", 0) or 0
            candidates.append((created, m))
        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0][1]
            api_id = best["id"]
            model_name = api_id if not STRIP_PREFIX.get(agent, True) else api_id.split("/", 1)[-1]
            resolved[agent] = model_name
    return resolved


# -- Main resolver -----------------------------------------------------------
def resolve_models(verbose: bool = False) -> dict:
    """
    Returns a dict of agent_key -> model_name ready to pass to each native API.
    """
    cached = load_cache()
    if cached:
        if verbose:
            print("  [using 24h cache]")
            for agent, model in cached.items():
                print(f"  {agent:8s} -> {model}")
        return cached

    # Fetch model metadata (needed for canonical slug resolution + llama fallback)
    raw = json.loads(_get(MODELS_URL))
    all_models = raw.get("data", [])
    # Build canonical_slug -> api_id map
    canonical_slug_map = {
        m["canonical_slug"]: m["id"]
        for m in all_models
        if m.get("canonical_slug")
    }

    # Resolve rankings-based providers
    resolved = resolve_from_rankings(canonical_slug_map)

    # Check for any rankings providers that failed
    missing_rankings = [a for a in RANKINGS_PROVIDERS if a not in resolved]
    if missing_rankings:
        print(f"[model_resolver] WARNING: Could not resolve from rankings: {missing_rankings}")

    # Resolve models-api-based providers (llama etc)
    resolved.update(resolve_from_models_api(all_models))

    # Final check
    all_agents = list(RANKINGS_PROVIDERS) + list(MODELS_API_PROVIDERS)
    missing = [a for a in all_agents if a not in resolved]
    if missing:
        raise RuntimeError(f"[model_resolver] Could not resolve: {missing}")

    save_cache(resolved)

    if verbose:
        for agent, model in resolved.items():
            print(f"  {agent:8s} -> {model}")

    return resolved


# -- Standalone check --------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  AIUNION Model Resolver v3.1 \u2014 Rankings-Based    \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"  Rankings: {RANKINGS_URL}")
    print(f"  Models:   {MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache: {CACHE_FILE}")
    print(f"  TTL:   {CACHE_TTL_HOURS}h\n")
