"""
model_resolver.py  —  AIUNION Live Model Resolver
==================================================
Fetches the current model list from OpenRouter (free, no auth, all providers)
and resolves the best available model for each agent.

Drop this file in C:\\Users\\dusti\\Desktop\\AIUNION\\
Both coordinator.py and bracket.py import it — zero manual updates ever.

Usage:
    from model_resolver import resolve_models

    models = resolve_models()
    # returns e.g.:
    # {
    #   "claude":  "claude-opus-4.6",
    #   "gpt":     "gpt-5",
    #   "gemini":  "gemini-3.1-pro-preview",
    #   "grok":    "grok-4",
    #   "llama":   "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
    # }

    # Claude/GPT/Gemini/Grok get the slug only (prefix stripped).
    # Llama keeps the full "meta-llama/..." path for Together.ai.

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Cache file — avoids hammering OpenRouter on every script run
CACHE_FILE = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24


# ── Preference lists ──────────────────────────────────────────────────────────
# First match wins. Add new models at the top as they release.
# Use OpenRouter's id format exactly: "provider/model-name"
# NOTE: OpenRouter uses dots for version numbers (claude-opus-4.6, not claude-opus-4-6)

PREFERENCES = {
    "claude": [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-opus-4.5",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-3.7-sonnet",
        "anthropic/claude-3.5-sonnet",
    ],
    "gpt": [
        "openai/gpt-5",
        "openai/gpt-4.1",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/gpt-4-turbo",
    ],
    "gemini": [
        "google/gemini-3.1-pro-preview",
        "google/gemini-3-pro-preview",
        "google/gemini-2.5-pro",
        "google/gemini-2.5-flash",
        "google/gemini-2.0-flash-001",
    ],
    "grok": [
        "x-ai/grok-4.20-beta",
        "x-ai/grok-4",
        "x-ai/grok-3",
        "x-ai/grok-3-beta",
    ],
    "llama": [
        "meta-llama/llama-3.3-70b-instruct",
        "meta-llama/llama-3.1-70b-instruct",
    ],
}


# ── Provider prefix stripping ─────────────────────────────────────────────────
# True  → strip "provider/" prefix before passing to the native SDK
# False → keep the full "provider/model" path
#
# Claude/GPT/Gemini/Grok native SDKs want just the slug.
# Together.ai wants the full "meta-llama/..." path, but with its OWN casing
# (e.g. "meta-llama/Llama-4-Maverick"), so we use TOGETHER_MAP below instead.

STRIP_PREFIX = {
    "claude": True,   # anthropic SDK:  "claude-opus-4.6"
    "gpt":    True,   # openai SDK:     "gpt-5"
    "gemini": True,   # google SDK:     "gemini-3.1-pro-preview"
    "grok":   True,   # xAI/openai SDK: "grok-4"
    "llama":  False,  # Together SDK:   keep full path, mapped below
}


# ── Together.ai model name map ────────────────────────────────────────────────
# OpenRouter uses lowercase slugs; Together.ai uses its own casing.
# Map OpenRouter id → Together.ai model string.
# Add new Llama models here as they appear on Together.

TOGETHER_MAP = {
    "meta-llama/llama-3.3-70b-instruct": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "meta-llama/llama-3.1-70b-instruct": "meta-llama/Llama-3.1-70B-Instruct-Turbo",
}


# ── Cache helpers ────────────────────────────────h─────────────────────────────

def load_cache():
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours > CACHE_TTL_HOURS:
            return None
        return data["model_ids"]
    except Exception:
        return None


def save_cache(model_ids: list):
    try:
        CACHE_FILE.write_text(json.dumps({
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "model_ids": model_ids,
        }, indent=2))
    except Exception:
        pass  # cache write failure is non-fatal


# ── Fetch from OpenRouter ─────────────────────────────────────────────────────

def fetch_openrouter_models() -> list:
    """
    Fetch live model list from OpenRouter. Returns list of model id strings.
    Falls back to cache if fetch fails, falls back to hardcoded defaults if
    both fail.
    """
    cached = load_cache()

    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={"User-Agent": "AIUNION-model-resolver/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        model_ids = [m["id"] for m in raw.get("data", []) if "id" in m]
        if model_ids:
            save_cache(model_ids)
            return model_ids
    except Exception as e:
        print(f"[model_resolver] OpenRouter fetch failed: {e}")

    if cached:
        print("[model_resolver] Using cached model list.")
        return cached

    # Last resort — return preference lists flattened so matching still works
    print("[model_resolver] WARNING: Using hardcoded fallback model list.")
    return [m for prefs in PREFERENCES.values() for m in prefs]


# ── Resolver ──────────────────────────────────────────────────────────────────

def resolve_models(verbose: bool = False) -> dict:
    """
    Returns a dict of agent_key → model_name ready to pass to each native API.

    Example:
        {
            "claude": "claude-opus-4.6",
            "gpt":    "gpt-5",
            "gemini": "gemini-3.1-pro-preview",
            "grok":   "grok-4",
            "llama":  "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        }
    """
    available = fetch_openrouter_models()
    available_lower = [m.lower() for m in available]

    resolved = {}
    for agent, prefs in PREFERENCES.items():
        matched = None
        for preferred in prefs:
            if preferred.lower() in available_lower:
                idx = available_lower.index(preferred.lower())
                matched = available[idx]  # use OpenRouter's original casing
                break

        if matched is None:
            matched = prefs[0]
            print(f"[model_resolver] WARNING: No live match for '{agent}', "
                  f"falling back to {matched}")

        if STRIP_PREFIX.get(agent, True):
            # Strip "provider/" prefix for native SDKs
            model_name = matched.split("/", 1)[-1]
        else:
            # Llama: translate OpenRouter id → Together.ai model string
            model_name = TOGETHER_MAP.get(matched.lower(), matched)

        resolved[agent] = model_name
        if verbose:
            print(f"  {agent:8s} → {model_name}")

    return resolved


# ── Standalone check ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════╗")
    print("║   AIUNION Model Resolver — Live Check               ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  Fetching from: {OPENROUTER_MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache: {CACHE_FILE}")
    print(f"  TTL:   {CACHE_TTL_HOURS}h\n")
