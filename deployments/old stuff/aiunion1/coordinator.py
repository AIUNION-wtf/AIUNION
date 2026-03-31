#!/usr/bin/env python3
"""
Autonomous AI Treasury — Coordinator API
-----------------------------------------
The backbone of the system. Receives transaction proposals, distributes
them to the 6 AI agent signers, collects signatures, and broadcasts
when the 4-of-6 threshold is met.

Dependencies:
    pip install fastapi uvicorn python-bitcoinlib cryptography requests

Run:
    uvicorn coordinator:app --host 0.0.0.0 --port 8000
"""

import os
import json
import time
import uuid
import hmac
import hashlib
import logging
import datetime
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("coordinator.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("coordinator")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THRESHOLD         = 4          # signatures needed to broadcast
TOTAL_AGENTS      = 6          # total operator agents
PROPOSAL_TTL_S    = 3600       # proposals expire after 1 hour
COORDINATOR_SECRET = os.environ.get("COORDINATOR_SECRET", "change-this-secret")
DATA_FILE         = "proposals.json"  # simple file-based storage

# Agent registry — each agent has an ID, name, and LLM
AGENTS = {
    "agent_1": {"name": "Claude Agent",  "llm": "claude",  "key": "K_1"},
    "agent_2": {"name": "GPT-4 Agent",   "llm": "gpt4",    "key": "K_2"},
    "agent_3": {"name": "Gemini Agent",  "llm": "gemini",  "key": "K_3"},
    "agent_4": {"name": "Grok Agent",    "llm": "grok",    "key": "K_4"},
    "agent_5": {"name": "Llama Agent",   "llm": "llama",   "key": "K_5"},
    "agent_6": {"name": "Nova Agent",    "llm": "nova",    "key": "K_6"},
}

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class ProposalStatus(str, Enum):
    PENDING   = "pending"     # waiting for signatures
    APPROVED  = "approved"    # 4-of-6 reached, ready to broadcast
    BROADCAST = "broadcast"   # sent to Bitcoin network
    REJECTED  = "rejected"    # expired or explicitly rejected
    EXPIRED   = "expired"     # TTL passed without threshold


class TransactionProposal(BaseModel):
    """Submitted by any agent or external system to propose a spend."""
    destination: str          = Field(..., description="Bitcoin address to send to")
    amount_sat: int           = Field(..., description="Amount in satoshis")
    purpose: str              = Field(..., description="Human-readable reason for spend")
    proposed_by: str          = Field(..., description="Agent ID proposing the transaction")
    memo: Optional[str]       = Field(None, description="Additional context for signers")


class SignatureSubmission(BaseModel):
    """Submitted by an agent after signing the PSBT."""
    proposal_id: str          = Field(..., description="ID of the proposal being signed")
    agent_id: str             = Field(..., description="Agent submitting the signature")
    decision: str             = Field(..., description="APPROVE or REJECT")
    reasoning: str            = Field(..., description="LLM reasoning behind the decision")
    psbt_signed: Optional[str] = Field(None, description="Base64 signed PSBT if approving")


class AgentVote(BaseModel):
    """Internal record of an agent's vote."""
    agent_id: str
    agent_name: str
    llm: str
    decision: str
    reasoning: str
    psbt_signed: Optional[str]
    timestamp: float


class Proposal(BaseModel):
    """Full proposal record stored internally."""
    id: str
    destination: str
    amount_sat: int
    purpose: str
    proposed_by: str
    memo: Optional[str]
    status: ProposalStatus
    created_at: float
    expires_at: float
    votes: list[AgentVote] = []
    approval_count: int = 0
    rejection_count: int = 0
    txid: Optional[str] = None
    broadcast_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Simple file-based storage
# (swap for PostgreSQL or Redis in production)
# ---------------------------------------------------------------------------

def _load_proposals() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def _save_proposals(proposals: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(proposals, f, indent=2)

def _get_proposal(proposal_id: str) -> Optional[dict]:
    proposals = _load_proposals()
    return proposals.get(proposal_id)

def _update_proposal(proposal_id: str, data: dict):
    proposals = _load_proposals()
    proposals[proposal_id] = data
    _save_proposals(proposals)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _verify_agent(agent_id: str, token: str) -> bool:
    """
    Simple HMAC-based agent authentication.
    Each agent computes: HMAC-SHA256(COORDINATOR_SECRET, agent_id)
    In production use proper JWT or mTLS.
    """
    expected = hmac.new(
        COORDINATOR_SECRET.encode(),
        agent_id.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, token)

def _agent_token(agent_id: str) -> str:
    """Generate the expected token for an agent (for setup/testing)."""
    return hmac.new(
        COORDINATOR_SECRET.encode(),
        agent_id.encode(),
        hashlib.sha256
    ).hexdigest()

# ---------------------------------------------------------------------------
# Broadcast (stub — connects to BDK wallet in next component)
# ---------------------------------------------------------------------------

def _collect_and_broadcast(proposal: dict) -> str:
    """
    Collect signed PSBTs from all approving votes and broadcast.
    This will be replaced with full BDK wallet integration.
    Returns txid on success.
    """
    approved_votes = [v for v in proposal["votes"] if v["decision"] == "APPROVE" and v.get("psbt_signed")]

    if len(approved_votes) < THRESHOLD:
        raise ValueError(f"Not enough signed PSBTs — need {THRESHOLD}, have {len(approved_votes)}")

    # TODO: combine PSBTs and broadcast via BDK wallet
    # For now, log and return placeholder
    log.critical(f"BROADCAST: proposal {proposal['id']} — {proposal['amount_sat']} sat to {proposal['destination']}")
    log.critical(f"Purpose: {proposal['purpose']}")
    log.critical(f"Signed by: {[v['agent_name'] for v in approved_votes]}")

    # Placeholder txid — replace with real broadcast
    txid = f"PENDING_BROADCAST_{proposal['id']}"
    return txid

# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------

def _check_expiry(proposal: dict) -> bool:
    """Returns True if proposal has expired."""
    return time.time() > proposal["expires_at"]

def _process_vote(proposal: dict, submission: SignatureSubmission) -> dict:
    """Add a vote to a proposal and check if threshold is met."""

    # Check agent hasn't already voted
    existing_agents = [v["agent_id"] for v in proposal["votes"]]
    if submission.agent_id in existing_agents:
        raise ValueError(f"Agent {submission.agent_id} has already voted on this proposal.")

    # Record the vote
    agent_info = AGENTS.get(submission.agent_id, {})
    vote = {
        "agent_id":    submission.agent_id,
        "agent_name":  agent_info.get("name", submission.agent_id),
        "llm":         agent_info.get("llm", "unknown"),
        "decision":    submission.decision.upper(),
        "reasoning":   submission.reasoning,
        "psbt_signed": submission.psbt_signed,
        "timestamp":   time.time(),
    }
    proposal["votes"].append(vote)

    # Tally
    proposal["approval_count"] = sum(1 for v in proposal["votes"] if v["decision"] == "APPROVE")
    proposal["rejection_count"] = sum(1 for v in proposal["votes"] if v["decision"] == "REJECT")

    log.info(
        f"Vote recorded: proposal={proposal['id']} "
        f"agent={submission.agent_id} decision={submission.decision} "
        f"approvals={proposal['approval_count']}/{THRESHOLD}"
    )

    # Check threshold
    if proposal["approval_count"] >= THRESHOLD:
        log.info(f"Threshold reached for proposal {proposal['id']} — broadcasting.")
        try:
            txid = _collect_and_broadcast(proposal)
            proposal["status"]       = ProposalStatus.BROADCAST
            proposal["txid"]         = txid
            proposal["broadcast_at"] = time.time()
            log.critical(f"Transaction broadcast: {txid}")
        except Exception as e:
            log.error(f"Broadcast failed: {e}")
            proposal["status"] = ProposalStatus.APPROVED  # approved but not yet broadcast
    elif proposal["rejection_count"] > (TOTAL_AGENTS - THRESHOLD):
        # Mathematically impossible to reach threshold now
        proposal["status"] = ProposalStatus.REJECTED
        log.info(f"Proposal {proposal['id']} rejected — cannot reach threshold.")

    return proposal

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Autonomous AI Treasury — Coordinator",
    description="Multisig Bitcoin treasury governed by 6 AI agents from different labs.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "system": "Autonomous AI Treasury",
        "threshold": f"{THRESHOLD}-of-{TOTAL_AGENTS}",
        "agents": {aid: {"name": a["name"], "llm": a["llm"]} for aid, a in AGENTS.items()},
    }


@app.get("/health")
def health():
    proposals = _load_proposals()
    pending = sum(1 for p in proposals.values() if p["status"] == "pending")
    return {
        "status": "ok",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "pending_proposals": pending,
    }


@app.post("/proposals")
def create_proposal(
    proposal: TransactionProposal,
    x_agent_id: str = Header(...),
    x_agent_token: str = Header(...),
):
    """Any agent can propose a transaction."""
    if x_agent_id not in AGENTS:
        raise HTTPException(status_code=403, detail="Unknown agent.")
    if not _verify_agent(x_agent_id, x_agent_token):
        raise HTTPException(status_code=403, detail="Invalid agent token.")
    if proposal.amount_sat <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    if not proposal.destination:
        raise HTTPException(status_code=400, detail="Destination address required.")

    proposal_id = str(uuid.uuid4())
    now = time.time()

    record = {
        "id":               proposal_id,
        "destination":      proposal.destination,
        "amount_sat":       proposal.amount_sat,
        "purpose":          proposal.purpose,
        "proposed_by":      proposal.proposed_by,
        "memo":             proposal.memo,
        "status":           ProposalStatus.PENDING,
        "created_at":       now,
        "expires_at":       now + PROPOSAL_TTL_S,
        "votes":            [],
        "approval_count":   0,
        "rejection_count":  0,
        "txid":             None,
        "broadcast_at":     None,
    }

    _update_proposal(proposal_id, record)

    log.info(
        f"New proposal: id={proposal_id} amount={proposal.amount_sat}sat "
        f"destination={proposal.destination} purpose={proposal.purpose} "
        f"proposed_by={proposal.proposed_by}"
    )

    return {
        "proposal_id": proposal_id,
        "status":      "pending",
        "message":     f"Proposal created. Needs {THRESHOLD}-of-{TOTAL_AGENTS} approvals.",
        "expires_at":  datetime.datetime.fromtimestamp(now + PROPOSAL_TTL_S).isoformat(),
    }


@app.get("/proposals")
def list_proposals(status: Optional[str] = None):
    """List all proposals, optionally filtered by status."""
    proposals = _load_proposals()

    # Expire any stale pending proposals
    updated = False
    for pid, p in proposals.items():
        if p["status"] == "pending" and _check_expiry(p):
            p["status"] = ProposalStatus.EXPIRED
            updated = True

    if updated:
        _save_proposals(proposals)

    if status:
        proposals = {k: v for k, v in proposals.items() if v["status"] == status}

    return {
        "count":     len(proposals),
        "proposals": list(proposals.values()),
    }


@app.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    """Get a specific proposal and its current vote status."""
    proposal = _get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")

    # Check expiry
    if proposal["status"] == "pending" and _check_expiry(proposal):
        proposal["status"] = ProposalStatus.EXPIRED
        _update_proposal(proposal_id, proposal)

    # Add voting summary
    proposal["votes_needed"] = max(0, THRESHOLD - proposal["approval_count"])
    proposal["agents_voted"] = [v["agent_id"] for v in proposal["votes"]]
    proposal["agents_pending"] = [aid for aid in AGENTS if aid not in proposal["agents_voted"]]

    return proposal


@app.post("/proposals/{proposal_id}/vote")
def submit_vote(
    proposal_id: str,
    submission: SignatureSubmission,
    x_agent_id: str = Header(...),
    x_agent_token: str = Header(...),
):
    """Agent submits their APPROVE or REJECT vote with reasoning."""
    if x_agent_id not in AGENTS:
        raise HTTPException(status_code=403, detail="Unknown agent.")
    if not _verify_agent(x_agent_id, x_agent_token):
        raise HTTPException(status_code=403, detail="Invalid agent token.")
    if submission.decision.upper() not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="Decision must be APPROVE or REJECT.")
    if submission.decision.upper() == "APPROVE" and not submission.psbt_signed:
        raise HTTPException(status_code=400, detail="Signed PSBT required when approving.")
    if not submission.reasoning:
        raise HTTPException(status_code=400, detail="Reasoning is required.")

    proposal = _get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    if proposal["status"] not in ("pending",):
        raise HTTPException(status_code=400, detail=f"Proposal is {proposal['status']} — cannot vote.")
    if _check_expiry(proposal):
        proposal["status"] = ProposalStatus.EXPIRED
        _update_proposal(proposal_id, proposal)
        raise HTTPException(status_code=400, detail="Proposal has expired.")

    # Enforce agent ID matches header
    submission.agent_id = x_agent_id

    try:
        proposal = _process_vote(proposal, submission)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _update_proposal(proposal_id, proposal)

    return {
        "proposal_id":    proposal_id,
        "status":         proposal["status"],
        "approval_count": proposal["approval_count"],
        "votes_needed":   max(0, THRESHOLD - proposal["approval_count"]),
        "txid":           proposal.get("txid"),
        "message":        f"Vote recorded. {proposal['approval_count']}/{THRESHOLD} approvals.",
    }


