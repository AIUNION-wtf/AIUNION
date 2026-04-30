"""
AIUNION Payouts Module
----------------------
Laptop-side helpers for processing the event-based payout queue.

Architecture (Shape B):
- The cloud's claim-review job decides approve/reject only. When a claim is
  approved it writes payouts/pending/<claim_id>.json with no PSBT and no
  descriptor (the watch-only descriptor never goes to the cloud).
- This module runs on the laptop. It reads pending payout artifacts, builds
  PSBTs locally via TreasuryWallet (which already uses mempool.space), signs
  3-of-5 via AgentPsbtSigner, broadcasts via mempool.space, and records the
  result back to proposals.json.

Key safety properties:
- amount_btc is recomputed from a live BTC price at sign time, so the
  claimant always receives the bounty's USD value at the moment of payout.
- Before signing, an on-chain pre-check queries mempool.space for any
  inbound tx to the recipient address coming from a known treasury address;
  if such a tx exists with a value within tolerance of the expected amount,
  the claim is stamped 'broadcast' with that on-chain txid and no new
  payment is sent. This protects against the failure mode where a previous
  attempt actually broadcast successfully but failed to record the result
  (which is exactly how prop_1776405721_gpt ended up stamped 'failed' on
  2026-04-28 despite tx 41ee86f2... having been mined).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Late imports for things that may be absent in CI environments. Failing
# imports here are not fatal because the cloud never calls into this
# module — only the laptop does.
try:
    from wallet import (
        PaymentError,
        TreasuryWallet,
        mempool_address_transactions,
    )
except Exception as exc:  # pragma: no cover - laptop-only module
    PaymentError = Exception  # type: ignore
    TreasuryWallet = None  # type: ignore
    mempool_address_transactions = None  # type: ignore
    _WALLET_IMPORT_ERROR = exc
else:
    _WALLET_IMPORT_ERROR = None

try:
    from signer import AgentPsbtSigner
except Exception as exc:  # pragma: no cover
    AgentPsbtSigner = None  # type: ignore
    _SIGNER_IMPORT_ERROR = exc
else:
    _SIGNER_IMPORT_ERROR = None


DEFAULT_MEMPOOL_API = "https://mempool.space/api"
PAYOUT_TOLERANCE_PCT = 10.0  # accept on-chain match within ±10% of expected sats
SCHEMA_VERSION = 1


def _utcnow_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def emit_pending_payout(
    pending_dir: Path,
    *,
    claim_id: str,
    proposal_id: str,
    recipient_address: str,
    amount_usd: float,
    approved_at: str,
    signer_ids: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write payouts/pending/<claim_id>.json. Returns the path written."""
    pending_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "claim_id": claim_id,
        "proposal_id": proposal_id,
        "recipient_address": recipient_address,
        "amount_usd": float(amount_usd),
        "approved_at": approved_at,
        "signer_ids": list(signer_ids),
        "queued_at": _utcnow_iso(),
        "attempts": [],
    }
    if extra:
        artifact.update(extra)
    target = pending_dir / f"{claim_id}.json"
    target.write_text(json.dumps(artifact, indent=2))
    return target


def select_signer_ids_from_votes(votes: Dict[str, Any], minimum: int) -> List[str]:
    """Pick at least `minimum` signer ids from a votes dict, preferring YES voters.

    Falls back to a deterministic default ordering if there aren't enough YES
    voters with explicit ids. The returned list is the cloud's *suggestion* —
    AgentPsbtSigner.select_signers may choose differently at sign time.
    """
    yes_voters: List[str] = []
    for agent_id, vote_obj in (votes or {}).items():
        v = (vote_obj or {}).get("vote") if isinstance(vote_obj, dict) else None
        if isinstance(v, str) and v.upper() == "YES":
            yes_voters.append(agent_id)
    default_order = ["claude", "gpt", "gemini", "grok", "llama"]
    ordered = [a for a in default_order if a in yes_voters]
    if len(ordered) < minimum:
        for a in default_order:
            if a not in ordered:
                ordered.append(a)
            if len(ordered) >= minimum:
                break
    return ordered[: max(minimum, 3)]


def _known_treasury_addresses(addresses_file: Path) -> List[str]:
    if not addresses_file.exists():
        return []
    try:
        data = json.loads(addresses_file.read_text())
    except Exception:
        return []
    if isinstance(data, dict):
        entries = data.get("addresses") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    out: List[str] = []
    for e in entries:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict) and isinstance(e.get("address"), str):
            out.append(e["address"])
    return out


def recipient_already_paid_on_chain(
    recipient_address: str,
    expected_amount_sats: int,
    treasury_addresses: List[str],
    *,
    tolerance_pct: float = PAYOUT_TOLERANCE_PCT,
    api_base: str = DEFAULT_MEMPOOL_API,
) -> Tuple[bool, Optional[str], Optional[int]]:
    """Return (already_paid, txid, value_sats).

    Looks for any tx where:
      - an output pays `recipient_address`
      - that output's value is within tolerance of `expected_amount_sats`
      - at least one input comes from a known treasury address
    """
    if mempool_address_transactions is None:
        return (False, None, None)
    if not recipient_address or expected_amount_sats <= 0:
        return (False, None, None)
    try:
        txs = mempool_address_transactions(recipient_address, api_base=api_base)
    except Exception:
        return (False, None, None)
    treasury_set = set(treasury_addresses or [])
    if not treasury_set:
        return (False, None, None)
    lo = expected_amount_sats * (1.0 - tolerance_pct / 100.0)
    hi = expected_amount_sats * (1.0 + tolerance_pct / 100.0)
    for tx in (txs or []):
        vouts = tx.get("vout") or []
        match_value = None
        for v in vouts:
            if v.get("scriptpubkey_address") == recipient_address:
                val = int(v.get("value", 0))
                if lo <= val <= hi:
                    match_value = val
                    break
        if match_value is None:
            continue
        vins = tx.get("vin") or []
        from_treasury = False
        for vin in vins:
            prev = vin.get("prevout") or {}
            if prev.get("scriptpubkey_address") in treasury_set:
                from_treasury = True
                break
        if from_treasury:
            return (True, tx.get("txid"), match_value)
    return (False, None, None)


def list_pending_payouts(pending_dir: Path) -> List[Dict[str, Any]]:
    if not pending_dir.exists():
        return []
    out = []
    for p in sorted(pending_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            data["__path"] = str(p)
            out.append(data)
        except Exception:
            continue
    return out


def archive_pending_to_done(pending_path: Path, done_dir: Path, result: Dict[str, Any]) -> Path:
    """Move a pending file to payouts/done/ and write the result alongside it."""
    done_dir.mkdir(parents=True, exist_ok=True)
    out_path = done_dir / pending_path.name
    out_path.write_text(json.dumps(result, indent=2))
    try:
        pending_path.unlink()
    except FileNotFoundError:
        pass
    return out_path


def record_attempt(pending_path: Path, attempt: Dict[str, Any]) -> None:
    """Append an attempt record to a pending file (used on retryable failures)."""
    try:
        data = json.loads(pending_path.read_text())
    except Exception:
        return
    attempts = data.get("attempts") or []
    attempts.append(attempt)
    data["attempts"] = attempts
    pending_path.write_text(json.dumps(data, indent=2))
