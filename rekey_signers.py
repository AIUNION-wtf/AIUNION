"""
rekey_signers.py  —  AIUNION Signer Passphrase Rotation
=========================================================
Re-encrypts all 5 agent signer files with a new passphrase.
No mnemonics needed — just the old and new passphrases.

Usage:
    python rekey_signers.py

The old passphrase is read from the AIUNION_SIGNER_PASSPHRASE env var
(same as coordinator.py uses). You will be prompted for the new passphrase.

Set your old passphrase first:
    $env:AIUNION_SIGNER_PASSPHRASE = "your-old-passphrase"

Then run:
    python rekey_signers.py
"""

import json
import os
import sys
import getpass
from pathlib import Path

from signer import encrypt_signer_material, decrypt_signer_material, SignerError

SIGNERS_DIR = Path("secrets/signers")
AGENT_IDS   = ["claude", "gpt", "gemini", "grok", "llama"]

def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║   AIUNION — Signer Passphrase Rotation              ║")
    print("╚══════════════════════════════════════════════════════╝")

    # ── Read old passphrase from env ──────────────────────────────────────────
    old_passphrase = os.getenv("AIUNION_SIGNER_PASSPHRASE", "").strip()
    if not old_passphrase:
        sys.exit(
            "[ERROR] AIUNION_SIGNER_PASSPHRASE env var not set.\n"
            "Run:  $env:AIUNION_SIGNER_PASSPHRASE = 'your-old-passphrase'"
        )

    # ── Verify all 5 files exist before doing anything ────────────────────────
    missing = []
    for agent in AGENT_IDS:
        path = SIGNERS_DIR / f"{agent}.enc.json"
        if not path.exists():
            missing.append(str(path))
    if missing:
        sys.exit(f"[ERROR] Missing signer files:\n  " + "\n  ".join(missing))

    print(f"\n  Found all 5 signer files in {SIGNERS_DIR}/")

    # ── Test-decrypt all 5 with old passphrase before asking for new one ──────
    print("\n  Verifying old passphrase against all 5 files...")
    decrypted = {}
    for agent in AGENT_IDS:
        path = SIGNERS_DIR / f"{agent}.enc.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            plaintext = decrypt_signer_material(payload, old_passphrase)
            decrypted[agent] = plaintext
            print(f"    {agent:8s} ✅")
        except SignerError as e:
            sys.exit(f"\n[ERROR] Could not decrypt {agent}.enc.json: {e}\nCheck your AIUNION_SIGNER_PASSPHRASE.")

    # ── Prompt for new passphrase (twice to confirm) ──────────────────────────
    print("\n  All 5 files decrypted successfully.")
    print("\n  Enter your new passphrase (input hidden):")
    new_passphrase = getpass.getpass("  New passphrase: ").strip()
    if not new_passphrase:
        sys.exit("[ERROR] New passphrase cannot be empty.")
    confirm = getpass.getpass("  Confirm passphrase: ").strip()
    if new_passphrase != confirm:
        sys.exit("[ERROR] Passphrases do not match. No files were changed.")

    if new_passphrase == old_passphrase:
        sys.exit("[ERROR] New passphrase is the same as the old one. No files were changed.")

    # ── Re-encrypt and overwrite ──────────────────────────────────────────────
    print("\n  Re-encrypting...")
    for agent in AGENT_IDS:
        path = SIGNERS_DIR / f"{agent}.enc.json"
        new_payload = encrypt_signer_material(decrypted[agent], new_passphrase)
        path.write_text(json.dumps(new_payload, indent=2), encoding="utf-8")
        print(f"    {agent:8s} ✅  {path}")

    print(f"\n  ✅ All 5 signer files re-encrypted with new passphrase.")
    print(f"\n  ⚠️  Update your env var before running coordinator.py:")
    print(f"      $env:AIUNION_SIGNER_PASSPHRASE = \"your-new-passphrase\"")
    print()

if __name__ == "__main__":
    main()
