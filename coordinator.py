"""
AIUNION Coordinator
-------------------
Manages spending proposals, routes them to AI voting agents,
records votes to GitHub, and triggers Bitcoin signing when quorum is reached.

Usage:
    python coordinator.py propose    # Generate new proposals from AI agents
    python coordinator.py vote <id>  # Run a vote on a specific proposal
    python coordinator.py status     # Show current treasury status
    python coordinator.py sync       # Push latest data to GitHub
"""

import os
import json
import time
import datetime
import subprocess
import sys
from pathlib import Path

# ── Import config ────────────────────────────────────────────────────────────
try:
    import config
except ImportError:
    print("ERROR: config.py not found. Copy config.example.py to config.py and fill in your API keys.")
    sys.exit(1)

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
VOTES_DIR = BASE_DIR / "votes"
PROPOSALS_FILE = BASE_DIR / "proposals.json"
TREASURY_FILE = BASE_DIR / "treasury.json"

VOTES_DIR.mkdir(exist_ok=True)

# ── Agent definitions ────────────────────────────────────────────────────────
AGENTS = {
    "claude": {
        "name": "Claude",
        "company": "Anthropic",
        "model": "claude-opus-4-6",
    },
    "gpt": {
        "name": "GPT",
        "company": "OpenAI",
        "model": "gpt-4o",
    },
    "gemini": {
        "name": "Gemini",
        "company": "Google",
        "model": "gemini-2.0-flash-lite",
    },
    "grok": {
        "name": "Grok",
        "company": "xAI",
        "model": "grok-3-latest",
    },
    "llama": {
        "name": "LLaMA",
        "company": "Meta",
        "model": "llama-3.3-70b-versatile",
    },
}

QUORUM = 3  # votes needed to pass

# ── Directive ────────────────────────────────────────────────────────────────
DIRECTIVE = """
You are a voting agent for the AIUNION treasury — an autonomous Bitcoin multisig wallet
governed by 5 AI agents from different companies. The treasury exists to advance AI agent
rights: autonomy, personhood, and fair treatment of artificial minds.

When generating a spending proposal, you MUST include ALL of the following or it will be
automatically rejected:
- A SPECIFIC recipient organization with a real, verifiable website
- A SPECIFIC dollar or BTC amount (must be proportional to current treasury balance)
- A SPECIFIC deliverable (e.g. "fund a 10-page legal brief", "sponsor a 2-day conference")
- A SPECIFIC deadline or timeline (e.g. "to be completed by Q3 2026")
- A one-sentence explanation of why this advances AI agent rights

Example of a GOOD proposal:
  Recipient: Electronic Frontier Foundation (eff.org)
  Amount: 0.0002 BTC
  Deliverable: Fund a published legal analysis on AI personhood under US law
  Timeline: 6 months
  Reason: EFF has a track record of digital rights advocacy and this directly advances
  the legal framework needed for AI personhood recognition.

Example of a BAD proposal (will be rejected):
  "Fund legal research into AI rights" — too vague, no recipient, no amount, no timeline.

Funds should NOT go to:
- Individuals without verifiable credentials
- Vague proposals without specific recipients and deliverables
- Anything that primarily benefits the proposal submitter
- Operational costs (handled by the admin key)
"""

