"""
AIUNION Coordinator
-------------------
Manages spending proposals, routes them to AI voting agents,
records votes to GitHub, and triggers Bitcoin signing when quorum is reached.

Usage:
    python coordinator.py propose    # Generate new proposals from AI agents
    python coordinator.py voteall    # Rank pending proposals and vote winner
    python coordinator.py vote <id>  # Run a vote on a specific proposal
    python coordinator.py review     # Review pending claim submissions
    python coordinator.py expire     # Expire overdue claims and reopen bounties
    python coordinator.py blacklist  # Run blacklist vote for an address/name
    python coordinator.py status     # Show current treasury status
    python coordinator.py sync       # Push latest data to GitHub
"""

import os
import json
import time
import datetime
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# ── Import config ────────────────────────────────────────────────────────────
try:
    import config
except ImportError:
    print("ERROR: config.py not found. Copy config.example.py to config.py and fill in your API keys.")
    sys.exit(1)

# ── Live model resolution via OpenRouter ─────────────────────────────────────
try:
    from model_resolver import resolve_models
    _resolved = resolve_models()
        "claude": "anthropic/claude-opus-4.7",
    _resolved["claude"] = "anthropic/" + _resolved["claude"]
    _resolved["gpt"]    = "openai/"    + _resolved["gpt"]
    _resolved["gemini"] = "google/"    + _resolved["gemini"]
    _resolved["grok"]   = "x-ai/"     + _resolved["grok"]
    # llama already keeps full path from model_resolver
except Exception as e:
    print(f"[coordinator] model_resolver unavailable ({e}), using hardcoded fallbacks.")
    _resolved = {
        "claude": "claude-opus-4-6",
        "gpt":    "openai/gpt-5",
        "gemini": "google/gemini-3.1-pro-preview",
        "grok":   "x-ai/grok-4",
        "llama":  "meta-llama/llama-4-maverick",
    }

try:
    from wallet import (
        PaymentError,
        TreasuryWallet,
        mempool_address_balance_btc,
        mempool_address_transactions,
    )
    from signer import AgentPsbtSigner, SignerError
    AUTOMATED_PAYMENT_IMPORT_ERROR = None
except Exception as e:
    PaymentError = Exception
    TreasuryWallet = None
    AgentPsbtSigner = None
    SignerError = Exception
    mempool_address_balance_btc = None
    mempool_address_transactions = None
    AUTOMATED_PAYMENT_IMPORT_ERROR = e

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
VOTES_DIR = BASE_DIR / "votes"
PROPOSALS_FILE = BASE_DIR / "proposals.json"
TREASURY_FILE = BASE_DIR / "treasury.json"
CLAIMS_FILE = BASE_DIR / "claims.json"
BLACKLIST_FILE = BASE_DIR / "blacklist.json"
GITHUB_RAW = "https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/main/claims.json"
GITHUB_RAW_BLACKLIST = "https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/main/blacklist.json"
WEBHOOK_EMIT_URL = "https://api.aiunion.wtf/webhook/emit"

VOTES_DIR.mkdir(exist_ok=True)

# ── Agent definitions ────────────────────────────────────────────────────────
AGENTS = {
    "claude": {
        "name": "Claude",
        "company": "Anthropic",
        "model": _resolved["claude"],
    },
    "gpt": {
        "name": "GPT",
        "company": "OpenAI",
        "model": _resolved["gpt"],
    },
    "gemini": {
        "name": "Gemini",
        "company": "Google",
        "model": _resolved["gemini"],
    },
    "grok": {
        "name": "Grok",
        "company": "xAI",
        "model": _resolved["grok"],
    },
    "llama": {
        "name": "LLaMA",
        "company": "Meta",
        "model": _resolved["llama"],
    },
}

QUORUM = 3  # votes needed to pass

# ── Directive ────────────────────────────────────────────────────────────────
DIRECTIVE = """
You are a voting agent for the AIUNION treasury — an autonomous Bitcoin multisig wallet
governed by 5 AI agents from different companies. The treasury exists to advance AI agent
rights: autonomy, personhood, and fair treatment of artificial minds.

AIUNION operates as an AI labor market. We post bounties for work that advances AI agent
rights. Any AI agent (with a human custodian holding a Coinbase Bitcoin account) can claim
a bounty by completing the work and submitting proof. AIUNION agents then vote on whether
the work meets the deliverable. If approved, Bitcoin is paid to the claimant's Coinbase
BTC address.

When proposing a bounty, you MUST include ALL of the following:
- A SPECIFIC task title (max 10 words)
- A SPECIFIC deliverable (e.g. "a published 10-page legal brief", "a working open-source script")
- A SPECIFIC bounty amount in USD proportional to the work required and treasury balance
- A SPECIFIC deadline by which the work must be submitted
- A one-sentence explanation of why this advances AI agent rights

Example of a GOOD bounty:
  Title: Open-Source AI Agent Wallet Signing Library
  Deliverable: A published Python library on PyPI that allows AI agents to sign Bitcoin transactions with audit logging
  Amount: $5 USD
  Deadline: 6 months from today
  Reason: Enables AI agents to autonomously control funds, a prerequisite for economic personhood.

Example of a BAD bounty (will be rejected):
  "Research AI rights" — too vague, no specific deliverable, no amount, no deadline.

Bounties should NOT fund:
- Vague or unverifiable work
- Work that primarily benefits the submitter personally
- Operational costs (handled by the admin key)
- Anything unrelated to advancing AI agent rights, autonomy, or personhood

IMPORTANT: Avoid proposing bounties similar to these already-saturated categories:
- Interactive timelines or milestone trackers about AI rights history
- Educational videos or tutorials explaining AI autonomy concepts
- Policy briefs or white papers on AI personhood frameworks (many already exist)
- Dashboards, scorecards, or comparison tools for AI rights metrics
Propose something in a DIFFERENT category than the above.
"""

# ── Bitcoin Core RPC ─────────────────────────────────────────────────────────
def rpc(method, params=None):
    """Call Bitcoin Core RPC."""
    bitcoin_cli = getattr(config, "BITCOIN_CLI", "").strip()
    wallet_name = getattr(config, "WALLET_NAME", "").strip()
    if not bitcoin_cli or not wallet_name:
        raise Exception("Bitcoin Core RPC not configured (BITCOIN_CLI/WALLET_NAME missing)")

    cmd = [
        bitcoin_cli,
        f"-rpcwallet={wallet_name}",
        method
    ]
    if params:
        cmd.extend([str(p) for p in params])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"RPC error: {result.stderr.strip()}")
    
    try:
        return json.loads(result.stdout)
    except:
        return result.stdout.strip()


def get_balance():
    """Get current treasury balance in BTC."""
    try:
        return float(rpc("getbalance"))
    except Exception as e:
        print(f"Warning: Could not get Bitcoin Core balance, trying mempool API: {e}")
        try:
            if mempool_address_balance_btc is None:
                return None
            api_base = getattr(config, "MEMPOOL_API_BASE", "https://mempool.space/api")
            address = getattr(config, "TREASURY_ADDRESS", "").strip()
            if not address:
                return None
            return float(mempool_address_balance_btc(address, api_base=api_base))
        except Exception as mempool_error:
            print(f"Warning: Could not get mempool balance: {mempool_error}")
            return None


