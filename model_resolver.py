"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Asks OpenRouter which models are available per provider and picks the
best one using a single criterion: newest by creation date.

No keyword filters. No exclude list. OpenRouter decides what's available.

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_FILE = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24

# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------
# Map agent key -> OpenRouter provider prefix.
# The resolver picks the newest paid text model whose id starts with this prefix.
PROVIDERS = {
    "claude": "anthropic/",
    "gpt":    "openai/",
    "gemini": "google/",
    "grok":   "x-ai/",
    "llama":  "meta-llama/",
}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def fetch_models() -> list:
    req = urllib.request.Request(
        MODELS_URL,
        headers={"User-Agent": "AIUNION-model-resolver/6.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode()).get("data", [])


# Model id substrings that disqualify a model — non-chat variants that return
# null content or are not general-purpose chat completions models.
EXCLUDE = [
    ":free", "-image-", "image-2", "-vision",
    "guard", "embed", "audio", "multi-agent",
    "gemma",   # Gemma != Gemini, different model family
]

def pick_best(all_models: list, prefix: str) -> dict | None:
    """Return the newest paid chat-capable model whose id starts with prefix."""
    candidates = []
    for m in all_models:
        mid = m.get("id", "").lower()
        if not mid.startswith(prefix.lower()):
            continue
        # Skip known non-chat variants
        if any(ex in mid for ex in EXCLUDE):
            continue
        # Must support text output but not be an image-generation model
        out_mods = m.get("architecture", {}).get("output_modalities", [])
        if "text" not in out_mods:
            continue
        if out_mods == ["image"]:
            continue
        # Must be a paid model (price > 0)
        price = float(m.get("pricing", {}).get("completion", "0") or "0")
        if price <= 0:
            continue
        candidates.append(m)
    if not candidates:
        return None
    # Newest first
    candidates.sort(key=lambda m: m.get("created", 0) or 0, reverse=True)
    return candidates[0]


def resolve_models(verbose: bool = False) -> dict:
    """
    Returns dict of agent_key -> full OpenRouter model ID.
    Ready to pass directly to any OpenRouter API call.
    """
    cached = load_cache()
    if cached:
        if verbose:
            print(" [using 24h cache]")
            for agent, model in cached.items():
                print(f"  {agent:8s} -> {model}")
        return cached

    all_models = fetch_models()
    resolved = {}

    for agent, prefix in PROVIDERS.items():
        best = pick_best(all_models, prefix)
        if best is None:
            raise RuntimeError(
                f"[model_resolver] No model found for '{agent}' (prefix: '{prefix}'). "
                f"Check that OpenRouter has models for this provider."
            )
        api_id = best["id"]
        resolved[agent] = api_id

        if verbose:
            created = best.get("created", 0) or 0
            date_str = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else "unknown"
            print(f"  {agent:8s} -> {api_id:50s} (released {date_str})")

    save_cache(resolved)
    return resolved


# ---------------------------------------------------------------------------
# Standalone check
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 56 + "\u2557")
    print("\u2551 AIUNION Model Resolver v6 \u2014 Newest per Provider  \u2551")
    print("\u255a" + "\u2550" * 56 + "\u255d")
    print(f" Source: {MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n Resolved {len(models)} agents.")
    print(f" Cache: {CACHE_FILE}")
    print(f" TTL: {CACHE_TTL_HOURS}h\n")
