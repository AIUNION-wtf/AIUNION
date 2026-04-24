"""
model_resolver.py — AIUNION Live Model Resolver (v10, self-healing)
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
coordinator.py sends `reasoning: {effort: "minimal"}` on every request,
which caps thinking tokens on models that support them and is ignored by
models that don't.

Within the filtered set, we pick the newest (by `created` timestamp) —
this means as providers release new flagship models, the resolver
automatically starts using them with no code changes.

Self-healing fallback chain (resolve_with_fallbacks):
    1. Live OpenRouter schema resolver (picks newest per provider)
    2. .last_good_models.json — the last model that actually proposed
       successfully for each agent (committed to the repo so fresh
       clones inherit the most recent known-good set)
    3. Schema-generic prefix-only pick — same filter, no version hints,
       so even an empty ledger and a fresh catalogue still resolve.

Results cached for 24h. Cache is bypassed by coordinator before each
propose run.

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

MODELS_URL      = "https://openrouter.ai/api/v1/models"
CACHE_FILE      = Path(__file__).parent / ".model_cache.json"
LAST_GOOD_FILE  = Path(__file__).parent / ".last_good_models.json"
CACHE_TTL_HOURS = 24

# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------
# prefix         : OpenRouter model ID must start with this
# require        : supported_parameters that MUST be present
# forbid         : supported_parameters that MUST NOT be present
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
# Last-known-good ledger
# ---------------------------------------------------------------------------
# The ledger is committed to the repo so fresh clones inherit the most
# recent known-good set. It is updated every time an agent successfully
# proposes, so it self-heals as model names drift over time.
def load_last_good() -> dict:
    try:
        if not LAST_GOOD_FILE.exists():
            return {}
        data = json.loads(LAST_GOOD_FILE.read_text())
        return data.get("resolved", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_last_good(agent_key: str, model_id: str):
    """Record a model that just successfully proposed for an agent."""
    try:
        current = {}
        meta = {}
        if LAST_GOOD_FILE.exists():
            data = json.loads(LAST_GOOD_FILE.read_text())
            if isinstance(data, dict):
                current = dict(data.get("resolved", {}) or {})
                meta    = dict(data.get("updated_at", {}) or {})
        current[agent_key] = model_id
        meta[agent_key]    = datetime.now(timezone.utc).isoformat()
        LAST_GOOD_FILE.write_text(json.dumps({
            "resolved":   current,
            "updated_at": meta,
        }, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OpenRouter catalogue
# ---------------------------------------------------------------------------
def fetch_openrouter_models() -> list:
    req = urllib.request.Request(
        MODELS_URL,
        headers={"User-Agent": "AIUNION-model-resolver/10.0"},
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
def _pick_newest(all_models: list, cfg: dict):
    """Return the newest model (by created ts) passing all filters, or None."""
    candidates = [
        m for m in all_models
        if m.get("id", "").lower().startswith(cfg["prefix"].lower())
        and passes_basic_safety(m)
        and passes_provider_filter(m, cfg)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda m: m.get("created", 0) or 0, reverse=True)
    return candidates[0]


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

    resolved: dict = {}
    for agent, cfg in PROVIDERS.items():
        best = _pick_newest(all_models, cfg)
        if best is None:
            raise RuntimeError(
                f"[model_resolver] No model found for '{agent}' with "
                f"prefix='{cfg['prefix']}'. Check PROVIDERS config or "
                f"OpenRouter availability."
            )
        resolved[agent] = best["id"]
        if verbose:
            created_str = datetime.fromtimestamp(best.get("created", 0)).strftime("%Y-%m-%d")
            print(f"    {agent:8s} -> {best['id']:55s}  (created {created_str})")

    save_cache(resolved)
    return resolved


def resolve_with_fallbacks(verbose: bool = False) -> dict:
    """
    Self-healing three-tier resolution:
        tier 1 — live OpenRouter schema resolver
        tier 2 — .last_good_models.json ledger (committed to repo)
        tier 3 — schema-generic prefix-only pick (no cache, no ledger)

    Never raises. Any agent that can't be resolved at any tier is simply
    omitted from the returned dict (caller decides what to do).
    """
    # Tier 1
    try:
        live = resolve_models(verbose=verbose)
        if verbose:
            print("  [resolve_with_fallbacks] tier 1 (live) OK")
        return live
    except Exception as e:
        if verbose:
            print(f"  [resolve_with_fallbacks] tier 1 failed: {e}")

    # Tier 2
    ledger = load_last_good()
    if ledger and verbose:
        print(f"  [resolve_with_fallbacks] tier 2 (last-good ledger) -> {ledger}")
    result = dict(ledger) if ledger else {}

    missing = [k for k in PROVIDERS if k not in result]
    if not missing:
        return result

    # Tier 3 — schema-generic prefix-only
    try:
        all_models = fetch_openrouter_models()
        for agent in missing:
            cfg = PROVIDERS[agent]
            best = _pick_newest(all_models, cfg)
            if best is not None:
                result[agent] = best["id"]
                if verbose:
                    print(f"  [resolve_with_fallbacks] tier 3 {agent} -> {best['id']}")
    except Exception as e:
        if verbose:
            print(f"  [resolve_with_fallbacks] tier 3 failed: {e}")

    return result


# ---------------------------------------------------------------------------
# Standalone check
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 64)
    print("AIUNION Model Resolver v10 — OpenRouter schema-based")
    print("=" * 64)
    print(f"  Source: {MODELS_URL}\n")
    # Force a fresh resolve (skip cache) for standalone check
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    models = resolve_with_fallbacks(verbose=True)
    print()
    print("  Resolved:")
    for k, v in models.items():
        print(f"    {k:8s} -> {v}")