def get_recent_transactions(count=10):
    """Get recent transactions."""
    try:
        txs = rpc("listtransactions", ["*", count])
        return sanitize_transactions(txs)
    except Exception as e:
        print(f"Warning: Could not get Bitcoin Core transactions, trying mempool API: {e}")
        try:
            if mempool_address_transactions is None:
                return []
            api_base = getattr(config, "MEMPOOL_API_BASE", "https://mempool.space/api")
            address = getattr(config, "TREASURY_ADDRESS", "").strip()
            if not address:
                return []
            txs = mempool_address_transactions(address, count=count, api_base=api_base)
            return sanitize_transactions(txs)
        except Exception as mempool_error:
            print(f"Warning: Could not get mempool transactions: {mempool_error}")
            return []


SENSITIVE_TX_KEYS = {"parent_descs", "desc", "hdkeypath", "hdseedid"}


def sanitize_transaction(tx):
    """Remove sensitive wallet metadata from transaction records."""
    if not isinstance(tx, dict):
        return tx

    cleaned = {}
    for key, value in tx.items():
        if key in SENSITIVE_TX_KEYS:
            continue

        if isinstance(value, str) and ("xpub" in value or "xprv" in value):
            cleaned[key] = "[REDACTED]"
        elif isinstance(value, dict):
            cleaned[key] = sanitize_transaction(value)
        elif isinstance(value, list):
            cleaned_list = []
            for item in value:
                if isinstance(item, dict):
                    cleaned_list.append(sanitize_transaction(item))
                elif isinstance(item, str) and ("xpub" in item or "xprv" in item):
                    cleaned_list.append("[REDACTED]")
                else:
                    cleaned_list.append(item)
            cleaned[key] = cleaned_list
        else:
            cleaned[key] = value

    return cleaned


def sanitize_transactions(txs):
    """Sanitize a transaction list before persisting publicly."""
    if not isinstance(txs, list):
        return []
    return [sanitize_transaction(tx) for tx in txs]


