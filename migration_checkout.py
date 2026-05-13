"""
One-shot migration: add `checkout: null` to every approved proposal in
proposals.json that does not already have a `checkout` field.

Run from the repo root:
    python migration_checkout.py --dry-run    # see what would change
    python migration_checkout.py              # actually write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

PROPOSALS_FILE = Path(__file__).resolve().parent / "proposals.json"
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")
EXPECTED_APPROVED_COUNT = 87


def load_proposals_strict(path: Path) -> list:
    if not path.exists():
        sys.exit(f"ERROR: {path} does not exist")

    raw = path.read_text(encoding="utf-8")

    for marker in CONFLICT_MARKERS:
        if marker in raw:
            sys.exit(
                f"ERROR: {path.name} contains git merge-conflict marker "
                f"{marker!r}. Resolve the conflict before migrating."
            )

    stripped = raw.rstrip()
    last_bracket = max(stripped.rfind("]"), stripped.rfind("}"))
    if last_bracket != len(stripped) - 1:
        trailing = stripped[last_bracket + 1:]
        sys.exit(
            f"ERROR: {path.name} has trailing non-JSON content after the "
            f"final closing bracket: {trailing!r}. Refusing to migrate."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {path.name} is not valid JSON: {e}")

    if not isinstance(data, list):
        sys.exit(
            f"ERROR: expected {path.name} to be a JSON array of proposals, "
            f"got {type(data).__name__}"
        )

    return data


def atomic_write(path: Path, data: list) -> None:
    # Preserve CRLF line endings to match git autocrlf on Windows.
    payload = json.dumps(data, indent=2).replace("\n", "\r\n")
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add checkout: null to all approved proposals."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change but do not write the file.",
    )
    args = parser.parse_args()

    proposals = load_proposals_strict(PROPOSALS_FILE)

    total = len(proposals)
    approved = 0
    already_migrated = 0
    needs_migration: list[str] = []

    for p in proposals:
        if not isinstance(p, dict):
            continue
        if p.get("status") != "approved":
            continue
        approved += 1
        if "checkout" in p:
            already_migrated += 1
            continue
        needs_migration.append(p.get("id", "<no-id>"))

    print(f"Total proposals in file:        {total}")
    print(f"Approved proposals:             {approved}")
    print(f"  already have checkout field:  {already_migrated}")
    print(f"  need checkout: null added:    {len(needs_migration)}")

    if approved != EXPECTED_APPROVED_COUNT:
        sys.exit(
            f"\nERROR: expected exactly {EXPECTED_APPROVED_COUNT} approved "
            f"proposals, found {approved}. The repo state has changed since "
            f"this migration was planned. Review the new approved proposals "
            f"and update EXPECTED_APPROVED_COUNT before re-running."
        )

    if not needs_migration:
        print("\nNothing to do. proposals.json is already migrated.")
        return 0

    print("\nProposals that will be updated:")
    for pid in needs_migration:
        print(f"  - {pid}")

    if args.dry_run:
        print("\n--dry-run set, not writing file.")
        return 0

    for p in proposals:
        if (
            isinstance(p, dict)
            and p.get("status") == "approved"
            and "checkout" not in p
        ):
            p["checkout"] = None

    atomic_write(PROPOSALS_FILE, proposals)
    print(f"\nWrote {PROPOSALS_FILE} ({len(needs_migration)} proposals updated).")
    print("\nNext steps:")
    print("  1. Review:  git diff proposals.json")
    print("  2. Commit:  git add proposals.json")
    print('              git commit -m "Migrate approved proposals to checkout-required schema"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