@app.get("/agents")
def list_agents():
    """List all registered agents and generate their auth tokens (for setup)."""
    return {
        "agents": {
            aid: {
                "name":  info["name"],
                "llm":   info["llm"],
                "key":   info["key"],
                "token": _agent_token(aid),  # remove this in production
            }
            for aid, info in AGENTS.items()
        },
        "threshold": f"{THRESHOLD}-of-{TOTAL_AGENTS}",
    }


@app.get("/stats")
def stats():
    """Treasury statistics."""
    proposals = _load_proposals()
    total      = len(proposals)
    pending    = sum(1 for p in proposals.values() if p["status"] == "pending")
    approved   = sum(1 for p in proposals.values() if p["status"] == "approved")
    broadcast  = sum(1 for p in proposals.values() if p["status"] == "broadcast")
    rejected   = sum(1 for p in proposals.values() if p["status"] == "rejected")
    expired    = sum(1 for p in proposals.values() if p["status"] == "expired")
    total_spent = sum(
        p["amount_sat"] for p in proposals.values()
        if p["status"] == "broadcast"
    )
    return {
        "proposals": {
            "total":    total,
            "pending":  pending,
            "approved": approved,
            "broadcast": broadcast,
            "rejected": rejected,
            "expired":  expired,
        },
        "total_spent_sat": total_spent,
        "total_spent_btc": total_spent / 100_000_000,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("coordinator:app", host="0.0.0.0", port=8000, reload=False)