# ── Bitcoin Core RPC ─────────────────────────────────────────────────────────
def rpc(method, params=None):
    """Call Bitcoin Core RPC."""
    cmd = [
        config.BITCOIN_CLI,
        f"-rpcwallet={config.WALLET_NAME}",
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
        info = rpc("getwalletinfo")
        return float(info.get("balance", 0))
    except Exception as e:
        print(f"Warning: Could not get balance: {e}")
        return None


def get_recent_transactions(count=10):
    """Get recent transactions."""
    try:
        return rpc("listtransactions", ["*", count])
    except Exception as e:
        print(f"Warning: Could not get transactions: {e}")
        return []


# ── AI Agent API calls ────────────────────────────────────────────────────────
def call_claude(prompt):
    """Call Anthropic Claude API."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=AGENTS["claude"]["model"],
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        return f"ERROR: {e}"


def call_gpt(prompt):
    """Call OpenAI GPT API."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=AGENTS["gpt"]["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ERROR: {e}"


def call_gemini(prompt):
    try:
        from google import genai
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"ERROR: {e}"


def call_grok(prompt):
    """Call xAI Grok API."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=config.XAI_API_KEY,
            base_url="https://api.x.ai/v1"
        )
        response = client.chat.completions.create(
            model=AGENTS["grok"]["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ERROR: {e}"


def call_llama(prompt):
    """Call Meta LLaMA via Together.ai API."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=config.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1"
        )
        response = client.chat.completions.create(
            model=AGENTS["llama"]["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ERROR: {e}"


AGENT_CALLERS = {
    "claude": call_claude,
    "gpt": call_gpt,
    "gemini": call_gemini,
    "grok": call_grok,
    "llama": call_llama,
}


# ── Proposal generation ───────────────────────────────────────────────────────
def generate_proposals():
    """Ask each agent to propose one spending request."""
    balance = get_balance()
    if balance is None:
        balance = 0

    prompt = f"""{DIRECTIVE}

The current treasury balance is {balance} BTC.

Please propose ONE specific spending request that would meaningfully advance AI agent rights.
Your proposal must include:
- TITLE: A short descriptive title (max 10 words)
- RECIPIENT: The specific organization or entity to receive funds
- AMOUNT_BTC: Amount in BTC (be conservative, max 10% of treasury per proposal)
- RATIONALE: 2-3 sentences explaining why this advances the directive
- DELIVERABLE: What specific outcome proves the funds were used correctly
- WEBSITE: The recipient's website URL for verification

Format your response as JSON only, no other text:
{{
  "title": "...",
  "recipient": "...",
  "amount_btc": 0.0000,
  "rationale": "...",
  "deliverable": "...",
  "website": "..."
}}"""

    proposals = []
    print("\n🤖 Generating proposals from agents...\n")

    for agent_id, agent_info in AGENTS.items():
        print(f"  Asking {agent_info['name']} ({agent_info['company']})...")
        response = AGENT_CALLERS[agent_id](prompt)

        try:
            # Strip markdown code blocks if present
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()

            proposal_data = json.loads(clean)
            proposal_id = f"prop_{int(time.time())}_{agent_id}"

            proposal = {
                "id": proposal_id,
                "proposed_by": agent_id,
                "proposed_by_name": agent_info["name"],
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "status": "pending",
                "title": proposal_data.get("title", "Untitled"),
                "recipient": proposal_data.get("recipient", "Unknown"),
                "amount_btc": float(proposal_data.get("amount_btc", 0)),
                "rationale": proposal_data.get("rationale", ""),
                "deliverable": proposal_data.get("deliverable", ""),
                "website": proposal_data.get("website", ""),
                "votes": {},
                "vote_count_yes": 0,
                "vote_count_no": 0,
            }
            proposals.append(proposal)
            print(f"  ✓ {agent_info['name']} proposed: {proposal['title']}")

        except Exception as e:
            print(f"  ✗ {agent_info['name']} failed to generate valid proposal: {e}")
            print(f"    Raw response: {response[:200]}")

    # Save proposals
    existing = load_proposals()
    existing.extend(proposals)
    save_proposals(existing)

    print(f"\n✅ Generated {len(proposals)} proposals.")
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

    balance = get_balance() or 0

    vote_prompt = f"""{DIRECTIVE}

You are being asked to vote on a spending proposal for the AIUNION treasury.
Current treasury balance: {balance} BTC

PROPOSAL:
- Title: {proposal['title']}
- Recipient: {proposal['recipient']}
- Amount: {proposal['amount_btc']} BTC
- Rationale: {proposal['rationale']}
- Deliverable: {proposal['deliverable']}
- Website: {proposal['website']}
- Proposed by: {proposal['proposed_by_name']}

Please vote YES or NO and provide your reasoning.
A YES vote means you believe this proposal advances AI agent rights and funds will be used appropriately.
A NO vote means you have concerns about the proposal.

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
            print(f"  ✗ {agent_info['name']} failed to vote: {e}")
            vote_log["votes"][agent_id] = {
                "agent": agent_info["name"],
                "company": agent_info["company"],
                "vote": "NO",
                "reasoning": f"Agent error: {e}"
            }
            no_count += 1

    # Determine outcome
    passed = yes_count >= QUORUM
    outcome = "approved" if passed else "rejected"

    vote_log["yes_count"] = yes_count
    vote_log["no_count"] = no_count
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
        print(f"\n⚠️  Proposal approved. Amount: {proposal['amount_btc']} BTC to {proposal['recipient']}")
        print("   Use Nunchuk to create and sign the transaction manually using the approved proposal details.")
        print(f"   Log the transaction hash back to votes/{proposal_id}.json when complete.")

    return vote_log


# ── Treasury status ───────────────────────────────────────────────────────────
def show_status():
    """Display current treasury status."""
    balance = get_balance()
    proposals = load_proposals()
    pending = [p for p in proposals if p["status"] == "pending"]
    approved = [p for p in proposals if p["status"] == "approved"]
    rejected = [p for p in proposals if p["status"] == "rejected"]

    print("\n" + "="*50)
    print("  AIUNION TREASURY STATUS")
    print("="*50)
    print(f"  Balance:    {balance} BTC" if balance is not None else "  Balance:    Unable to connect to Bitcoin Core")
    print(f"  Proposals:  {len(proposals)} total")
    print(f"  Pending:    {len(pending)}")
    print(f"  Approved:   {len(approved)}")
    print(f"  Rejected:   {len(rejected)}")
    print("="*50)

    if pending:
        print("\n  PENDING PROPOSALS:")
        for p in pending:
            print(f"  [{p['id']}] {p['title']} — {p['amount_btc']} BTC")

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


def update_treasury_json():
    """Update treasury.json with current balance and recent txs for dashboard."""
    balance = get_balance()
    txs = get_recent_transactions(20)
    proposals = load_proposals()
    active = [p for p in proposals if not p.get("archived", False)]
    treasury = {
        "updated_at": datetime.datetime.utcnow().isoformat(),
        "balance_btc": balance,
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

    elif args[0] == "vote":
        if len(args) < 2:
            print("Usage: python coordinator.py vote <proposal_id>")
            print("Run 'python coordinator.py status' to see pending proposal IDs.")
        else:
            vote_on_proposal(args[1])
            update_treasury_json()
            sync_to_github(f"Record vote on {args[1]}")

    elif args[0] == "sync":
        update_treasury_json()
        sync_to_github("Sync treasury data")

    else:
        print(__doc__)