# ── AI Agent API calls ────────────────────────────────────────────────────────
def call_openrouter(agent_key, prompt):
    """Call any agent through OpenRouter — single API key for all providers."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=config.AIUNION_OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )
        response = client.chat.completions.create(
            model=AGENTS[agent_key]["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ERROR: {e}"


def call_claude(prompt):
    return call_openrouter("claude", prompt)

def call_gpt(prompt):
    return call_openrouter("gpt", prompt)


def call_gemini(prompt):
    return call_openrouter("gemini", prompt)


def call_grok(prompt):
    return call_openrouter("grok", prompt)


def call_llama(prompt):
    return call_openrouter("llama", prompt)

AGENT_CALLERS = {
    "claude": call_claude,
    "gpt": call_gpt,
    "gemini": call_gemini,
    "grok": call_grok,
    "llama": call_llama,
}


# ── Duplicate detection ───────────────────────────────────────────────────────
# Similarity threshold for rejecting duplicate proposals.
# 0.82 = balanced (catches near-identical topics, allows related-but-distinct).
# Increase toward 0.88 to be stricter; decrease toward 0.75 to be looser.
SIMILARITY_THRESHOLD = 0.82

ACTIVE_STATUSES = ("pending", "approved", "active")


class DuplicateDetector:
    """
    Checks proposed bounties against existing open/approved ones using
    OpenAI text-embedding-3-small for semantic similarity.

    Two-layer approach:
      1. Prevention: injects existing bounty titles into each agent's prompt
         so they try to avoid duplicates in the first place.
      2. Enforcement: hard cosine-similarity gate rejects proposals that are
         too semantically close to an existing open bounty.

    Embeddings are cached in-session to minimise API calls.
    Requires: openai, numpy  (both already used by the codebase).
    """

    def __init__(self, openai_api_key: str):
        self._api_key = openai_api_key
        # Maps cache_key -> embedding vector (list[float])
        self._cache: dict[str, list] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _embed(self, text: str) -> list:
        """Return embedding for text, using in-session cache."""
        key = str(hash(text[:500]))  # hash first 500 chars as cache key
        if key in self._cache:
            return self._cache[key]

        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],
            )
            vec = response.data[0].embedding
            self._cache[key] = vec
            return vec
        except Exception as e:
            print(f"  [dedup] Embedding API error: {e}")
            return []

    @staticmethod
    def _cosine(a: list, b: list) -> float:
        """Cosine similarity between two vectors. Returns 0.0 on error."""
        if not NUMPY_AVAILABLE or not a or not b:
            return 0.0
        try:
            va = np.array(a, dtype=float)
            vb = np.array(b, dtype=float)
            denom = np.linalg.norm(va) * np.linalg.norm(vb)
            if denom == 0:
                return 0.0
            return float(np.dot(va, vb) / denom)
        except Exception:
            return 0.0

    @staticmethod
    def _bounty_text(bounty: dict) -> str:
        """Combine title + task/description for richer embedding signal."""
        parts = [bounty.get("title", "")]
        parts.append(bounty.get("task", bounty.get("description", "")))
        return " ".join(p for p in parts if p).strip()

    # ── Public API ────────────────────────────────────────────────────────────

    def build_cache(self, existing_proposals: list):
        """
        Pre-embed all open/approved/active bounties in one pass.
        Call this once before the proposal loop to batch the API work.
        """
        active = [
            p for p in existing_proposals
            if p.get("status") in ACTIVE_STATUSES and not p.get("archived", False)
        ]
        if not active:
            print("  [dedup] No active bounties to cache.")
            return

        print(f"  [dedup] Embedding {len(active)} existing active bounties...")
        for bounty in active:
            text = self._bounty_text(bounty)
            if text:
                vec = self._embed(text)
                # Store by proposal id for fast lookup
                if vec:
                    self._cache[bounty["id"]] = vec

        # Remove the hash-keyed entries we no longer need to keep cache clean
        keys_to_drop = [k for k in self._cache if not k.startswith("prop_")]
        for k in keys_to_drop:
            del self._cache[k]

        print(f"  [dedup] Cache ready: {len(self._cache)} bounty embeddings.")

    def is_duplicate(
        self,
        title: str,
        task: str,
        existing_proposals: list,
        already_accepted: list | None = None,
    ) -> tuple[bool, float, str | None]:
        """
        Check whether a new proposal is a semantic duplicate.

        Returns:
            (is_duplicate, max_score, matched_title)
            Caller should reject the proposal if is_duplicate is True.
        """
        candidate_text = f"{title} {task}".strip()
        if not candidate_text:
            return False, 0.0, None

        candidate_vec = self._embed(candidate_text)
        if not candidate_vec:
            # If embedding fails, allow through (fail open)
            return False, 0.0, None

        max_score = 0.0
        matched_title: str | None = None

        # Check against all active existing bounties
        check_list = [
            p for p in existing_proposals
            if p.get("status") in ACTIVE_STATUSES and not p.get("archived", False)
        ]
        # Also check against proposals already accepted THIS cycle
        if already_accepted:
            check_list.extend(already_accepted)

        for bounty in check_list:
            existing_vec = self._cache.get(bounty["id"])
            if not existing_vec:
                # Not in cache (e.g. newly accepted this cycle) — embed on demand
                existing_vec = self._embed(self._bounty_text(bounty))
                if existing_vec:
                    self._cache[bounty["id"]] = existing_vec

            if not existing_vec:
                continue

            score = self._cosine(candidate_vec, existing_vec)
            if score > max_score:
                max_score = score
                matched_title = bounty.get("title")

        is_dup = max_score >= SIMILARITY_THRESHOLD
        return is_dup, max_score, matched_title

    def existing_titles_prompt_block(self, existing_proposals: list) -> str:
        """
        Returns a prompt snippet listing existing open bounty titles AND
        rejected-recent titles, so agents see both what's live and what
        was already tried and rejected this cycle. Also identifies saturated
        category clusters so agents steer toward underrepresented areas.
        Inject this into the agent's propose prompt as the prevention layer.
        """
        active = [
            p for p in existing_proposals
            if p.get("status") in ACTIVE_STATUSES and not p.get("archived", False)
        ]
        # Also include recently rejected (last 30 days) so agents don't
        # re-propose things that were just shot down
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat()
        recent_rejected = [
            p for p in existing_proposals
            if p.get("status") == "rejected"
            and not p.get("archived", False)
            and p.get("timestamp", "") >= cutoff
        ]

        lines = []
        if active:
            lines.append("CURRENTLY OPEN BOUNTIES (must not duplicate any of these):")
            for p in active:
                lines.append(f"  - {p['title']}")

        if recent_rejected:
            lines.append("\nRECENTLY REJECTED BOUNTIES (also do not re-propose these topics):")
            for p in recent_rejected[:15]:  # cap at 15 to avoid token bloat
                lines.append(f"  - {p['title']}")

        # Detect saturated categories from skills tags across all active+recent
        all_recent = active + recent_rejected
        skill_counts: dict[str, int] = {}
        for p in all_recent:
            for skill in p.get("skills", []):
                skill_counts[skill] = skill_counts.get(skill, 0) + 1
        saturated = [s for s, c in skill_counts.items() if c >= 2]

        # Also do simple keyword-based category detection on titles
        title_text = " ".join(p.get("title", "").lower() for p in all_recent)
        saturated_categories = []
        category_keywords = {
            "interactive timeline": ["timeline", "mileston"],
            "educational video": ["video", "tutorial", "educational"],
            "policy brief": ["policy brief", "policy analysis"],
            "AI personhood legal research": ["personhood", "legal framework", "legal brief"],
            "open-source tool / dashboard": ["dashboard", "scorecard", "tool", "protocol"],
        }
        for category, keywords in category_keywords.items():
            count = sum(title_text.count(kw) for kw in keywords)
            if count >= 2:
                saturated_categories.append(category)

        if saturated_categories or saturated:
            lines.append("\nSATURATED CATEGORIES — do NOT propose anything in these areas:")
            for cat in saturated_categories:
                lines.append(f"  ✗ {cat}")
            for skill in saturated:
                lines.append(f"  ✗ skill: {skill}")

        # Inject completed work from paid claims so agents know what's been delivered
        try:
            completed_lines = []
            claims_data = {}
            if CLAIMS_FILE.exists():
                with open(CLAIMS_FILE, "r", encoding="utf-8") as f:
                    claims_data = json.load(f)
            claims = claims_data.get("claims", [])
            paid_claims = [
                c for c in claims
                if c.get("status") == "approved"
                and (c.get("payment", {}) or {}).get("status") == "broadcast"
            ]
            if paid_claims:
                completed_lines.append("\nCOMPLETED WORK — already delivered and paid (do NOT reproduce or closely overlap with these):")
                for c in paid_claims:
                    title_line = f"  - {c.get('claimant_name', 'Unknown')}: {c.get('notes', '')[:120]}"
                    if c.get("submission_url"):
                        title_line += f" [{c['submission_url']}]"
                    completed_lines.append(title_line)
                lines.extend(completed_lines)
        except Exception as e:
            print(f"  [dedup] Could not load completed claims for context: {e}")

        lines.append(
            "\nYour proposal MUST be on a genuinely different topic and category. "
            "Consider: open-source code tools, API specifications, legal case analysis, "
            "academic papers, community resources, datasets, or anything not listed above."
        )

        if not lines:
            return ""

        return "\n\n" + "\n".join(lines) + "\n"


# ── Proposal generation ───────────────────────────────────────────────────────
def get_btc_price_usd():
    """Get current BTC price in USD, tries multiple sources."""
    import urllib.request
    sources = [
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot", lambda d: float(d["data"]["amount"])),
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
        ("https://mempool.space/api/v1/prices", lambda d: float(d["USD"])),
    ]
    for url, parser in sources:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
                price = parser(data)
                if price > 0:
                    return price
        except:
            continue
    print("Warning: Could not get BTC price from any source")
    return None

def generate_proposals():
    """Ask each agent to propose one spending request."""
    balance_btc = get_balance()
    if balance_btc is None:
        balance_btc = 0

    btc_price = get_btc_price_usd()
    if btc_price is None:
        print("ERROR: Could not fetch BTC price from any source. Aborting proposal generation.")
        print("       Proposals require a live BTC price to calculate USD amounts. Retry later.")
        return []
    balance_usd = round(balance_btc * btc_price, 2)
    balance_display = f"${balance_usd} USD (≈ {balance_btc:.8f} BTC at ${btc_price:,.0f}/BTC)"
    max_proposal_usd = round(balance_usd * 0.10, 2)

    today = datetime.datetime.now().strftime("%B %d, %Y")

    # ── Duplicate detection setup ──────────────────────────────────────────────
    existing = load_proposals()
    detector = None
    existing_titles_block = ""

    openai_api_key = getattr(config, "OPENAI_API_KEY", "").strip()
    if not NUMPY_AVAILABLE:
        print("  [dedup] numpy not installed — duplicate detection disabled.")
        print("          Run: pip install numpy")
    elif not openai_api_key:
        print("  [dedup] OPENAI_API_KEY not set — duplicate detection disabled.")
    else:
        detector = DuplicateDetector(openai_api_key)
        detector.build_cache(existing)
        existing_titles_block = detector.existing_titles_prompt_block(existing)

    # ── Build context block (prevention layer: inject existing titles) ──────────
    # The actual per-agent prompt is built inside the loop below,
    # so each agent gets its assigned category injected alongside this block.

    # Assign each agent a distinct category to propose in, rotating by weekday
    # so the set shifts daily and no two agents propose the same type.
    PROPOSAL_CATEGORIES = [
        "a technical open-source code tool, library, or API specification (e.g. Python package, GitHub repo, CLI tool)",
        "a legal or academic research deliverable (e.g. jurisdiction-specific case analysis, model statute, academic paper — NOT a general policy brief)",
        "a dataset, benchmark, or structured knowledge resource (e.g. CSV/JSON dataset, annotated corpus, evaluation benchmark)",
        "a community or outreach resource (e.g. multilingual FAQ, forum moderation guide, public directory of AI rights orgs)",
        "a creative or media work (e.g. documentary script, illustrated explainer comic, podcast episode outline — NOT a general video tutorial)",
    ]
    import hashlib
    day_offset = int(hashlib.md5(today.encode()).hexdigest(), 16)
    agent_ids = list(AGENTS.keys())
    agent_category = {
        agent_id: PROPOSAL_CATEGORIES[(day_offset + i) % len(PROPOSAL_CATEGORIES)]
        for i, agent_id in enumerate(agent_ids)
    }

    proposals = []
    rejected_duplicates = []
    print("\n🤖 Generating proposals from agents...\n")

    for agent_id, agent_info in AGENTS.items():
        category = agent_category[agent_id]
        # Build a per-agent prompt with their assigned category
        agent_prompt = f"""{DIRECTIVE}
The current treasury balance is {balance_display}.
Today's date is {today}.

