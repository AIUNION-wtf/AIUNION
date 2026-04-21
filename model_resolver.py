"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Queries OpenRouter for all available models and automatically selects the most
expensive (highest capability) text model for each provider. No hardcoded model
names — fully autonomous, updates itself every 24 hours from the live API.

Providers resolved:
  claude  -> anthropic/
  gpt     -> openai/
  gemini  -> google/gemini
  grok    -> x-ai/
  llama   -> meta-llama/

Selection logic (per provider):
  1. Fetch live model list from OpenRouter (free, no auth)
  2. Filter to text-only output models (exclude image/audio output)
  3. Exclude free-tier (:free suffix) and specialty variants (-fast, -customtools)
  4. Exclude reasoning-only models (o1, o3, o4 series for GPT)
  5. Sort by creation date descending — newest model = most capable
  6. Take the top result

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

# -- Provider prefixes -------------------------------------------------------
# Maps agent key -> OpenRouter provider prefix to filter by
PROVIDER_PREFIXES = {
    "claude": "anthropic/",
    "gpt":    "openai/",
    "gemini": "google/gemini",
    "grok":   "x-ai/",
    "llama":  "meta-llama/",
}

# -- Provider prefix stripping ------------------------------------------------
# True  -> strip "provider/" prefix before passing to the native SDK
# False -> keep full "provider/model" path (Together.ai needs it)
STRIP_PREFIX = {
    "claude": True,   # anthropic SDK: "claude-opus-4.7"
    "gpt":    True,   # openai SDK:    "gpt-5"
    "gemini": True,   # google SDK:    "gemini-3.1-pro-preview"
    "grok":   True,   # xAI SDK:       "grok-4"
    "llama":  False,  # Together SDK:  keep full "meta-llama/..." path
}

# -- Specialty model exclusions -----------------------------------------------
# Substrings that identify non-standard-chat model variants to skip.
# Kept intentionally small — creation date + output modality filters handle the rest.
EXCLUDE_SUBSTRINGS = [
    "-fast",          # speed-optimized cache variants with inflated pricing
    "-customtools",   # provider-specific tool-calling variants
    "-multi-agent",   # orchestration variants
]

# -- Cache helpers -------------------------------------------------------------
def load_cache():
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
        if age_hours > CACHE_TTL_HOURS:
            return None
        return data["models"]
    except Exception:
        return None


def save_cache(models: list):
    try:
        CACHE_FILE.write_text(json.dumps({
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "models": models,
        }, indent=2))
    except Exception:
        pass  # cache write failure is non-fatal


# -- Fetch from OpenRouter ----------------------------------------------------
def fetch_openrouter_models() -> list:
    """
    Fetch full model metadata from OpenRouter.
    Returns list of model dicts (id, pricing, architecture, etc).
    Falls back to cache on failure.
    """
    cached = load_cache()
    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={"User-Agent": "AIUNION-model-resolver/2.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
            models = raw.get("data", [])
            if models:
                save_cache(models)
            return models
    except Exception as e:
        print(f"[model_resolver] OpenRouter fetch failed: {e}")
        if cached:
            print("[model_resolver] Using cached model list.")
            return cached
        raise RuntimeError("[model_resolver] FATAL: No live data and no cache available.") from e


# -- Model selector -----------------------------------------------------------
def _is_reasoning_model(model_id: str) -> bool:
    """Exclude reasoning-only models (o1, o3, o4 series) — not standard chat."""
    slug = model_id.split("/", 1)[-1]
    return slug[:2] in ("o1", "o3", "o4") and (len(slug) == 2 or not slug[2].isalpha())


def _pick_best(models: list, prefix: str) -> dict:
    """
    From the full OpenRouter model list, pick the most expensive text-output
    model matching the given provider prefix.
    """
    candidates = []
    for m in models:
        mid = m.get("id", "")
        if not mid.startswith(prefix):
            continue
        # Exclude free-tier and variant suffixes (e.g. model:free, model:extended)
        if ":" in mid:
            continue
        # Exclude specialty substrings
        if any(s in mid for s in EXCLUDE_SUBSTRINGS):
            continue
        # Exclude reasoning-only models
        if _is_reasoning_model(mid):
            continue
        # Must output text
        out_mods = m.get("architecture", {}).get("output_modalities", [])
        if "text" not in out_mods:
            continue
        # Exclude image-output models (e.g. image generation)
        if "image" in out_mods:
            continue
        # Must have a real (non-zero) completion price
        try:
            price = float(m.get("pricing", {}).get("completion", "0"))
        except (ValueError, TypeError):
            price = 0.0
        if price <= 0:
            continue
        created = m.get("created", 0) or 0
        candidates.append((created, m))

    if not candidates:
        return None

    # Sort by creation date descending — newest model = most capable
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# -- Resolver -----------------------------------------------------------------
def resolve_models(verbose: bool = False) -> dict:
    """
    Returns a dict of agent_key -> model_name ready to pass to each native API.

    Example:
        {
            "claude": "claude-opus-4.7",
            "gpt":    "gpt-5",
            "gemini": "gemini-3.1-pro-preview",
            "grok":   "grok-4",
            "llama":  "meta-llama/llama-4-maverick",
        }

    Claude/GPT/Gemini/Grok get the slug only (prefix stripped).
    Llama keeps the full "meta-llama/..." path for Together.ai.
    """
    all_models = fetch_openrouter_models()
    resolved = {}

    for agent, prefix in PROVIDER_PREFIXES.items():
        best = _pick_best(all_models, prefix)
        if best is None:
            raise RuntimeError(
                f"[model_resolver] Could not find any valid {agent} model on OpenRouter."
            )
        model_id = best["id"]
        created  = best.get("created", 0) or 0

        if STRIP_PREFIX.get(agent, True):
            model_name = model_id.split("/", 1)[-1]
        else:
            model_name = model_id  # Llama: keep full path for Together.ai

        resolved[agent] = model_name

        if verbose:
            from datetime import datetime
            date_str = datetime.utcfromtimestamp(created).strftime("%Y-%m-%d") if created else "unknown"
            print(f"  {agent:8s} -> {model_name:45s} (released {date_str})")

    return resolved


# -- Standalone check ---------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  AIUNION Model Resolver v2 \u2014 Live Check           \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print(f"  Fetching from: {OPENROUTER_MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache: {CACHE_FILE}")
    print(f"  TTL:   {CACHE_TTL_HOURS}h\n")
