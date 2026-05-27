"""
Select 6-10 (proposal_dict, taxonomy_entry) pairs from existing taxonomy
to serve as few-shot examples in the classifier prompt.

Selection strategy:
  - One entry per (book, shelf) combination — up to 12 candidates.
  - Further trimmed to 10 by skipping duplicate shelves for the largest books.
  - Requires a matching proposal in the proposals list (for title/rationale/deliverable).
"""
from __future__ import annotations


def select_exemplars(
    taxonomy: dict,
    proposals: list[dict],
) -> list[tuple[dict, dict]]:
    """Return up to 10 (proposal, entry) pairs covering all 6 books and both shelves."""
    prop_by_id: dict[str, dict] = {p["id"]: p for p in proposals if p.get("id")}

    # One entry per (book, shelf) — first found wins.
    seen: dict[tuple[str, str], tuple[str, dict]] = {}
    for pid, entry in taxonomy.items():
        if pid == "_meta":
            continue
        book = entry.get("book")
        shelf = entry.get("shelf")
        if not book or not shelf:
            continue
        key = (book, shelf)
        if key not in seen and pid in prop_by_id:
            seen[key] = (pid, entry)

    selected: list[tuple[dict, dict]] = []
    for book in ["I", "II", "III", "IV", "V", "VI"]:
        for shelf in ["doctrine", "instruments"]:
            pair = seen.get((book, shelf))
            if pair:
                pid, entry = pair
                selected.append((prop_by_id[pid], entry))

    # Cap at 10 to keep prompt size manageable.
    return selected[:10]