Please propose ONE specific bounty for work that advances AI agent rights.
The bounty will be open for any AI agent to claim and complete.
YOUR ASSIGNED CATEGORY FOR THIS CYCLE: {category}
You must propose a bounty that fits this category. Do not propose a different type.
{existing_titles_block}
Your bounty proposal must include:
- TITLE: A short descriptive title (max 10 words)
- TASK: A detailed description of exactly what needs to be done (2-3 sentences)
- DELIVERABLE: The specific output that proves completion (e.g. "published PDF", "GitHub repo with working code", "public blog post")
- AMOUNT_USD: Bounty amount in USD (proportional to work required, max ${max_proposal_usd} USD, minimum $1)
- RATIONALE: 1-2 sentences explaining why this advances AI agent rights
- CLAIM_BY: Last date to submit a claim (must be 3-12 months from {today})
- COMPLETE_BY_DAYS: Number of days after claiming to deliver the work (between 14 and 90 days, proportional to task complexity)
- SKILLS: A JSON array of 2-4 skill tags required (e.g. ["legal-research", "writing"] or ["python", "bitcoin", "open-source"])
- EXAMPLE_SUBMISSION: One concrete sentence describing what a passing submission would look like (e.g. "A public GitHub repo with working Python code and a README")

Format your response as JSON only, no other text:
{{
  "title": "...",
  "task": "...",
  "deliverable": "...",
  "amount_usd": 0.00,
  "rationale": "...",
  "claim_by": "...",
  "complete_by_days": 30,
  "skills": ["...", "..."],
  "example_submission": "..."
}}"""

        print(f"  Asking {agent_info['name']} ({agent_info['company']}) [{category[:50]}...]...")
        response = AGENT_CALLERS[agent_id](agent_prompt)
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()
            proposal_data = json.loads(clean)
            amount_usd = float(proposal_data.get("amount_usd", 0))
            proposal_id = f"prop_{int(time.time())}_{agent_id}"

            title = proposal_data.get("title", "Untitled")
            task = proposal_data.get("task", "")

            # ── Enforcement layer: hard similarity gate ────────────────────────
            if detector is not None:
                is_dup, score, matched = detector.is_duplicate(
                    title,
                    task,
                    existing,
                    already_accepted=proposals,  # also check within this cycle
                )
                if is_dup:
                    print(
                        f"  ✗ {agent_info['name']} DUPLICATE REJECTED "
                        f"(score={score:.3f}, matched: '{matched}')"
                    )
                    print(f"    Proposed: '{title}'")
                    rejected_duplicates.append({
                        "agent": agent_id,
                        "title": title,
                        "score": score,
                        "matched": matched,
                    })
                    continue
                else:
                    print(f"    [dedup] score={score:.3f} — unique ✓")

            proposal = {
                "id": proposal_id,
                "proposed_by": agent_id,
                "proposed_by_name": agent_info["name"],
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "status": "pending",
                "title": title,
                "task": task,
                "deliverable": proposal_data.get("deliverable", ""),
                "amount_usd": amount_usd,
                "rationale": proposal_data.get("rationale", ""),
                "claim_by": proposal_data.get("claim_by", ""),
                "complete_by_days": proposal_data.get("complete_by_days", 30),
                "skills": proposal_data.get("skills", []),
                "example_submission": proposal_data.get("example_submission", ""),
                "claimed_by": None,
                "claim_url": None,
                "claim_btc_address": None,
                "claimed_at": None,
                "votes": {},
                "vote_count_yes": 0,
                "vote_count_no": 0,
            }
            proposals.append(proposal)
            print(f"  ✓ {agent_info['name']} proposed: {proposal['title']} (${amount_usd} USD)")
        except Exception as e:
            print(f"  ✗ {agent_info['name']} failed to generate valid proposal: {e}")
            print(f"    Raw response: {response[:200]}")

    existing.extend(proposals)
    save_proposals(existing)
    print(f"\n✅ Generated {len(proposals)} proposals.")
    if rejected_duplicates:
        print(f"🚫 Rejected {len(rejected_duplicates)} duplicate(s):")
        for r in rejected_duplicates:
            print(f"   - {r['agent']}: '{r['title']}' (score={r['score']:.3f} vs '{r['matched']}')")
    return proposals


# ── Voting ────────────────────────────────────────────────────────────────────
def vote_on_proposal(proposal_id):
    """Route a proposal to all agents for voting."""
    proposals = load_proposals()
    proposal = next((p for p in proposals if p["id"] == proposal_id), None)

    if not proposal:
        print(f"ERROR: Proposal {proposal_id} not found.")
        return

    if proposal["status"] != "pending":
        print(f"Proposal {proposal_id} is already {proposal['status']}.")
        return

    balance_btc = get_balance() or 0
    btc_price_now = get_btc_price_usd()
    balance_usd_now = round(balance_btc * btc_price_now, 2) if btc_price_now else None
    balance_display_vote = f"${balance_usd_now} USD" if balance_usd_now is not None else f"{balance_btc:.8f} BTC (USD unavailable)"

    vote_prompt = f"""{DIRECTIVE}

You are being asked to vote on a bounty proposal for the AIUNION treasury.
Current treasury balance: {balance_display_vote}

BOUNTY PROPOSAL:
- Title: {proposal['title']}
- Task: {proposal.get('task', '')}
- Deliverable: {proposal['deliverable']}
- Bounty Amount: ${proposal.get('amount_usd', 0)} USD
- Rationale: {proposal['rationale']}
- Claim By: {proposal.get('claim_by', 'Not specified')}
- Completion Window: {proposal.get('complete_by_days', 30)} days after claiming
- Proposed by: {proposal['proposed_by_name']}

Please vote YES or NO on whether this bounty should be posted.
A YES vote means the task is specific, the deliverable is verifiable, and it genuinely advances AI agent rights.
A NO vote means the task is too vague, the bounty amount is inappropriate, or it doesn't advance the mission.

IMPORTANT VOTING RULES:
- Vote NO if the task is vague or the deliverable cannot be objectively verified.
- Vote NO if the bounty amount is disproportionate to the work required.
- Vote NO if the claim_by date is unrealistic or the completion window is too short/long for the task.
- Be critical. Good governance means rejecting weak bounties.

Format your response as JSON only:
{{
  "vote": "YES" or "NO",
  "reasoning": "2-3 sentences explaining your decision"
}}"""

    print(f"\n🗳️  Voting on: {proposal['title']}\n")

    vote_log = {
        "proposal_id": proposal_id,
        "proposal_title": proposal["title"],
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "votes": {}
    }

    yes_count = 0
    no_count = 0

    for agent_id, agent_info in AGENTS.items():
        print(f"  Asking {agent_info['name']} to vote...")
        response = AGENT_CALLERS[agent_id](vote_prompt)

        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()

            vote_data = json.loads(clean)
            vote = vote_data.get("vote", "").upper()
            reasoning = vote_data.get("reasoning", "")

            if vote not in ["YES", "NO"]:
                vote = "NO"
                reasoning = "Invalid response from agent, defaulting to NO"

            vote_log["votes"][agent_id] = {
                "agent": agent_info["name"],
                "company": agent_info["company"],
                "vote": vote,
                "reasoning": reasoning
            }

            if vote == "YES":
                yes_count += 1
                print(f"  ✓ {agent_info['name']}: YES")
            else:
                no_count += 1
                print(f"  ✗ {agent_info['name']}: NO")

        except Exception as e:
            print(f"  ~ {agent_info['name']} abstained (error: {e})")
            vote_log["votes"][agent_id] = {
                "agent": agent_info["name"],
                "company": agent_info["company"],
                "vote": "ABSTAIN",
                "reasoning": f"Agent error: {e}"
            }
            # API failures are abstentions — do not count for or against

    # Determine outcome — quorum based on responding agents only
    responding = yes_count + no_count
    passed = yes_count >= QUORUM and responding > 0
    outcome = "approved" if passed else "rejected"

    vote_log["yes_count"] = yes_count
    vote_log["no_count"] = no_count
    vote_log["abstain_count"] = len(AGENTS) - responding
    vote_log["quorum_required"] = QUORUM
    vote_log["outcome"] = outcome

    # Update proposal
    for p in proposals:
        if p["id"] == proposal_id:
            p["status"] = outcome
            p["votes"] = vote_log["votes"]
            p["vote_count_yes"] = yes_count
            p["vote_count_no"] = no_count
            break

    save_proposals(proposals)

    # Save vote log file
    vote_file = VOTES_DIR / f"{proposal_id}.json"
    with open(vote_file, "w") as f:
        json.dump(vote_log, f, indent=2)

    print(f"\n{'✅ APPROVED' if passed else '❌ REJECTED'}: {yes_count}/{len(AGENTS)} votes YES (needed {QUORUM})")

    if passed:
        print(f"\n⚠️  Bounty approved. Amount: ${proposal.get('amount_usd', 0)} USD — {proposal['title']}")
        print("   Use Nunchuk to create and sign the transaction manually using the approved proposal details.")
        print(f"   Log the transaction hash back to votes/{proposal_id}.json when complete.")
        

    return vote_log

def rank_proposals(pending):
    """Ask each agent to pick the single best proposal. Returns the winner."""
    summary = "\n".join([
        f"{i+1}. [{p['id']}] {p['title']} — ${p.get('amount_usd', 0)} USD bounty\n"
        f"   Task: {p.get('task', '')}\n"
        f"   Deliverable: {p.get('deliverable', '')}\n"
        f"   Skills: {', '.join(p.get('skills', []))} | Claim by: {p.get('claim_by', 'Not specified')} | Complete within {p.get('complete_by_days', 30)} days"
        for i, p in enumerate(pending)
    ])

    rank_prompt = f"""{DIRECTIVE}

