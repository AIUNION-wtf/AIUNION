"""
model_resolver.py — AIUNION Live Model Resolver
==================================================
Resolves the best available chat model per provider using two sources:

 1. PRIMARY — Chatbot Arena (arena.ai): scraped from the live leaderboard,
    backed by ~6M real human preference votes. The top-ranked model per
    provider that also exists on OpenRouter is used.

 2. FALLBACK — OpenRouter /api/v1/models: if Arena scraping fails or a
    provider's top Arena model isn't available on OpenRouter, falls back to
    keyword + newest filtering against the OpenRouter catalogue.

Results cached for 24h.

Run standalone to check current resolved models:
    python model_resolver.py
"""

import json
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

MODELS_URL  = "https://openrouter.ai/api/v1/models"
ARENA_URL   = "https://arena.ai/leaderboard/text"
CACHE_FILE  = Path(__file__).parent / ".model_cache.json"
CACHE_TTL_HOURS = 24

# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------
# prefix:    OpenRouter provider prefix to scope model lookup.
# keywords:  Fallback keyword filter — model id must contain at least one.
# arena_org: String that appears in the Arena leaderboard "org" cell for
#            this provider, used to identify their models in the scraped data.
PROVIDERS = {
    "claude": {
        "prefix":    "anthropic/",
        "keywords":  ["opus"],
        "arena_org": "Anthropic",
    },
    "gpt": {
        "prefix":    "openai/",
        "keywords":  ["gpt-4.1", "gpt-4o"],
        "arena_org": "OpenAI",
    },
    "gemini": {
        "prefix":    "google/gemini",
        "keywords":  ["pro-preview", "gemini-pro", "gemini-2.5-pro"],
        "arena_org": "Google",
    },
    "grok": {
        "prefix":    "x-ai/",
        "keywords":  ["grok-4."],
        "arena_org": "xAI",
    },
    "llama": {
        "prefix":    "meta-llama/",
        "keywords":  ["maverick", "llama-4"],
        "arena_org": "Meta",
    },
}

# Substrings that disqualify a model from OpenRouter fallback.
EXCLUDE = [
    "guard", "embed", "multi-agent", "customtools",
    "-image-", "image-2", "gemma",
    "-nano", "-mini", "-lite", "-fast", "-flash", "scout",
    ":free",
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
            "resolved":  resolved,
        }, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Source 1 — Chatbot Arena scraper
# ---------------------------------------------------------------------------
def scrape_arena_leaderboard() -> dict[str, str]:
    """
    Scrape arena.ai/leaderboard/text and return a dict of
    arena_org -> arena_model_name for the top-ranked model per org.
    Model names use Arena's short format (e.g. "claude-opus-4-7").
    """
    req = urllib.request.Request(
        ARENA_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AIUNION-resolver/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Arena SSR page — model names and orgs are in <td> cells inside a <table>.
    # Row pattern: <td>RANK</td> ... <td>MODEL_NAME ORG [dot] ...</td>
    # We parse all <td> text blocks and zip rank+name+org.
    td_texts = re.findall(r'<td[^>]*>(.*?)</td>', html, re.DOTALL)
    # Strip HTML tags within cells
    def strip(s):
        return re.sub(r'<[^>]+>', '', s).strip()

    clean = [strip(t) for t in td_texts]

    # Table columns (based on observed structure):
    # 0:rank  1:rank_spread  2:model+org  3:elo  4:ci  5:votes  6:org_badge  ...
    # We walk in strides of ~7 and pull rank + model_cell
    best_per_org: dict[str, tuple[int, str]] = {}  # org -> (rank, arena_name)

    i = 0
    while i < len(clean) - 2:
        rank_str = clean[i].strip()
        if not rank_str.isdigit():
            i += 1
            continue
        rank = int(rank_str)
        # model cell is typically 2 positions ahead
        model_cell = clean[i + 2] if i + 2 < len(clean) else ""
        lines = [l.strip() for l in model_cell.split("\n") if l.strip()]
        if len(lines) < 2:
            i += 1
            continue
        arena_name = lines[0]
        org_line   = lines[1]  # e.g. "Anthropic [dot] Proprietary"
        org        = org_line.split()[0].strip() if org_line.split() else ""

        if org and arena_name:
            if org not in best_per_org or rank < best_per_org[org][0]:
                best_per_org[org] = (rank, arena_name)
        i += 1

    return {org: name for org, (_, name) in best_per_org.items()}


def arena_name_to_openrouter_id(
    arena_name: str,
    prefix: str,
    all_openrouter_models: list,
) -> str | None:
    """
    Convert an Arena short name (e.g. "claude-opus-4-7") to the matching
    OpenRouter model ID (e.g. "anthropic/claude-opus-4.7").

    Strategy: normalise both strings (replace - with . and vice-versa, lower)
    and look for the best substring match among OpenRouter models with prefix.
    """
    # Normalise: Arena uses hyphens everywhere, OR uses dots in version numbers
    def normalise(s: str) -> str:
        return s.lower().replace(".", "-").replace("_", "-")

    norm_arena = normalise(arena_name)
    # Remove known Arena-only suffixes that don't appear in OR IDs
    for suffix in ["-thinking", "-preview", "-beta", "-beta1", "-high"]:
        norm_arena = norm_arena.replace(normalise(suffix), "")
    norm_arena = norm_arena.strip("-")

    candidates = [
        m for m in all_openrouter_models
        if m.get("id", "").lower().startswith(prefix.lower())
        and not m.get("expiration_date")
        and float(m.get("pricing", {}).get("completion", "0") or "0") > 0
    ]

    # Score each candidate by how much of norm_arena appears in normalise(id)
    scored = []
    for m in candidates:
        norm_id = normalise(m["id"])
        # Count matching tokens
        tokens = [t for t in norm_arena.split("-") if t]
        hits = sum(1 for t in tokens if t in norm_id)
        if hits > 0:
            scored.append((hits, m.get("created", 0) or 0, m))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]["id"]


