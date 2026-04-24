"""
model_resolver.py — AIUNION Live Model Resolver (v9, schema-based)
===================================================================
Resolves the best available chat model per provider using ONLY the
OpenRouter /api/v1/models catalogue and its declarative schema.

Core idea: instead of matching model names (which change over time),
filter by declarative capability signals from the OpenRouter schema:

  * architecture.output_modalities == ["text"]   — text-only output
    (blocks image/audio multi-output models that reject plain chat)
  * architecture.input_modalities contains "text"
  * pricing.completion > 0                       — paid tier (blocks free)
  * expiration_date is None                      — not being sunset
  * "max_tokens" in supported_parameters         — accepts token limit

Per provider, we can optionally require/forbid specific supported_parameters
to exclude provider-specific problem models (e.g. for OpenAI, we require
"temperature" to exclude reasoning-only models like o3/gpt-5 that reject
standard chat completion requests).

Reasoning-capable models (Claude, Gemini, Grok) are safely used because
coordinator.py sends `reasoning: {effort: "none"}` on every request, which
disables thinking tokens on models that support them and is ignored by
models that don't.

Within the filtered set, we pick the newest (by `created` timestamp) —
this means as providers release new flagship models, the resolver
automatically starts using them with no code changes.

Results cached for 24h.

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

MODELS_URL     = "https://openrouter.ai/api/v1/models"
CACHE_FILE     = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24

# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------
# prefix   : OpenRouter model ID prefix scoping this provider's models
# require  : supported_parameters values that MUST be present
# forbid   : supported_parameters values that MUST NOT be present
# name_blocklist : case-insensitive substrings that disqualify a model by
#                  ID — used only as a last-resort safety net for variants
#                  that the schema cannot distinguish (e.g. "-image-",
#                  "-audio", "-guard", "-safeguard"). Kept minimal.
PROVIDERS = {
    "claude": {
        "prefix":         "anthropic/",
        "require":        [],
        "forbid":         [],
        "name_blocklist": ["haiku", "-fast", "-latest"],
    },
    "gpt": {
        "prefix":         "openai/",
        # OpenAI reasoning models (o1/o3/o4/gpt-5) lack "temperature" and
        # reject our standard chat requests. Requiring temperature filters
        # them out without naming any specific model.
        "require":        ["temperature"],
        "forbid":         ["reasoning"],
        "name_blocklist": ["oss", "audio", "guard", "safeguard",
                           "deep-research", "-image", "-mini", "-nano"],
    },
    "gemini": {
        "prefix":         "google/gemini",
        "require":        ["temperature"],
        "forbid":         [],  # can't forbid reasoning — all new Geminis have it
        "name_blocklist": ["-image", "-flash-lite", "customtools",
                           "-lite", "-audio", "embedding"],
    },
    "grok": {
        "prefix":         "x-ai/",
        "require":        ["temperature"],
        "forbid":         [],
        "name_blocklist": ["multi-agent", "customtools", "-image",
                           "-mini", "-nano", "-fast"],
    },
    "llama": {
        "prefix":         "meta-llama/",
        "require":        [],
        "forbid":         [],
        "name_blocklist": ["guard", "embed", "-scout", "-mini", "-nano",
                           "prompt-guard"],
    },
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
            "resolved":  resolved,
        }, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OpenRouter catalogue
# ---------------------------------------------------------------------------
def fetch_openrouter_models() -> list:
    req = urllib.request.Request(
        MODELS_URL,
        headers={"User-Agent": "AIUNION-model-resolver/9.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode()).get("data", [])


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def passes_basic_safety(m: dict) -> bool:
    """Universal schema filter for plain-chat capable models."""
    arch = m.get("architecture", {}) or {}
    out_mods = arch.get("output_modalities", []) or []
    in_mods  = arch.get("input_modalities", []) or []
    sp       = m.get("supported_parameters", []) or []
    price    = m.get("pricing", {}) or {}

    if out_mods != ["text"]:
        return False
    if "text" not in in_mods:
        return False
    try:
        if float(price.get("completion", "0") or "0") <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if m.get("expiration_date"):
        return False
    if "max_tokens" not in sp:
        return False
    return True


def passes_provider_filter(m: dict, cfg: dict) -> bool:
    """Per-provider require/forbid/name_blocklist rules."""
    sp = m.get("supported_parameters", []) or []
    for r in cfg["require"]:
        if r not in sp:
            return False
    for f in cfg["forbid"]:
        if f in sp:
            return False
    mid_lower = m.get("id", "").lower()
    for bad in cfg["name_blocklist"]:
        if bad.lower() in mid_lower:
            return False
    return True


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def resolve_models(verbose: bool = False) -> dict:
    """
    Returns dict of agent_key -> full OpenRouter model ID.
    Picks the newest model per provider that passes both the basic
    safety filter and that provider's require/forbid/blocklist rules.
    """
    cached = load_cache()
    if cached:
        if verbose:
            print("  [using 24h cache]")
            for agent, model in cached.items():
                print(f"    {agent:8s} -> {model}")
        return cached

    if verbose:
        print("  Fetching OpenRouter model catalogue...")
    all_models = fetch_openrouter_models()

    resolved: dict[str, str] = {}
    for agent, cfg in PROVIDERS.items():
        candidates = [
            m for m in all_models
            if m.get("id", "").lower().startswith(cfg["prefix"].lower())
            and passes_basic_safety(m)
            and passes_provider_filter(m, cfg)
        ]
        if not candidates:
            raise RuntimeError(
                f"[model_resolver] No model found for '{agent}' with "
                f"prefix='{cfg['prefix']}'. Check PROVIDERS config or "
                f"OpenRouter availability."
            )
        # Newest first
        candidates.sort(key=lambda m: m.get("created", 0) or 0, reverse=True)
        best = candidates[0]
        resolved[agent] = best["id"]
        if verbose:
            created_str = datetime.fromtimestamp(best.get("created", 0)).strftime("%Y-%m-%d")
            print(f"    {agent:8s} -> {best['id']:55s}  (created {created_str})")

    save_cache(resolved)
    return resolved


# ---------------------------------------------------------------------------
# Standalone check
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 62 + "\u2557")
    print("\u2551 AIUNION Model Resolver v9 \u2014 OpenRouter schema-based       \u2551")
    print("\u255a" + "\u2550" * 62 + "\u255d")
    print(f"  Source: {MODELS_URL}\n")
    # Force a fresh resolve (skip cache) for standalone check
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    models = resolve_models(verbose=True)
    print(f"\n  Resolved {len(models)} agents.")
    print(f"  Cache:  {CACHE_FILE}")
    print(f"  TTL:    {CACHE_TTL_HOURS}h\n")