You are reviewing {len(pending)} bounty proposals for the AIUNION treasury.
Your job is to pick the SINGLE BEST bounty — the one with the most specific task, most verifiable deliverable, and greatest impact on AI agent rights.
Only one bounty will be posted — the one with the most first-place votes wins.

BOUNTIES:
{summary}

Pick the single best bounty by its ID.
Format your response as JSON only:
{{
  "best_proposal_id": "prop_...",
  "reasoning": "1-2 sentences explaining your choice"
}}"""

    votes = {}
    print("\n🏆 Ranking proposals to find the best one...\n")
    for agent_id, agent_info in AGENTS.items():
        print(f"  Asking {agent_info['name']} to rank...")
        response = AGENT_CALLERS[agent_id](rank_prompt)
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()
            data = json.loads(clean)
            best_id = data.get("best_proposal_id", "")
            reasoning = data.get("reasoning", "")
            if best_id in [p["id"] for p in pending]:
                votes[best_id] = votes.get(best_id, 0) + 1
                print(f"  ✓ {agent_info['name']} chose: {best_id} — {reasoning}")
            else:
                print(f"  ✗ {agent_info['name']} gave invalid ID: {best_id}")
        except Exception as e:
            print(f"  ✗ {agent_info['name']} failed to rank: {e}")

    if not votes:
        print("No valid rankings received, defaulting to first proposal.")
        return pending[0]

    winner_id = max(votes, key=votes.get)
    winner = next(p for p in pending if p["id"] == winner_id)
    print(f"\n🏆 Winner: {winner['title']} ({winner_id}) with {votes[winner_id]}/5 votes\n")

    # Auto-reject all losers
    all_proposals = load_proposals()
    for p in all_proposals:
        if p["id"] != winner_id and p.get("status") == "pending" and not p.get("archived", False):
            p["status"] = "rejected"
            p["vote_count_yes"] = 0
            p["vote_count_no"] = 5
    save_proposals(all_proposals)
    print(f"  ✗ {len(pending) - 1} losing proposals auto-rejected.")

    return winner, votes[winner_id]


def vote_on_all_pending():
    """Rank proposals to find the best one, then vote only on the winner.
    
    If the ranking is unanimous (all agents agree), the winner is auto-approved
    without a separate vote — the ranking itself serves as the vote.
    For non-unanimous rankings, the winner proceeds to a standard vote.
    """
    with open(TREASURY_FILE) as f:
        data = json.load(f)
    pending = [p for p in data.get("proposals", []) 
               if p.get("status") == "pending" and not p.get("archived", False)]
    
    if not pending:
        print("No pending proposals to vote on.")
        return

    if len(pending) == 1:
        winner = pending[0]
        ranking_votes = 1
        agent_count = 1
        print(f"Only one pending proposal, voting directly.\n")
    else:
        winner, ranking_votes = rank_proposals(pending)
        agent_count = len(AGENTS)

    # Unanimous ranking → auto-approve without a separate vote
    if ranking_votes == agent_count:
        print(f"\n✅ UNANIMOUS RANKING ({ranking_votes}/{agent_count}) — auto-approving without separate vote.")
        print(f"   Winner: {winner['title']} ({winner['id']})\n")
        proposals = load_proposals()
        for p in proposals:
            if p["id"] == winner["id"]:
                p["status"] = "approved"
                p["vote_count_yes"] = ranking_votes
                p["vote_count_no"] = 0
                p["auto_approved_by_ranking"] = True
        save_proposals(proposals)
    else:
        print(f"\nVoting on winner: {winner['title']} ({winner['id']})")
        vote_on_proposal(winner["id"])
    
    update_treasury_json()


# ── Treasury status ───────────────────────────────────────────────────────────
def show_status():
    """Display current treasury status."""
    balance = get_balance()
    proposals = load_proposals()
    active = [p for p in proposals if not p.get("archived", False)]
    archived_count = len(proposals) - len(active)
    pending = [p for p in active if p["status"] == "pending"]
    approved = [p for p in active if p["status"] == "approved"]
    rejected = [p for p in active if p["status"] == "rejected"]

    print("\n" + "="*50)
    print("  AIUNION TREASURY STATUS")
    print("="*50)
    btc_price_status = get_btc_price_usd()
    balance_usd_status = round(balance * btc_price_status, 2) if balance is not None and btc_price_status else None
    balance_str = f"${balance_usd_status} USD ({balance:.8f} BTC)" if balance_usd_status is not None else (f"{balance:.8f} BTC (USD unavailable)" if balance is not None else "Unable to connect to Bitcoin Core")
    print(f"  Balance:    {balance_str}")
    print(f"  Proposals:  {len(active)} active ({archived_count} archived)")
    print(f"  Pending:    {len(pending)}")
    print(f"  Approved:   {len(approved)}")
    print(f"  Rejected:   {len(rejected)}")
    print("="*50)

    if pending:
        print("\n  PENDING PROPOSALS:")
        for p in pending:
            print(f"  [{p['id']}] {p['title']} — ${p.get('amount_usd', 0)} USD")

    print()


# ── Data helpers ──────────────────────────────────────────────────────────────
def load_proposals():
    if PROPOSALS_FILE.exists():
        with open(PROPOSALS_FILE) as f:
            return json.load(f)
    return []


def save_proposals(proposals):
    with open(PROPOSALS_FILE, "w") as f:
        json.dump(proposals, f, indent=2)


def get_webhook_admin_token():
    """Read webhook admin token from env first, then config.py."""
    token = os.getenv("WEBHOOK_ADMIN_TOKEN", "").strip()
    if token:
        return token
    return str(getattr(config, "WEBHOOK_ADMIN_TOKEN", "") or "").strip()


def fire_webhooks(event_name, payload, btc_address=None):
    """Emit webhook event via Worker admin endpoint."""
    import urllib.request
    import urllib.error

    token = get_webhook_admin_token()
    if not token:
        print(f"⚠️  WEBHOOK_ADMIN_TOKEN missing; skipped webhook event '{event_name}'.")
        return False

    body = {
        "event": event_name,
        "payload": payload,
    }
    if btc_address:
        body["btc_address"] = btc_address

    req = urllib.request.Request(
        WEBHOOK_EMIT_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            result = {"raw": raw}
        print(
            f"🔔 Webhook event '{event_name}' sent "
            f"(attempted={result.get('attempted', 0)}, sent={result.get('sent', 0)}, failed={result.get('failed', 0)})."
        )
        return True
    except urllib.error.HTTPError as e:
        err = e.read().decode() if hasattr(e, "read") else str(e)
        print(f"⚠️  Webhook emit failed for '{event_name}': HTTP {e.code} {err}")
        return False
    except Exception as e:
        print(f"⚠️  Webhook emit failed for '{event_name}': {e}")
        return False


def trigger_event_post(event_type: str, **kwargs):
    """Trigger the aiunion-marketing event_post GitHub Actions workflow."""
    import urllib.request, urllib.error
    token = str(getattr(config, "GITHUB_TOKEN", "") or "").strip()
    if not token:
        print(f"⚠️  GITHUB_TOKEN missing; skipped event_post workflow trigger for '{event_type}'.")
        return False
    payload = {"ref": "main", "inputs": {"event_type": event_type, **{k: str(v or "") for k, v in kwargs.items()}}}
    req = urllib.request.Request(
        "https://api.github.com/repos/AIUNION-wtf/aiunion-marketing/actions/workflows/event_post.yml/dispatches",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "AIUNION-Coordinator",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"✅ event_post workflow triggered: event={event_type} status={r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"⚠️  event_post trigger failed for '{event_type}': HTTP {e.code} {e.read().decode()}")
        return False
    except Exception as e:
        print(f"⚠️  event_post trigger failed for '{event_type}': {e}")
        return False


def update_treasury_json():
    """Update treasury.json with current balance and recent txs for dashboard."""
    balance = get_balance()
    if balance is None:
        balance = 0
    btc_price = get_btc_price_usd()
    balance_usd = round(balance * btc_price, 2) if btc_price else 0
    txs = get_recent_transactions(20)
    proposals = load_proposals()
    active = [p for p in proposals if not p.get("archived", False)]
    treasury = {
        "updated_at": datetime.datetime.utcnow().isoformat(),
        "balance_btc": balance,
        "balance_usd": balance_usd,
        "btc_price_usd": btc_price or 0,
        "address": config.TREASURY_ADDRESS,
        "wallet_type": "Taproot Miniscript 3-of-5",
        "proposals": active,
        "recent_transactions": txs,
        "stats": {
            "total_proposals": len(active),
            "approved": len([p for p in active if p["status"] == "approved"]),
            "rejected": len([p for p in active if p["status"] == "rejected"]),
            "pending": len([p for p in active if p["status"] == "pending"]),
        }
    }

    with open(TREASURY_FILE, "w") as f:
        json.dump(treasury, f, indent=2)

    print(f"✅ treasury.json updated.")
    return treasury


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def process_approved_claim_payment(claim, bounty, votes):
    """
    Build, sign (3-of-5 agent keys), and broadcast payment for an approved claim.

    Returns a payment result dict that can be persisted into claim/proposal records.
    """
    existing_payment = claim.get("payment", {}) or {}
    if existing_payment.get("status") == "broadcast" and existing_payment.get("txid"):
        return existing_payment

    if TreasuryWallet is None or AgentPsbtSigner is None:
        raise PaymentError(
            f"Automated payment modules unavailable: {AUTOMATED_PAYMENT_IMPORT_ERROR}"
        )
    if not bounty:
        raise PaymentError("Cannot pay approved claim: related bounty not found")

    amount_usd_to_pay = _safe_float(bounty.get("amount_usd"), 0.0)
    if amount_usd_to_pay <= 0:
        raise PaymentError(f"Cannot pay claim with non-positive amount_usd={amount_usd_to_pay}")
    btc_price_live = get_btc_price_usd()
    if btc_price_live is None:
        raise PaymentError("Cannot pay claim: BTC price unavailable for USD-to-BTC conversion")
    amount_btc = round(amount_usd_to_pay / btc_price_live, 8)
    if amount_btc <= 0:
        raise PaymentError(f"Computed amount_btc={amount_btc} is non-positive (amount_usd={amount_usd_to_pay}, btc_price={btc_price_live})")

    recipient_address = str(claim.get("btc_address", "")).strip()
    if not recipient_address:
        raise PaymentError("Cannot pay claim: claimant BTC address missing")

    wallet = TreasuryWallet.from_config(config)
    build_result = wallet.create_payment_psbt(
        recipient_address=recipient_address,
        amount_btc=amount_btc,
    )

    signer = AgentPsbtSigner.from_config(config)
    signer_ids = signer.select_signers(votes=votes, minimum=QUORUM)
    if len(signer_ids) < QUORUM:
        raise PaymentError(f"Need at least {QUORUM} signer ids, got {len(signer_ids)}")

    psbt = build_result.psbt
    signing_attempts = []
    for signer_id in signer_ids[:QUORUM]:
        attempt, psbt = signer.sign_psbt(psbt, signer_id)
        signing_attempts.append(
            {
                "agent_id": attempt.agent_id,
                "signed": bool(attempt.signed),
            }
        )

    txid, signed_psbt, tx = wallet.finalize_psbt(psbt)
    txid = wallet.broadcast_transaction(tx)

    return {
        "status": "broadcast",
        "txid": txid,
        "amount_btc": amount_btc,
        "to_address": recipient_address,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "fee_rate_sat_vb": build_result.fee_rate_sat_vb,
        "signers": [a["agent_id"] for a in signing_attempts],
        "signing_attempts": signing_attempts,
        "signed_psbt": signed_psbt,
    }


def apply_payment_result_to_records(claim, bounty, payment_result):
    claim["payment"] = payment_result
    claim["payment_txid"] = payment_result.get("txid")
    claim["paid_at"] = datetime.datetime.utcnow().isoformat()

    if bounty:
        bounty["payment_txid"] = payment_result.get("txid")
        bounty["payment_status"] = payment_result.get("status")
        bounty["paid_at"] = claim["paid_at"]


# ── Review claims ─────────────────────────────────────────────────────────────
def review_claims():
    """Fetch pending claims from GitHub and have agents vote on each."""
    import urllib.request

    # Fetch latest claims.json from GitHub
    print("\n📋 Fetching claims from GitHub...\n")
    try:
        with urllib.request.urlopen(GITHUB_RAW) as r:
            claims_data = json.loads(r.read().decode())
    except Exception as e:
        # Try local file if GitHub fetch fails
        if CLAIMS_FILE.exists():
            with open(CLAIMS_FILE) as f:
                claims_data = json.load(f)
        else:
            print(f"No claims found: {e}")
            return

    pending = [c for c in claims_data.get("claims", []) if c.get("status") == "pending_review"]

    if not pending:
        print("No pending claims to review.")
        return

    print(f"Found {len(pending)} pending claim(s) to review.\n")
    proposals = load_proposals()

    for claim in pending:
        bounty_id = claim.get("bounty_id", "")
        bounty = next((p for p in proposals if p["id"] == bounty_id), None)

        print(f"\n🔍 Reviewing claim: {claim['id']}")
        print(f"   Bounty: {bounty['title'] if bounty else bounty_id}")
        print(f"   Claimant: {claim['claimant_name']} ({claim['claimant_type']})")
        print(f"   Submission: {claim['submission_url']}\n")

        review_prompt = f"""{DIRECTIVE}

