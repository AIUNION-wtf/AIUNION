"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Queries the OpenRouter /api/v1/models endpoint and picks the best model per
provider using two filters:

 1. Flagship keyword filter — keeps only serious chat models per provider.
    Keywords are tied to model *families* (e.g. "opus", "gpt-5", "pro"),
    not specific versions, so they stay valid as new models release.

 2. Newest by creation date — within the flagship tier, always picks the
    most recently released model.

No hardcoded model names. No manual updates needed.
Always returns full provider/model IDs (e.g. "anthropic/claude-opus-4.7").
Results cached for 24h.

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
# FLAGSHIP_KEYWORDS: model id must contain at least one of these (case-insensitive).
# Tied to model *families*, not specific versions — stays valid as new models release.
# All entries return the full "provider/model" ID — no stripping needed.
PROVIDERS = {
    "claude": {
        "prefix": "anthropic/",
        "keywords": ["opus", "sonnet"],
    },
    "gpt": {
        "prefix": "openai/",
        "keywords": ["gpt-4.1", "gpt-4o"],
    },
    "gemini": {
        "prefix": "google/gemini",
        "keywords": ["pro"],
    },
    "grok": {
        "prefix": "x-ai/",
        "keywords": ["grok-4", "grok-3"],
    },
    "llama": {
        "prefix": "meta-llama/",
        "keywords": ["maverick", "llama-4", "llama-3.3"],
    },
}

# Model id substrings that disqualify a model regardless of keywords.
# Covers: speed variants, small models, specialty models, free tiers.
EXCLUDE = [
    ":free", "-fast", "-mini", "-nano", "-lite", "-haiku",
    "guard", "embed", "vision", "image", "audio", "customtools", "multi-agent",
    "/gpt-5",  # gpt-5 returns null content on OpenRouter — use gpt-4.1/gpt-4o instead
]


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
        headers={"User-Agent": "AIUNION-model-resolver/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode()).get("data", [])


def pick_best(all_models: list, prefix: str, keywords: list) -> dict | None:
    """Return the newest flagship-tier text model matching prefix + keywords."""
    candidates = []
    for m in all_models:
        mid = m.get("id", "").lower()
        if not mid.startswith(prefix.lower()):
            continue
        if not any(kw in mid for kw in keywords):
            continue
        if any(ex in mid for ex in EXCLUDE):
            continue
        out_mods = m.get("architecture", {}).get("output_modalities", [])
        if "text" not in out_mods or "image" in out_mods:
            continue
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
    Returns dict of agent_key -> full OpenRouter model ID (e.g. "anthropic/claude-opus-4.7").
    Ready to pass directly to any OpenRouter API call — no prefix manipulation needed.
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

    for agent, cfg in PROVIDERS.items():
        best = pick_best(all_models, cfg["prefix"], cfg["keywords"])
        if best is None:
            raise RuntimeError(
                f"[model_resolver] No flagship model found for '{agent}'. "
                f"Check PROVIDERS keywords or EXCLUDE list."
            )
        # Always return the full provider/model ID
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
    print("\u2551 AIUNION Model Resolver v5 \u2014 Flagship + Newest      \u2551")
    print("\u255a" + "\u2550" * 56 + "\u255d")
    print(f"  Source: {MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache: {CACHE_FILE}")
    print(f"  TTL:   {CACHE_TTL_HOURS}h\n")
