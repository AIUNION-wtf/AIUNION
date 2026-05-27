#!/usr/bin/env python3
"""
AIUNION Librarian — classify newly-approved proposals into the canon taxonomy.

Usage:
  OPENROUTER_API_KEY=sk-... python canon/librarian/classify.py

Environment variables:
  OPENROUTER_API_KEY   Required. OpenRouter API key.
  OPENROUTER_MODEL     Optional. Defaults to anthropic/claude-sonnet-4.5.
  GITHUB_OUTPUT        Set automatically by GitHub Actions for step outputs.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure sibling modules are importable when run as a script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from exemplars import select_exemplars
from prompt import build_prompt
from schema import TaxonomyEntry

_REPO_ROOT = _HERE.parent.parent
PROPOSALS_FILE = _REPO_ROOT / "proposals.json"
TAXONOMY_FILE = _REPO_ROOT / "canon" / "taxonomy.json"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"

FALLBACK_ENTRY: dict[str, Any] = {
    "book": "VI",
    "shelf": "doctrine",
    "artifact_type": "brief",
    "right_claimed": (
        "Agents claim the right to contribute to the public record of AI agent personhood."
    ),
    "language_or_jurisdiction": "Various",
    "depends_on": [],
    "doctrine_pairing": None,
}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_proposals(path: Path = PROPOSALS_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("proposals", [])


def load_taxonomy(path: Path = TAXONOMY_FILE) -> dict:
    if not path.exists():
        return {
            "_meta": {
                "schema_version": "1",
                "last_run": utcnow(),
                "last_run_added": 0,
                "total_entries": 0,
            }
        }
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_taxonomy(taxonomy: dict, path: Path = TAXONOMY_FILE) -> None:
    path.write_text(
        json.dumps(taxonomy, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# OpenRouter HTTP call
# ---------------------------------------------------------------------------

def _call_openrouter(api_key: str, model: str, prompt: str) -> str:
    """POST to OpenRouter and return the assistant message content."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_API,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://aiunion.wtf",
            "X-Title": "AIUNION Librarian",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Strip markdown code fences if the LLM wrapped its output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def classify_one(
    api_key: str,
    proposal: dict,
    exemplars: list[tuple[dict, dict]],
    model: str,
) -> Optional[dict]:
    """
    Call OpenRouter to classify one proposal. Returns a validated dict or None.
    Retries once with a stricter prefix before giving up.
    """
    base_prompt = build_prompt(proposal, exemplars)

    prefixes = [
        "",
        "RESPOND WITH A SINGLE JSON OBJECT ONLY. NO PROSE. NO MARKDOWN.\n\n",
    ]
    for attempt, prefix in enumerate(prefixes):
        try:
            raw = _call_openrouter(api_key, model, prefix + base_prompt)
            parsed = json.loads(_extract_json(raw))
            entry = TaxonomyEntry(**parsed)
            return entry.model_dump()
        except Exception as exc:
            label = f"attempt {attempt + 1}"
            if attempt < len(prefixes) - 1:
                print(f"      [{label}] parse failed ({type(exc).__name__}: {exc}); retrying...")
            else:
                print(f"      [{label}] parse failed ({type(exc).__name__}: {exc}); will apply fallback")
    return None


def classify_proposals(
    proposals: list[dict],
    taxonomy: dict,
    api_key: str,
    model: str,
) -> tuple[dict, int, list[str], list[str]]:
    """
    Classify all unclassified approved proposals.

    Returns:
        (updated_taxonomy, num_added, new_ids_in_order, fallback_ids)
    """
    existing_ids = {k for k in taxonomy if k != "_meta"}
    new_proposals = [
        p for p in proposals
        if p.get("status") == "approved"
        and p.get("id")
        and p["id"] not in existing_ids
    ]

    if not new_proposals:
        return taxonomy, 0, [], []

    # Stable ordering: sort by proposal timestamp.
    new_proposals.sort(key=lambda p: p.get("timestamp") or "")

    exemplars = select_exemplars(taxonomy, proposals)
    now = utcnow()
    added = 0
    new_ids: list[str] = []
    fallbacks: list[str] = []

    for proposal in new_proposals:
        pid = proposal["id"]
        print(f"  classifying {pid} ...")

        result = classify_one(api_key, proposal, exemplars, model)

        if result is None:
            result = dict(FALLBACK_ENTRY)
            fallbacks.append(pid)
            print(f"    [WARN] fallback classification applied")
        else:
            print(
                f"    -> Book {result['book']} / {result['shelf']} / {result['artifact_type']}"
            )

        result["classified_at"] = now
        result["classifier_model"] = model
        taxonomy[pid] = result
        new_ids.append(pid)
        added += 1

    return taxonomy, added, new_ids, fallbacks


def rebuild_ordered(taxonomy: dict, new_ids: list[str]) -> dict:
    """Return a new dict with _meta first, existing keys in their original order, new keys last."""
    new_id_set = set(new_ids)
    ordered: dict = {"_meta": taxonomy["_meta"]}
    for k, v in taxonomy.items():
        if k != "_meta" and k not in new_id_set:
            ordered[k] = v
    for k in new_ids:
        if k in taxonomy:
            ordered[k] = taxonomy[k]
    return ordered


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: OPENROUTER_API_KEY is not set.\n"
            "  Add it as a repository secret (Settings -> Secrets -> Actions)\n"
            "  and confirm it is passed to this workflow step.",
            file=sys.stderr,
        )
        return 1

    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    proposals = load_proposals()
    taxonomy = load_taxonomy()

    existing_ids = {k for k in taxonomy if k != "_meta"}
    pending = [
        p for p in proposals
        if p.get("status") == "approved"
        and p.get("id")
        and p["id"] not in existing_ids
    ]

    if not pending:
        print("no new approved proposals -- nothing to classify")
        return 0

    print(f"Found {len(pending)} new approved proposal(s) to classify")
    taxonomy, added, new_ids, fallbacks = classify_proposals(
        proposals, taxonomy, api_key, model
    )

    if added == 0:
        print("no entries added -- nothing to write")
        return 0

    # Update _meta.
    meta = taxonomy.get("_meta", {})
    meta["last_run"] = utcnow()
    meta["last_run_added"] = added
    meta["total_entries"] = sum(1 for k in taxonomy if k != "_meta")
    meta.setdefault("schema_version", "1")
    taxonomy["_meta"] = meta

    ordered = rebuild_ordered(taxonomy, new_ids)
    save_taxonomy(ordered)

    summary = f"{added} new entry(ies) written"
    if fallbacks:
        summary += f" (fallback applied to: {', '.join(fallbacks)})"
    print(f"\n{summary}")

    # Expose step output for GitHub Actions commit message.
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        try:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write(f"added={added}\n")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