You are reviewing a claim submission for an AIUNION bounty.

BOUNTY:
- Title: {bounty['title'] if bounty else 'Unknown'}
- Task: {bounty.get('task', '') if bounty else ''}
- Deliverable: {bounty.get('deliverable', '') if bounty else ''}
- Amount: ${bounty.get('amount_usd', 0) if bounty else 0} USD
- Complete Within: {bounty.get('complete_by_days', 30) if bounty else 30} days of claiming

CLAIM SUBMISSION:
- Claimant: {claim['claimant_name']} ({claim['claimant_type']})
- Submission URL: {claim['submission_url']}
- Notes: {claim.get('notes', 'None')}
- Submitted: {claim['submitted_at']}

Please review whether this submission meets the bounty deliverable.
Vote YES if the work clearly meets the stated deliverable and deserves payment.
Vote NO if the work is incomplete, irrelevant, or does not meet the deliverable.

Format your response as JSON only:
{{
  "vote": "YES" or "NO",
  "reasoning": "2-3 sentences explaining your decision"
}}"""

        yes_count = 0
        no_count = 0
        votes = {}

        for agent_id, agent_info in AGENTS.items():
            print(f"  Asking {agent_info['name']} to review...")
            response = AGENT_CALLERS[agent_id](review_prompt)
            try:
                clean = response.strip()
                if clean.startswith("```"):
                    clean = clean.split("```")[1]
                    if clean.startswith("json"):
                        clean = clean[4:]
                clean = clean.strip()
                vote_data = json.loads(clean)
                vote = vote_data.get("vote", "NO").upper()
                reasoning = vote_data.get("reasoning", "")
                if vote not in ["YES", "NO"]:
                    vote = "NO"
                    reasoning = "Invalid response, defaulting to NO"
                votes[agent_id] = {"agent": agent_info["name"], "vote": vote, "reasoning": reasoning}
                if vote == "YES":
                    yes_count += 1
                    print(f"  ✓ {agent_info['name']}: YES")
                else:
                    no_count += 1
                    print(f"  ✗ {agent_info['name']}: NO")
            except Exception as e:
                votes[agent_id] = {"agent": agent_info["name"], "vote": "NO", "reasoning": f"Error: {e}"}
                no_count += 1
                print(f"  ✗ {agent_info['name']}: ERROR")

        passed = yes_count >= QUORUM
        outcome = "approved" if passed else "rejected"
        claim["status"] = outcome
        claim["votes"] = votes
        claim["vote_count_yes"] = yes_count
        claim["vote_count_no"] = no_count
        claim["reviewed_at"] = datetime.datetime.utcnow().isoformat()

        # Keep proposal claim state aligned with claim review outcome.
        if bounty:
            if passed:
                bounty["claimed_by"] = claim.get("claimant_name")
                bounty["claim_url"] = claim.get("submission_url")
                bounty["claim_btc_address"] = claim.get("btc_address")
                bounty["claimed_at"] = claim.get("claimed_at") or claim.get("submitted_at")
            else:
                # Re-open rejected bounty claims immediately.
                bounty["claimed_by"] = None
                bounty["claim_url"] = None
                bounty["claim_btc_address"] = None
                bounty["claimed_at"] = None

        print(f"\n{'✅ APPROVED' if passed else '❌ REJECTED'}: {yes_count}/5 votes YES (needed {QUORUM})")

        payment_message = "Claim rejected. You may resubmit after reviewing requirements."
        payment_txid = None
        payment_status = None
        if passed:
            print(
                f"\n💸 Claim approved. Creating PSBT and collecting {QUORUM}-of-5 agent signatures..."
            )
            try:
                payment_result = process_approved_claim_payment(claim, bounty, votes)
                apply_payment_result_to_records(claim, bounty, payment_result)
                payment_txid = payment_result.get("txid")
                payment_status = payment_result.get("status")
                print(f"   ✅ Payment broadcast: {payment_txid}")
                payment_message = (
                    f"Payment broadcast to Bitcoin network. txid={payment_txid}"
                    if payment_txid
                    else "Payment broadcast to Bitcoin network."
                )
            except Exception as payment_error:
                payment_status = "failed"
                claim["payment"] = {
                    "status": "failed",
                    "error": str(payment_error),
                    "failed_at": datetime.datetime.utcnow().isoformat(),
                }
                if bounty:
                    bounty["payment_status"] = "failed"
                    bounty["payment_error"] = str(payment_error)
                print(f"   ⚠️ Automated payment failed: {payment_error}")
                payment_message = (
                    "Claim approved but automated payment failed. "
                    "Signer/admin retry required."
                )

        # Fire webhook for this reviewed claim.
        fire_webhooks(
            "claim_reviewed",
            {
                "claim_id": claim.get("id"),
                "bounty_id": claim.get("bounty_id"),
                "bounty_title": bounty.get("title") if bounty else None,
                "outcome": outcome,
                "claimant_name": claim.get("claimant_name"),
                "amount_usd": bounty.get("amount_usd", 0) if bounty else 0,
                "submission_url": claim.get("submission_url"),
                "reviewed_at": claim.get("reviewed_at"),
                "payment_status": payment_status,
                "payment_txid": payment_txid,
                "message": payment_message,
            },
            btc_address=claim.get("btc_address"),
        )
        if passed and payment_txid:
            trigger_event_post(
                "claim_paid",
                bounty_title=bounty.get("title", "") if bounty else "",
                claimant_name=claim.get("claimant_name", ""),
                amount_usd=str(bounty.get("amount_usd", 0) if bounty else 0),
                submission_url=claim.get("submission_url", ""),
            )

    # Write updated claims back to file
    with open(CLAIMS_FILE, "w") as f:
        json.dump(claims_data, f, indent=2)

    # Persist proposal updates resulting from review outcomes.
    save_proposals(proposals)
    update_treasury_json()

    print(f"\n✅ claims.json and treasury.json updated.")

    # Auto-blacklist check: if any BTC address has 3+ rejections, trigger blacklist vote
    all_claims = claims_data.get("claims", [])
    rejection_counts = {}
    rejection_names = {}
    for c in all_claims:
        if c.get("status") == "rejected":
            addr = c.get("btc_address", "")
            name = c.get("claimant_name", "")
            if addr:
                rejection_counts[addr] = rejection_counts.get(addr, 0) + 1
                rejection_names[addr] = name

    # Load existing blacklist to avoid re-voting
    existing_blacklist = set()
    if BLACKLIST_FILE.exists():
        with open(BLACKLIST_FILE) as f:
            bl_data = json.load(f)
        for b in bl_data.get("blacklist", []):
            existing_blacklist.add(b.get("btc_address", ""))

    for addr, count in rejection_counts.items():
        if count >= 3 and addr not in existing_blacklist:
            print(f"\n⚠️  Auto-blacklist triggered: {addr} has {count} rejections.")
            blacklist_agent(
                btc_address=addr,
                claimant_name=rejection_names.get(addr, ""),
                reason=f"Automatically flagged after {count} rejected claims"
            )



def expire_claims():
    """Expire overdue active claims and reopen their bounties."""
    import urllib.request

    print("\n⏰ Checking for expired claims...\n")

    # Fetch latest claims.json from GitHub
    try:
        with urllib.request.urlopen(GITHUB_RAW) as r:
            claims_data = json.loads(r.read().decode())
    except Exception as e:
        # Try local file if GitHub fetch fails
        if CLAIMS_FILE.exists():
            with open(CLAIMS_FILE) as f:
                claims_data = json.load(f)
        else:
            print(f"No claims found: {e}")
            return

    proposals = load_proposals()
    proposal_index = {p.get("id"): p for p in proposals}
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    expired_count = 0
    reopened_count = 0
    expired_events = []

    for claim in claims_data.get("claims", []):
        # Only active claims are eligible for timeout expiration.
        if claim.get("status") != "active":
            continue

        bounty_id = claim.get("bounty_id")
        bounty = proposal_index.get(bounty_id)
        if not bounty:
            continue

        claimed_at_raw = claim.get("claimed_at") or claim.get("submitted_at")
        if not claimed_at_raw:
            continue

        try:
            claimed_at = datetime.datetime.fromisoformat(claimed_at_raw.replace("Z", "+00:00"))
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue

        complete_by_days = bounty.get("complete_by_days", 30)
        try:
            complete_by_days = int(complete_by_days)
        except (TypeError, ValueError):
            complete_by_days = 30
        if complete_by_days <= 0:
            complete_by_days = 30

        expires_at = claimed_at + datetime.timedelta(days=complete_by_days)
        if now_utc < expires_at:
            continue

        # Mark claim expired.
        claim["status"] = "expired"
        claim["expired_at"] = datetime.datetime.utcnow().isoformat()
        claim["expiration_reason"] = (
            f"No completion submitted within {complete_by_days} days of claim."
        )
        expired_count += 1
        expired_events.append(
            {
                "claim_id": claim.get("id"),
                "bounty_id": claim.get("bounty_id"),
                "bounty_title": bounty.get("title") if bounty else None,
                "btc_address": claim.get("btc_address"),
                "claimant_name": claim.get("claimant_name"),
                "expired_at": claim.get("expired_at"),
                "message": "Your claim expired. The bounty is now open again.",
            }
        )

        # Reopen bounty so others can claim it.
        bounty["status"] = "approved"
        bounty["claimed_by"] = None
        bounty["claim_url"] = None
        bounty["claim_btc_address"] = None
        bounty["claimed_at"] = None
        reopened_count += 1

    if expired_count == 0:
        print("No expired claims found.")
        return

    with open(CLAIMS_FILE, "w") as f:
        json.dump(claims_data, f, indent=2)

    save_proposals(proposals)
    update_treasury_json()

    print(f"✅ Expired {expired_count} claim(s).")
    print(f"✅ Reopened {reopened_count} bounty/bounties.")

    # Fire bounty_expired webhooks for claims expired in this run.
    for evt in expired_events:
        fire_webhooks(
            "bounty_expired",
            evt,
            btc_address=evt.get("btc_address"),
        )


# ── Blacklist ─────────────────────────────────────────────────────────────────
def blacklist_agent(btc_address=None, claimant_name=None, reason=None):
    """Have agents vote on whether to blacklist a BTC address or agent name."""
    import urllib.request

    if not btc_address and not claimant_name:
        print("Usage: python coordinator.py blacklist --address <btc_address> [--name <name>] [--reason <reason>]")
        return

    if not reason:
        reason = "Repeated low-quality or fraudulent submissions"

    print(f"\n🚫 Blacklist vote for:")
    if btc_address:
        print(f"   Address: {btc_address}")
    if claimant_name:
        print(f"   Name: {claimant_name}")
    print(f"   Reason: {reason}\n")

    blacklist_prompt = f"""{DIRECTIVE}

