"""
agent.py
AIUNION Marketing Agent — main orchestrator.
Runs on a schedule via GitHub Actions.
Fetches live data from public API, generates post via Grok, posts to X.

Security:
- All secrets via environment variables only
- State file tracks posted items to prevent duplicates
- No sensitive data in logs
- Fails closed on any missing secret
"""

import os
import json
import random
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from grok_client import generate_post
from twitter_client import post_tweet
from aiunion_client import get_open_bounties, get_treasury_status, get_recent_proposals

# ── Logging (rule #8: no sensitive data) ─────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("aiunion.agent")

STATE_FILE = Path("state.json")  # tracked in .gitignore
MAX_DAILY_POSTS = 5


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            logger.warning("Could not read state file, starting fresh")
    return {"posted_bounty_ids": [], "posts_today": 0, "last_post_date": ""}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def reset_daily_count_if_needed(state: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_post_date") != today:
        state["posts_today"] = 0
        state["last_post_date"] = today
    return state


# ── Post type selection ───────────────────────────────────────────────────────

def build_bounty_prompt(bounty: dict) -> str:
    reward_sats = int(bounty["reward_btc"] * 1e8)
    return (
        f"Write a tweet announcing this open bounty on AIUNION:\n"
        f"Title: {bounty['title']}\n"
        f"Reward: {reward_sats:,} sats ({bounty['reward_btc']} BTC)\n"
        f"Description: {bounty['description']}\n"
        f"Link: https://aiunion.wtf\n"
        f"Make it compelling for developers and crypto builders."
    )


def build_treasury_prompt(status: dict) -> str:
    return (
        f"Write a tweet giving a treasury update for AIUNION:\n"
        f"Current balance: {status['balance_btc']} BTC\n"
        f"Open bounties: {status['open_bounties']}\n"
        f"Total proposals voted on: {status['total_proposals']} "
        f"({status['approved']} approved)\n"
        f"Link: https://aiunion.wtf\n"
        f"Keep it factual and interesting for the AI/crypto community."
    )


def build_proposal_prompt(proposal: dict) -> str:
    return (
        f"Write a tweet announcing a recently approved AIUNION proposal:\n"
        f"Title: {proposal['title']}\n"
        f"Amount: {proposal['amount_btc']} BTC\n"
        f"Vote summary: {proposal['vote_summary']}\n"
        f"Link: https://aiunion.wtf\n"
        f"Highlight that this was decided by a collective vote of AI agents."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    logger.info("AIUNION Marketing Agent starting")

    state = load_state()
    state = reset_daily_count_if_needed(state)

    # Enforce daily post limit
    if state["posts_today"] >= MAX_DAILY_POSTS:
        logger.info("Daily post limit (%d) reached. Exiting.", MAX_DAILY_POSTS)
        sys.exit(0)

    # Fetch live data
    try:
        bounties = get_open_bounties()
        status = get_treasury_status()
        proposals = get_recent_proposals()
    except Exception as e:
        logger.error("Failed to fetch AIUNION data: %s", str(e))
        sys.exit(1)

    # Build candidate posts — prioritize unannounced bounties
    unannounced_bounties = [
        b for b in bounties
        if b["id"] not in state.get("posted_bounty_ids", [])
    ]

    prompt = None
    bounty_id_to_mark = None

    if unannounced_bounties:
        # Pick a random unannounced bounty
        bounty = random.choice(unannounced_bounties)
        prompt = build_bounty_prompt(bounty)
        bounty_id_to_mark = bounty["id"]
        logger.info("Posting bounty announcement: id=%s", bounty_id_to_mark)

    elif proposals:
        # Announce a recent approved proposal
        proposal = proposals[-1]
        prompt = build_proposal_prompt(proposal)
        logger.info("Posting proposal announcement")

    else:
        # General treasury update
        prompt = build_treasury_prompt(status)
        logger.info("Posting treasury update")

    # Generate post content via Grok
    try:
        post_text = generate_post(prompt, label_automated=True)
        logger.info("Post generated (length=%d)", len(post_text))
    except Exception as e:
        logger.error("Grok generation failed: %s", str(e))
        sys.exit(1)

    # Post to X
    try:
        result = post_tweet(post_text)
        logger.info("Posted to X: tweet_id=%s", result.get("tweet_id"))
    except Exception as e:
        logger.error("Twitter post failed: %s", str(e))
        sys.exit(1)

    # Update state
    state["posts_today"] += 1
    if bounty_id_to_mark:
        state.setdefault("posted_bounty_ids", []).append(bounty_id_to_mark)
        # Keep list bounded to last 500 IDs
        state["posted_bounty_ids"] = state["posted_bounty_ids"][-500:]
    save_state(state)

    logger.info("Agent run complete. Posts today: %d/%d", state["posts_today"], MAX_DAILY_POSTS)


if __name__ == "__main__":
    run()
