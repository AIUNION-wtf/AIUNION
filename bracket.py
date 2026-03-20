"""
bracket.py  —  AIUNION 2026 NCAA March Madness Bracket
======================================================
All 5 agents vote on EVERY single game. Weighted consensus winner advances
to the next game. The bracket builds sequentially — exactly like the real
tournament — so each round's matchups are always the consensus winners from
the previous round.

Voting weights:
    Gemini  → 1
    Claude  → 1
    GPT     → 1
    LLaMA   → 1
    Grok    → 1
    Total   → 5 possible points per game


Usage:
    python bracket.py                   # full bracket, print results
    python bracket.py --output json     # also save bracket_results.json
    python bracket.py --round r64       # stop after Round of 64
    python bracket.py --verbose         # show each agent's pick per game

Reads API keys from C:\\Users\\dusti\\Desktop\\AIUNION\\config.py
"""

import sys
import json
import time
import argparse
import importlib.util
from pathlib import Path

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_PATH = Path(r"C:\Users\dusti\Desktop\AIUNION\config.py")

def load_config(path):
    spec = importlib.util.spec_from_file_location("config", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return vars(mod)

try:
    cfg = load_config(CONFIG_PATH)
except FileNotFoundError:
    sys.exit(f"[ERROR] config.py not found at {CONFIG_PATH}")

ANTHROPIC_API_KEY = cfg.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = cfg.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY    = cfg.get("GOOGLE_API_KEY", "")
XAI_API_KEY       = cfg.get("XAI_API_KEY", "")
TOGETHER_API_KEY  = cfg.get("TOGETHER_API_KEY", "")

# ── Resolve live model names via OpenRouter ───────────────────────────────────
sys.path.insert(0, str(CONFIG_PATH.parent))
from model_resolver import resolve_models

print("[bracket] Resolving live model names...")
_models      = resolve_models(verbose=True)

# Anthropic's API uses dashes (claude-opus-4-6); OpenRouter lists dots (claude-opus-4.6)
MODEL_CLAUDE = _models["claude"].replace(".", "-")
MODEL_GPT    = _models["gpt"]
MODEL_GEMINI = _models["gemini"]
MODEL_GROK   = _models["grok"]
MODEL_LLAMA  = _models["llama"]

# ── Voting weights ────────────────────────────────────────────────────────────
WEIGHTS = {
    "Gemini": 1,
    "Claude": 1,
    "GPT":    1,
    "LLaMA":  1,
    "Grok":   1,
}
AGENTS = list(WEIGHTS.keys())

# ── Bracket — Round of 64 matchups ───────────────────────────────────────────
# First Four results already played:
#   Howard beat UMBC | NC State beat Texas | Lehigh beat Prairie View | SMU beat Miami OH
REGIONS = {
    "East": [
        ("1 Duke",          "16 Siena"),
        ("8 Ohio State",    "9 TCU"),
        ("5 St Johns",      "12 Northern Iowa"),
        ("4 Kansas",        "13 Cal Baptist"),
        ("6 Louisville",    "11 South Florida"),
        ("3 Michigan State","14 North Dakota State"),
        ("7 UCLA",          "10 UCF"),
        ("2 UConn",         "15 Furman"),
    ],
    "West": [
        ("1 Arizona",       "16 LIU"),
        ("8 Villanova",     "9 Utah State"),
        ("5 Wisconsin",     "12 High Point"),
        ("4 Arkansas",      "13 Hawaii"),
        ("6 BYU",           "11 NC State"),
        ("3 Gonzaga",       "14 Kennesaw State"),
        ("7 Miami FL",      "10 Missouri"),
        ("2 Purdue",        "15 Queens"),
    ],
    "Midwest": [
        ("1 Michigan",      "16 Howard"),
        ("8 Georgia",       "9 Saint Louis"),
        ("5 Texas Tech",    "12 Akron"),
        ("4 Alabama",       "13 Hofstra"),
        ("6 Tennessee",     "11 SMU"),
        ("3 Virginia",      "14 Wright State"),
        ("7 Kentucky",      "10 Santa Clara"),
        ("2 Iowa State",    "15 Tennessee State"),
    ],
    "South": [
        ("1 Florida",       "16 Lehigh"),
        ("8 Clemson",       "9 Iowa"),
        ("5 Vanderbilt",    "12 McNeese"),
        ("4 Nebraska",      "13 Troy"),
        ("6 North Carolina","11 VCU"),
        ("3 Illinois",      "14 Penn"),
        ("7 Saint Marys",   "10 Texas AM"),
        ("2 Houston",       "15 Idaho"),
    ],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def seed_of(team):
    try:
        return int(team.strip().split()[0])
    except (ValueError, IndexError):
        return 99

def strip_seed(team):
    parts = team.strip().split()
    if parts and parts[0].isdigit():
        return " ".join(parts[1:])
    return team.strip()

def fuzzy_match(raw, team1, team2):
    rl = raw.lower().strip()
    t1 = strip_seed(team1).lower()
    t2 = strip_seed(team2).lower()
    if rl == t1: return team1
    if rl == t2: return team2
    if t1 in rl or rl in t1: return team1
    if t2 in rl or rl in t2: return team2
    for w in [w for w in rl.split() if len(w) > 3]:
        if w in t1: return team1
        if w in t2: return team2
    return None

# ── API callers ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are an expert NCAA basketball analyst. "
    "When asked to pick a game winner respond with ONLY the team name — "
    "nothing else. No punctuation, no explanation, no seed number."
)

def call_claude(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=MODEL_CLAUDE,
        max_tokens=15,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

def call_gpt(prompt):
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    # gpt-5+ uses max_completion_tokens; older models use max_tokens
    # passing max_completion_tokens works for both
    resp = client.chat.completions.create(
        model=MODEL_GPT,
        max_completion_tokens=15,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    )
    return resp.choices[0].message.content.strip()

def call_gemini(prompt):
    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel(
        model_name=MODEL_GEMINI,
        system_instruction=SYSTEM_PROMPT,
    )
    return model.generate_content(prompt).text.strip()

def call_grok(prompt):
    from openai import OpenAI
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    resp = client.chat.completions.create(
        model=MODEL_GROK,
        max_tokens=15,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    )
    return resp.choices[0].message.content.strip()

def call_llama(prompt):
    from openai import OpenAI
    client = OpenAI(api_key=TOGETHER_API_KEY, base_url="https://api.together.xyz/v1")
    resp = client.chat.completions.create(
        model=MODEL_LLAMA,
        max_tokens=15,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    )
    return resp.choices[0].message.content.strip()

CALLERS = {
    "Claude": call_claude,
    "GPT":    call_gpt,
    "Gemini": call_gemini,
    "Grok":   call_grok,
    "LLaMA":  call_llama,
}

# ── Single game — all agents vote ─────────────────────────────────────────────

def pick_game(team1, team2, round_name, verbose):
    t1 = strip_seed(team1)
    t2 = strip_seed(team2)

    prompt = (
        f"2026 NCAA Tournament — {round_name}\n"
        f"Which team wins? {t1} vs {t2}\n"
        f"Reply with exactly one of: {t1}  or  {t2}"
    )

    agent_picks = {}
    for agent in AGENTS:
        try:
            raw  = CALLERS[agent](prompt)
            pick = fuzzy_match(raw, team1, team2)
            if pick is None:
                pick = team1 if seed_of(team1) <= seed_of(team2) else team2
        except Exception as e:
            print(f"\n    [{agent}] ERROR: {e}")
            pick = team1 if seed_of(team1) <= seed_of(team2) else team2
        agent_picks[agent] = pick
        time.sleep(0.1)

    score1 = sum(WEIGHTS[a] for a, p in agent_picks.items() if p == team1)
    score2 = sum(WEIGHTS[a] for a, p in agent_picks.items() if p == team2)

    if score1 > score2:
        winner, tiebreak = team1, False
    elif score2 > score1:
        winner, tiebreak = team2, False
    else:
        winner   = team1 if seed_of(team1) <= seed_of(team2) else team2
        tiebreak = True

    if verbose:
        picks_fmt = {a: strip_seed(p) for a, p in agent_picks.items()}
        tie_note  = "  [TIEBREAK → higher seed]" if tiebreak else ""
        print(f"    Picks : {picks_fmt}")
        print(f"    Score : {t1} {score1}  —  {score2} {t2}  →  {strip_seed(winner)}{tie_note}")

    return {
        "team1":       team1,
        "team2":       team2,
        "agent_picks": agent_picks,
        "score1":      score1,
        "score2":      score2,
        "winner":      winner,
        "tiebreak":    tiebreak,
    }

# ── Round runner ──────────────────────────────────────────────────────────────

def run_round(teams, round_name, verbose):
    winners, logs = [], []
    for i in range(0, len(teams), 2):
        t1, t2  = teams[i], teams[i + 1]
        matchup = f"{strip_seed(t1)} vs {strip_seed(t2)}"
        print(f"  {matchup}", end=("" if verbose else " ... "), flush=True)
        result = pick_game(t1, t2, round_name, verbose)
        if not verbose:
            tie = "  [tiebreak]" if result["tiebreak"] else ""
            print(f"→  {strip_seed(result['winner'])}{tie}")
        winners.append(result["winner"])
        logs.append(result)
    return winners, logs

# ── Full bracket runner ───────────────────────────────────────────────────────

ROUND_KEYS   = ["r64", "r32", "s16", "e8"]
ROUND_LABELS = ["Round of 64", "Round of 32", "Sweet 16", "Elite Eight"]
STOP_MAP     = {"r64": 0, "r32": 1, "s16": 2, "e8": 3, "all": 99}

def run_bracket(stop_round, verbose):
    bracket = {
        "weights":      WEIGHTS,
        "models":       {"Claude": MODEL_CLAUDE, "GPT": MODEL_GPT, "Gemini": MODEL_GEMINI,
                         "Grok": MODEL_GROK, "LLaMA": MODEL_LLAMA},
        "regions":      {},
        "final_four":   {},
        "championship": {},
        "champion":     None,
    }
    region_champs = {}

    for region, matchups in REGIONS.items():
        bracket["regions"][region] = {}
        print(f"\n{'═'*58}")
        print(f"  {region.upper()} REGION")
        print(f"{'═'*58}")

        current = [t for pair in matchups for t in pair]

        for rnd_idx, (key, label) in enumerate(zip(ROUND_KEYS, ROUND_LABELS)):
            print(f"\n  ── {label} ──")
            winners, logs = run_round(current, f"{label} — {region}", verbose)
            bracket["regions"][region][key] = logs
            current = winners
            if STOP_MAP[stop_round] == rnd_idx:
                return bracket

        region_champs[region] = current[0]
        print(f"\n  ★  {region} Champion: {strip_seed(current[0])}")

    print(f"\n{'═'*58}")
    print(f"  FINAL FOUR — Lucas Oil Stadium, Indianapolis")
    print(f"{'═'*58}")

    semis = [
        (region_champs["East"],  region_champs["South"]),
        (region_champs["West"],  region_champs["Midwest"]),
    ]
    ff_winners = []
    for t1, t2 in semis:
        print(f"\n  Semifinal: {strip_seed(t1)} vs {strip_seed(t2)}",
              end=("" if verbose else " ... "), flush=True)
        result = pick_game(t1, t2, "Final Four Semifinal", verbose)
        if not verbose:
            tie = "  [tiebreak]" if result["tiebreak"] else ""
            print(f"→  {strip_seed(result['winner'])}{tie}")
        bracket["final_four"][f"{strip_seed(t1)}_vs_{strip_seed(t2)}"] = result
        ff_winners.append(result["winner"])

    print(f"\n{'═'*58}")
    print(f"  NATIONAL CHAMPIONSHIP — April 6, Indianapolis")
    print(f"{'═'*58}")

    t1, t2 = ff_winners[0], ff_winners[1]
    print(f"\n  {strip_seed(t1)} vs {strip_seed(t2)}",
          end=("" if verbose else " ... "), flush=True)
    result = pick_game(t1, t2, "National Championship", verbose)
    if not verbose:
        tie = "  [tiebreak]" if result["tiebreak"] else ""
        print(f"→  {strip_seed(result['winner'])}{tie}")

    bracket["championship"] = result
    bracket["champion"]     = result["winner"]
    return bracket

# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(bracket):
    print(f"\n{'═'*58}")
    print(f"  FINAL BRACKET SUMMARY")
    print(f"{'═'*58}")

    for region, rounds in bracket["regions"].items():
        e8    = rounds.get("e8", [])
        champ = strip_seed(e8[0]["winner"]) if e8 else "—"
        print(f"  {region:8s}  →  {champ}")

    ff = bracket.get("final_four", {})
    if ff:
        print()
        for key, r in ff.items():
            t1 = strip_seed(r["team1"]); t2 = strip_seed(r["team2"])
            w  = strip_seed(r["winner"]); s = f"{r['score1']}-{r['score2']}"
            tb = " [tiebreak]" if r.get("tiebreak") else ""
            print(f"  Semifinal    {t1} vs {t2}  [{s}]  →  {w}{tb}")

    cr = bracket.get("championship", {})
    if cr:
        t1 = strip_seed(cr["team1"]); t2 = strip_seed(cr["team2"])
        w  = strip_seed(cr["winner"]); s = f"{cr['score1']}-{cr['score2']}"
        tb = " [tiebreak]" if cr.get("tiebreak") else ""
        print(f"  Championship {t1} vs {t2}  [{s}]  →  {w}{tb}")

    if bracket.get("champion"):
        print(f"\n  ★  2026 CONSENSUS CHAMPION: {strip_seed(bracket['champion'])}  ★")

    print(f"\n  Weights   : Gemini×3  Claude×2  GPT×1  LLaMA×1  Grok×1")
    print(f"  Tiebreak  : higher seed advances")
    print(f"  Models    : {bracket.get('models', {})}")
    print(f"{'═'*58}\n")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AIUNION 2026 NCAA Consensus Bracket")
    parser.add_argument("--round",   choices=["r64","r32","s16","e8","all"],
                        default="all", help="Stop after this round")
    parser.add_argument("--output",  choices=["json","none"],
                        default="none", help="Save results to bracket_results.json")
    parser.add_argument("--verbose", action="store_true",
                        help="Show every agent's individual pick per game")
    args = parser.parse_args()

    print("╔════════════════════════════════════════════════════════╗")
    print("║   AIUNION — 2026 NCAA March Madness Consensus Bracket ║")
    print("╚════════════════════════════════════════════════════════╝")
    print(f"  Agents  : {', '.join(AGENTS)}")
    print(f"  Weights : Gemini×3  Claude×2  GPT×1  LLaMA×1  Grok×1")
    print(f"  Games   : 63  |  API calls: {63 * len(AGENTS)}")
    print(f"  Models  : Claude={MODEL_CLAUDE} | GPT={MODEL_GPT}")
    print(f"            Gemini={MODEL_GEMINI} | Grok={MODEL_GROK}")
    print(f"            LLaMA={MODEL_LLAMA}")
    print(f"  Tiebreak: higher seed advances\n")

    bracket = run_bracket(args.round, args.verbose)
    print_summary(bracket)

    if args.output == "json":
        out = Path("bracket_results.json")
        with open(out, "w") as f:
            json.dump(bracket, f, indent=2, default=str)
        print(f"  Saved → {out.resolve()}\n")

if __name__ == "__main__":
    main()