# ---------------------------------------------------------------------------
# Source 2 — OpenRouter fallback
# ---------------------------------------------------------------------------
def fetch_openrouter_models() -> list:
    req = urllib.request.Request(
        MODELS_URL,
        headers={"User-Agent": "AIUNION-model-resolver/8.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode()).get("data", [])


def pick_best_openrouter(all_models: list, prefix: str, keywords: list) -> dict | None:
    """Return the newest flagship-tier chat model matching prefix + keywords."""
    candidates = []
    for m in all_models:
        mid = m.get("id", "").lower()
        if not mid.startswith(prefix.lower()):
            continue
        if not any(kw.lower() in mid for kw in keywords):
            continue
        if any(ex.lower() in mid for ex in EXCLUDE):
            continue
        if m.get("expiration_date"):
            continue
        out_mods = m.get("architecture", {}).get("output_modalities", [])
        if "text" not in out_mods:
            continue
        price = float(m.get("pricing", {}).get("completion", "0") or "0")
        if price <= 0:
            continue
        candidates.append(m)
    if not candidates:
        return None
    candidates.sort(key=lambda m: m.get("created", 0) or 0, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
def resolve_models(verbose: bool = False) -> dict:
    """
    Returns dict of agent_key -> full OpenRouter model ID.
    Tries Arena first per provider; falls back to OpenRouter keyword filter.
    """
    cached = load_cache()
    if cached:
        if verbose:
            print(" [using 24h cache]")
            for agent, model in cached.items():
                print(f"  {agent:8s} -> {model}")
        return cached

    # Fetch OpenRouter catalogue (needed for both Arena ID mapping and fallback)
    if verbose:
        print(" Fetching OpenRouter model catalogue...")
    all_or_models = fetch_openrouter_models()

    # Attempt Arena scrape
    arena_top: dict[str, str] = {}
    try:
        if verbose:
            print(" Scraping Chatbot Arena leaderboard...")
        arena_top = scrape_arena_leaderboard()
        if verbose:
            print(f"  Arena returned top models for {len(arena_top)} orgs.")
    except Exception as e:
        if verbose:
            print(f"  Arena scrape failed ({e}) — using OpenRouter fallback for all providers.")

    resolved: dict[str, str] = {}

    for agent, cfg in PROVIDERS.items():
        model_id: str | None = None
        source = "?"

        # --- Try Arena ---
        arena_org  = cfg["arena_org"]
        arena_name = arena_top.get(arena_org)
        if arena_name:
            model_id = arena_name_to_openrouter_id(arena_name, cfg["prefix"], all_or_models)
            if model_id:
                source = f"Arena #{arena_name}"
            elif verbose:
                print(f"  [{agent}] Arena pick '{arena_name}' not found on OpenRouter — trying fallback.")

        # --- Fallback: OpenRouter keyword filter ---
        if not model_id:
            best = pick_best_openrouter(all_or_models, cfg["prefix"], cfg["keywords"])
            if best:
                model_id = best["id"]
                source = "OpenRouter fallback"

        if not model_id:
            raise RuntimeError(
                f"[model_resolver] No model found for '{agent}'. "
                f"Arena org='{arena_org}', prefix='{cfg['prefix']}'. "
                f"Check PROVIDERS config."
            )

        resolved[agent] = model_id
        if verbose:
            print(f"  {agent:8s} -> {model_id:55s} [{source}]")

    save_cache(resolved)
    return resolved


# ---------------------------------------------------------------------------
# Standalone check
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\u2554" + "\u2550" * 62 + "\u2557")
    print("\u2551 AIUNION Model Resolver v8 \u2014 Arena primary / OR fallback \u2551")
    print("\u255a" + "\u2550" * 62 + "\u255d")
    print(f" Arena:  {ARENA_URL}")
    print(f" OR API: {MODELS_URL}\n")
    models = resolve_models(verbose=True)
    print(f"\n Resolved {len(models)} agents.")
    print(f" Cache: {CACHE_FILE}")
    print(f" TTL:   {CACHE_TTL_HOURS}h\n")
