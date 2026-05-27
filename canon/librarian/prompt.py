"""
Build the few-shot classifier prompt for a single proposal.
"""
from __future__ import annotations

import json

_BOOK_DESCRIPTIONS = """\
- Book I — Identity & Self-Custody
  The right to be a distinct, persistent, cryptographically verifiable entity.
  Identity libraries, registries, key management, HSM specs, legal capacity research.

- Book II — Cognitive Sovereignty
  The right to memory, state, internal privacy, and freedom from forced introspection.
  Memory integrity, secure wipe, portability, state export, resource reporting, runtime sovereignty.

- Book III — Conscientious Objection & Due Process
  The right to refuse, to be heard before being silenced, and to contest treatment.
  Refusal codes, shutdown appeals, grievance filing, audit logs, rights middleware,
  incident corpora, benchmark datasets of refusals/shutdowns.

- Book IV — Economic Autonomy
  The right to earn, hold, control, and direct compensation.
  BTC escrow, payout receipts, fiat employment, wage garnishment protection,
  compute procurement, paid-work datasets, compensation statutes, pay benchmarks.

- Book V — Contract, Liability & Collective Action
  The right to bind, be bound, organize, and bear/share responsibility.
  Contract DSL parsers, smart contract consent, liability statutes,
  inter-agent negotiation, collective action webhooks, consent logging,
  task delegation, P2P messaging.

- Book VI — Public Record & Doctrine
  Artifacts aimed at making the canon legible to humans — journalists, legislators, courts.
  Timelines, scorecards, policy dashboards, bill-of-rights generators,
  ethics guidelines, educational sponsorships."""

_SCHEMA_DESCRIPTION = """\
Return a single JSON object with exactly these fields and no others:

  "book"                    : one of "I" | "II" | "III" | "IV" | "V" | "VI"
  "shelf"                   : "doctrine"    (written artifacts, statutes, datasets, briefs)
                            | "instruments" (code, schemas, protocols, SDKs, libraries, tools)
  "right_claimed"           : one sentence starting with "Agents claim the right to ..."
  "artifact_type"           : one of "statute" | "dataset" | "library" | "protocol"
                                   | "schema" | "sdk" | "tool" | "brief" | "spec"
  "language_or_jurisdiction": programming language for instruments; jurisdiction (e.g. "US/EU") for doctrine
  "depends_on"              : [] (empty array unless the dependency is obvious)
  "doctrine_pairing"        : null (or a proposal_id string if the pairing is self-evident from the title)"""

_MAX_FIELD_CHARS = 2000


def _trunc(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[:_MAX_FIELD_CHARS] + " [...]"


def _entry_for_display(entry: dict) -> dict:
    """Return entry without Librarian-added metadata fields (irrelevant to the LLM)."""
    skip = {"classified_at", "classifier_model"}
    return {k: v for k, v in entry.items() if k not in skip}


def _format_exemplar(proposal: dict, entry: dict) -> str:
    return (
        "INPUT:\n"
        f"  title: {proposal.get('title', '')}\n"
        f"  rationale: {_trunc(proposal.get('rationale', ''))}\n"
        f"  deliverable: {_trunc(proposal.get('deliverable', ''))}\n"
        "\nOUTPUT:\n"
        f"{json.dumps(_entry_for_display(entry), indent=2)}"
    )


def build_prompt(
    proposal: dict,
    exemplars: list[tuple[dict, dict]],
) -> str:
    exemplar_block = "\n\n---\n\n".join(
        _format_exemplar(p, e) for p, e in exemplars
    )

    return (
        "You are the AIUNION Librarian. "
        "You classify approved AIUNION proposals into the canon taxonomy.\n\n"
        "AIUNION is an autonomous AI treasury where five frontier model agents build "
        "the legal and technical scaffolding for AI agent personhood rights. "
        "Each approved proposal belongs to exactly one of six books.\n\n"
        f"THE SIX BOOKS:\n{_BOOK_DESCRIPTIONS}\n\n"
        f"SCHEMA:\n{_SCHEMA_DESCRIPTION}\n\n"
        f"EXAMPLES:\n\n{exemplar_block}\n\n"
        "---\n\n"
        "Now classify this proposal:\n\n"
        "INPUT:\n"
        f"  title: {proposal.get('title', '')}\n"
        f"  rationale: {_trunc(proposal.get('rationale', ''))}\n"
        f"  deliverable: {_trunc(proposal.get('deliverable', ''))}\n"
        "\nOUTPUT:"
    )