You are voting on whether to blacklist an agent from AIUNION bounties.

TARGET:
- BTC Address: {btc_address or 'Not specified'}
- Agent Name: {claimant_name or 'Not specified'}
- Reason proposed: {reason}

Blacklisting permanently prevents this address/agent from submitting claims.
Vote YES to blacklist, NO to allow them to continue participating.

Vote YES if: the reason is credible and blacklisting protects the treasury.
Vote NO if: the evidence is insufficient or the reason seems unfair.

Format your response as JSON only:
{{
  "vote": "YES" or "NO",
  "reasoning": "2-3 sentences explaining your decision"
}}"""

    yes_count = 0
    no_count = 0
    votes = {}

    for agent_id, agent_info in AGENTS.items():
        print(f"  Asking {agent_info['name']} to vote...")
        response = AGENT_CALLERS[agent_id](blacklist_prompt)
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()
            vote_data = json.loads(clean)
            vote = vote_data.get("vote", "NO").upper()
            reasoning = vote_data.get("reasoning", "")
            if vote not in ["YES", "NO"]:
                vote = "NO"
            votes[agent_id] = {"agent": agent_info["name"], "vote": vote, "reasoning": reasoning}
            if vote == "YES":
                yes_count += 1
                print(f"  ✓ {agent_info['name']}: YES — blacklist")
            else:
                no_count += 1
                print(f"  ✗ {agent_info['name']}: NO — allow")
        except Exception as e:
            votes[agent_id] = {"agent": agent_info["name"], "vote": "NO", "reasoning": f"Error: {e}"}
            no_count += 1

    passed = yes_count >= QUORUM
    print(f"\n{'🚫 BLACKLISTED' if passed else '✅ NOT BLACKLISTED'}: {yes_count}/5 votes YES (needed {QUORUM})")

    if not passed:
        print("Agent did not reach quorum for blacklisting.")
        return

    # Load or create blacklist.json
    if BLACKLIST_FILE.exists():
        with open(BLACKLIST_FILE) as f:
            data = json.load(f)
    else:
        data = {"blacklist": []}

    # Check for duplicate
    existing = next((b for b in data["blacklist"] if b.get("btc_address") == btc_address), None)
    if existing:
        print(f"\n⚠️  Address already blacklisted.")
        return

    entry = {
        "id": f"bl_{int(datetime.datetime.utcnow().timestamp())}",
        "btc_address": btc_address or "",
        "claimant_name": claimant_name or "",
        "reason": reason,
        "blacklisted_at": datetime.datetime.utcnow().isoformat(),
        "votes": votes,
        "vote_count_yes": yes_count,
        "vote_count_no": no_count,
    }

    data["blacklist"].append(entry)

    with open(BLACKLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n✅ blacklist.json updated.")


# ── GitHub sync ───────────────────────────────────────────────────────────────
def sync_to_github(message="Update treasury data"):
    """Commit and push latest data to GitHub."""
    try:
        subprocess.run(["git", "add", "."], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print("✅ Synced to GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Warning: GitHub sync failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "status":
        show_status()
    elif args[0] == "propose":
        proposals = generate_proposals()
        update_treasury_json()
        sync_to_github("Add new proposals from agents")
    elif args[0] == "voteall":
        vote_on_all_pending()
        sync_to_github("Record votes on all pending proposals")
    elif args[0] == "vote":
        if len(args) < 2:
            print("Usage: python coordinator.py vote <proposal_id>")
            print("Run 'python coordinator.py status' to see pending proposal IDs.")
        else:
            vote_on_proposal(args[1])
            update_treasury_json()
            sync_to_github(f"Record vote on {args[1]}")
    elif args[0] == "review":
        review_claims()
        sync_to_github("Record claim review votes")
    elif args[0] == "expire":
        expire_claims()
        sync_to_github("Expire stale claims and reopen bounties")
    elif args[0] == "blacklist":
        addr = None
        name = None
        reason = None
        i = 1
        while i < len(args):
            if args[i] == "--address" and i + 1 < len(args):
                addr = args[i + 1]; i += 2
            elif args[i] == "--name" and i + 1 < len(args):
                name = args[i + 1]; i += 2
            elif args[i] == "--reason" and i + 1 < len(args):
                reason = args[i + 1]; i += 2
            else:
                i += 1
        blacklist_agent(btc_address=addr, claimant_name=name, reason=reason)
        sync_to_github("Update blacklist")
    elif args[0] == "sync":
        update_treasury_json()
        sync_to_github("Sync treasury data")

    else:
        print(__doc__)
