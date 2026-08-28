#!/usr/bin/env python3
"""
AIUNION Admin Override Signing Tool
=====================================
Manual-only emergency admin key signing for Bitcoin treasury payments.

WARNING: This tool bypasses the 3-of-5 AI agent quorum.
Only use when quorum cannot be reached or an emergency payment is needed.
Every use is permanently logged to admin_log.txt.

THIS SCRIPT MUST NEVER BE CALLED FROM coordinator.py OR ANY AUTOMATED TASK.

Wallet details:
  Policy:  or(pk(K_admin), or(thresh(3,pk(K_1),...,pk(K_5)), pk(K_scorch)))
  Path:    m/87'/0'/0'
  Network: Bitcoin mainnet

Usage:
  python admin_sign.py --psbt <path_to_psbt_file>
  python admin_sign.py --claim <claim_id>

Keyfile format (K_admin.json — created separately, NEVER commit to git):
  {
    "version": 1,
    "key_type": "admin",
    "note": "AIUNION K_admin key",
    "salt": "<base64-encoded 16-byte random salt>",
    "encrypted_privkey": "<Fernet-encrypted hex private key>",
    "xonly_pubkey_hex": "<32-byte x-only public key, hex>",
    "taproot_merkle_root_hex": "<32-byte tap tree merkle root, hex — required for --claim mode>"
  }

To create a keyfile for a derived key (m/87'/0'/0'):
  from embit.bip32 import HDKey
  from embit import ec
  root = HDKey.from_string("xprv...")
  child = root.derive("m/87h/0h/0h")
  privkey_hex = child.key.serialize().hex()
  # Then run create_keyfile.py or encrypt manually using the format above.
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from getpass import getpass

# ── Dependency checks ─────────────────────────────────────────────────────────

_missing = []
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes as _crypto_hashes
except ImportError:
    _missing.append("cryptography")

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _missing.append("requests")
    _REQUESTS_OK = False

try:
    from embit import ec as _ec, hashes as _hashes
    from embit.psbt import PSBT as _PSBT
    from embit.transaction import (
        Transaction as _Tx,
        TransactionInput as _TxIn,
        TransactionOutput as _TxOut,
    )
    from embit.script import Script as _Script, Witness as _Witness
    from embit.script import address_to_scriptpubkey as _addr2script
except ImportError:
    _missing.append("embit")

if _missing:
    print(f"\nERROR: Missing Python packages: {', '.join(_missing)}")
    print(f"Install with:  pip install {' '.join(_missing)}")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────

TREASURY_ADDRESS = "bc1pjjmjypmzqgqkjxrhx0hpmaetlk75k04gh9hvkexmmfqyl5g7sjfsk4cge7"
MEMPOOL_API      = "https://mempool.space/api"
GITHUB_CLAIMS    = "https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/main/claims.json"
ADMIN_LOG        = "admin_log.txt"
CLAIMS_FILE      = "claims.json"
TREASURY_FILE    = "treasury.json"
PBKDF2_ITERS     = 600_000

GREEN = "\033[92m"
AMBER = "\033[93m"
RED   = "\033[91m"
BOLD  = "\033[1m"
RST   = "\033[0m"

# ── Key encryption ────────────────────────────────────────────────────────────

def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit Fernet key from a password using PBKDF2-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=_crypto_hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def load_admin_keyfile(keyfile_path: str, password: str) -> dict:
    """
    Decrypt the K_admin keyfile.

    Returns a dict with:
      privkey_bytes     - 32-byte raw private key
      xonly_pubkey      - 32-byte x-only public key
      taproot_merkle_root - 32-byte tap tree merkle root, or None
    """
    if not os.path.isfile(keyfile_path):
        raise FileNotFoundError(f"Keyfile not found: {keyfile_path}")

    with open(keyfile_path, encoding="utf-8") as f:
        kf = json.load(f)

    if kf.get("version") != 1 or kf.get("key_type") != "admin":
        raise ValueError(
            f"Expected key_type='admin' version=1, "
            f"got key_type='{kf.get('key_type')}' version={kf.get('version')}."
        )

    salt = base64.b64decode(kf["salt"])
    fk   = _derive_fernet_key(password, salt)

    try:
        privkey_hex = Fernet(fk).decrypt(
            kf["encrypted_privkey"].encode()
        ).decode()
    except (InvalidToken, Exception):
        raise ValueError("Wrong passphrase or corrupted keyfile.")

    privkey_bytes = bytes.fromhex(privkey_hex)
    privkey       = _ec.PrivateKey(privkey_bytes)
    xonly         = privkey.get_public_key().xonly()

    stored = kf.get("xonly_pubkey_hex", "")
    if stored and bytes.fromhex(stored) != xonly:
        raise ValueError(
            "Derived public key does not match keyfile xonly_pubkey_hex. "
            "Wrong key or corrupted keyfile."
        )

    merkle_root = None
    if kf.get("taproot_merkle_root_hex"):
        merkle_root = bytes.fromhex(kf["taproot_merkle_root_hex"])

    return {
        "privkey_bytes":      privkey_bytes,
        "xonly_pubkey":       xonly,
        "taproot_merkle_root": merkle_root,
    }


def clear_key_dict(d: dict) -> None:
    """Best-effort clear of sensitive fields from a dict."""
    d.pop("privkey_bytes", None)
    d.clear()


# ── mempool.space API ─────────────────────────────────────────────────────────

def fetch_utxos(address: str) -> list:
    r = _requests.get(f"{MEMPOOL_API}/address/{address}/utxo", timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_btc_price() -> float:
    try:
        r = _requests.get(f"{MEMPOOL_API}/v1/prices", timeout=10)
        r.raise_for_status()
        return float(r.json().get("USD", 70_000))
    except Exception:
        return 70_000.0


def fetch_fee_rate() -> int:
    """Return recommended half-hour fee rate in sat/vB."""
    try:
        r = _requests.get(f"{MEMPOOL_API}/v1/fees/recommended", timeout=10)
        r.raise_for_status()
        return int(r.json().get("halfHourFee", 5))
    except Exception:
        return 5


def broadcast_tx(raw_hex: str) -> str:
    """Broadcast a raw transaction hex via mempool.space. Returns txid."""
    r = _requests.post(
        f"{MEMPOOL_API}/tx",
        data=raw_hex,
        headers={"Content-Type": "text/plain"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text.strip()


# ── PSBT helpers ──────────────────────────────────────────────────────────────

def load_psbt(path: str) -> _PSBT:
    """Load a PSBT from a file (base64 text or raw bytes)."""
    import io
    with open(path, "rb") as f:
        raw = f.read().strip()
    try:
        data = base64.b64decode(raw)
    except Exception:
        data = raw
    return _PSBT.read_from(io.BytesIO(data))


def save_psbt(psbt: _PSBT, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(psbt.serialize()).decode())


def psbt_to_raw_tx(psbt: _PSBT) -> bytes:
    """
    Extract the final signed transaction bytes from a PSBT.

    For Taproot key path inputs: reads final_scriptwitness (set by embit signing).
    For Taproot script path inputs: assembles witness from taproot_sigs + taproot_scripts.
    Raises ValueError if any input is unsigned.
    """
    vin = []
    for i, inp in enumerate(psbt.inputs):
        tx_in = inp.vin  # fresh TransactionInput (no witness yet)
        w = inp.final_scriptwitness

        if not w and inp.taproot_sigs:
            # Script path spend — assemble [sig, script, ctrl_block]
            for (pub, leaf), sig in inp.taproot_sigs.items():
                for ctrl, sc in inp.taproot_scripts.items():
                    lv   = sc[-1]
                    sdata = sc[:-1]
                    computed = _hashes.tagged_hash(
                        "TapLeaf",
                        bytes([lv]) + _Script(sdata).serialize(),
                    )
                    if computed == leaf:
                        w = _Witness([sig, sdata, ctrl])
                        break
                if w:
                    break

        if not w:
            raise ValueError(
                f"Input {i} is unsigned — cannot finalize. "
                "Verify that K_admin is the taproot internal key for this PSBT."
            )

        tx_in.witness = w
        vin.append(tx_in)

    final_tx = _Tx(
        version=psbt.tx_version or 2,
        locktime=psbt.locktime or 0,
        vin=vin,
        vout=[out.vout for out in psbt.outputs],
    )
    return final_tx.serialize()


# ── Claim helpers ─────────────────────────────────────────────────────────────

def fetch_claim(claim_id: str) -> dict:
    """Look up a claim by ID from local file then GitHub."""
    if os.path.isfile(CLAIMS_FILE):
        with open(CLAIMS_FILE, encoding="utf-8") as f:
            for c in json.load(f):
                if c.get("id") == claim_id:
                    return c

    r = _requests.get(GITHUB_CLAIMS, timeout=20)
    r.raise_for_status()
    for c in r.json():
        if c.get("id") == claim_id:
            return c

    raise ValueError(f"Claim '{claim_id}' not found in claims.json or GitHub.")


def build_psbt_from_claim(claim: dict, admin_xonly: bytes, merkle_root: bytes) -> _PSBT:
    """
    Build an unsigned P2TR PSBT to pay an approved claim from the treasury.

    Sets taproot_internal_key and taproot_merkle_root on each input so embit
    can sign via key path (K_admin is the taproot internal key).
    """
    recipient = claim.get("btc_address")
    if not recipient:
        raise ValueError("Claim has no btc_address.")

    amount_sats = int(claim.get("amount_btc", 0) * 1e8)
    if amount_sats <= 0:
        amount_usd = float(claim.get("amount_usd", 0))
        if amount_usd <= 0:
            raise ValueError("Claim has no valid payment amount (amount_btc / amount_usd).")
        btc_price   = fetch_btc_price()
        amount_sats = int((amount_usd / btc_price) * 1e8)

    utxos      = fetch_utxos(TREASURY_ADDRESS)
    if not utxos:
        raise ValueError("No UTXOs found at treasury address.")

    total_sats = sum(u["value"] for u in utxos)
    fee_rate   = fetch_fee_rate()

    # P2TR: ~58 vB/input, ~43 vB/output, 10 vB overhead; 2 outputs (payment + change)
    est_vbytes = 10 + len(utxos) * 58 + 43 * 2
    fee_sats   = max(est_vbytes * fee_rate, 300)  # floor at 300 sat

    if amount_sats + fee_sats > total_sats:
        raise ValueError(
            f"Insufficient funds: need {amount_sats + fee_sats:,} sat, "
            f"have {total_sats:,} sat."
        )
    change_sats = total_sats - amount_sats - fee_sats

    treasury_script = _addr2script(TREASURY_ADDRESS)
    recip_script    = _addr2script(recipient)

    vin = [
        _TxIn(bytes.fromhex(u["txid"])[::-1], u["vout"], sequence=0xFFFFFFFD)
        for u in utxos
    ]
    vout = [_TxOut(amount_sats, recip_script)]
    if change_sats > 546:
        vout.append(_TxOut(change_sats, treasury_script))

    tx   = _Tx(vin=vin, vout=vout)
    psbt = _PSBT(tx)

    for i, u in enumerate(utxos):
        psbt.inputs[i].witness_utxo        = _TxOut(u["value"], treasury_script)
        psbt.inputs[i].taproot_internal_key = _ec.PublicKey.from_xonly(admin_xonly)
        if merkle_root is not None:
            psbt.inputs[i].taproot_merkle_root = merkle_root

    return psbt


# ── Logging + record update ───────────────────────────────────────────────────

def log_admin(action: str, detail: str = "") -> None:
    ts    = datetime.now(timezone.utc).isoformat()
    entry = f"[{ts}] ADMIN OVERRIDE | {action}"
    if detail:
        entry += f" | {detail}"
    entry += "\n"
    with open(ADMIN_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


def update_records(claim_id: str, txid: str) -> None:
    """Write txid back into local claims.json and treasury.json if they exist."""
    now = datetime.now(timezone.utc).isoformat()

    if os.path.isfile(CLAIMS_FILE):
        with open(CLAIMS_FILE, encoding="utf-8") as f:
            claims = json.load(f)
        for c in claims:
            if c.get("id") == claim_id:
                c["txid"]           = txid
                c["paid_at"]        = now
                c["payment_method"] = "admin_override"
        with open(CLAIMS_FILE, "w", encoding="utf-8") as f:
            json.dump(claims, f, indent=2)
        print(f"  {GREEN}claims.json updated.{RST}")

    if os.path.isfile(TREASURY_FILE):
        with open(TREASURY_FILE, encoding="utf-8") as f:
            t = json.load(f)
        t.setdefault("admin_overrides", []).append(
            {"claim_id": claim_id, "txid": txid, "at": now}
        )
        with open(TREASURY_FILE, "w", encoding="utf-8") as f:
            json.dump(t, f, indent=2)
        print(f"  {GREEN}treasury.json updated.{RST}")


# ── UI ────────────────────────────────────────────────────────────────────────

def print_override_warning() -> None:
    print(f"\n{RED}{BOLD}")
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║          ⚠   ADMIN OVERRIDE MODE ACTIVE   ⚠             ║")
    print("  ║                                                          ║")
    print("  ║  You are bypassing the 3-of-5 AI agent quorum.          ║")
    print("  ║  This action is permanently logged to admin_log.txt.    ║")
    print("  ║  Only use in genuine emergencies.                       ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print(f"{RST}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="AIUNION Admin Override Signing Tool — MANUAL USE ONLY",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python admin_sign.py --psbt payment.psbt\n"
            "  python admin_sign.py --claim claim_1234567890\n\n"
            "NEVER call this from coordinator.py or any automated script.\n"
            "This tool signs with K_admin only. K_1–K_5 and K_scorch are never loaded."
        ),
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--psbt",  metavar="PATH",     help="Path to PSBT file to sign")
    grp.add_argument("--claim", metavar="CLAIM_ID", help="Build PSBT from an approved claim and sign it")
    args = ap.parse_args()

    print_override_warning()
    log_admin("SESSION_START", f"mode={'psbt' if args.psbt else 'claim'} pid={os.getpid()}")

    # ── Credentials ───────────────────────────────────────────────────────────
    keyfile_path = input(f"{AMBER}K_admin keyfile path: {RST}").strip().strip("'\"")
    if not os.path.isfile(keyfile_path):
        print(f"\n  {RED}ERROR: Keyfile not found: {keyfile_path}{RST}")
        log_admin("KEY_LOAD_FAILED", "keyfile not found")
        sys.exit(1)

    password = getpass(f"{AMBER}K_admin passphrase:   {RST}")

    print(f"\n  Loading K_admin key ... ", end="", flush=True)
    try:
        key_data = load_admin_keyfile(keyfile_path, password)
    except Exception as e:
        print(f"{RED}FAILED{RST}")
        print(f"\n  {RED}ERROR: {e}{RST}")
        log_admin("KEY_LOAD_FAILED", str(e))
        sys.exit(1)
    finally:
        password = None  # clear passphrase from locals ASAP

    print(f"{GREEN}OK{RST}")
    admin_xonly  = key_data["xonly_pubkey"]
    merkle_root  = key_data["taproot_merkle_root"]
    print(f"  Admin public key (x-only): {admin_xonly.hex()}")
    if merkle_root:
        print(f"  Tap merkle root: {merkle_root.hex()}")
    else:
        if args.claim:
            print(f"  {AMBER}WARNING: taproot_merkle_root_hex not in keyfile.{RST}")
            print(f"  Key path signing for the --claim PSBT may fail without it.")

    # ── Build or load PSBT ────────────────────────────────────────────────────
    psbt_path = args.psbt
    claim_id  = None
    claim     = None

    if args.claim:
        claim_id = args.claim
        print(f"\n  Fetching claim {claim_id} ...")
        try:
            claim = fetch_claim(claim_id)
        except Exception as e:
            print(f"  {RED}ERROR: {e}{RST}")
            log_admin("CLAIM_FETCH_FAILED", str(e))
            clear_key_dict(key_data)
            sys.exit(1)

        if claim.get("status") != "approved":
            print(f"  {RED}ERROR: Claim status is '{claim.get('status')}', expected 'approved'.{RST}")
            clear_key_dict(key_data)
            sys.exit(1)

        print(f"  Claim : {claim.get('title', claim_id)}")
        print(f"  Amount: ${claim.get('amount_usd', '?')} USD")
        print(f"  To    : {claim.get('btc_address', 'UNKNOWN')}")
        print(f"\n  Building PSBT (fetching UTXOs from mempool.space) ...")

        try:
            psbt = build_psbt_from_claim(claim, admin_xonly, merkle_root)
        except Exception as e:
            print(f"  {RED}ERROR building PSBT: {e}{RST}")
            log_admin("PSBT_BUILD_FAILED", str(e))
            clear_key_dict(key_data)
            sys.exit(1)

        psbt_path = f"admin_unsigned_{claim_id}_{int(time.time())}.psbt"
        save_psbt(psbt, psbt_path)
        print(f"  {GREEN}Unsigned PSBT saved: {psbt_path}{RST}")

    # ── Sign ──────────────────────────────────────────────────────────────────
    print(f"\n  Signing: {psbt_path}")

    if not psbt_path or not os.path.isfile(psbt_path):
        print(f"  {RED}ERROR: PSBT file not found: {psbt_path}{RST}")
        clear_key_dict(key_data)
        sys.exit(1)

    try:
        psbt = load_psbt(psbt_path)
    except Exception as e:
        print(f"  {RED}ERROR loading PSBT: {e}{RST}")
        log_admin("PSBT_LOAD_FAILED", str(e))
        clear_key_dict(key_data)
        sys.exit(1)

    try:
        privkey  = _ec.PrivateKey(key_data["privkey_bytes"])
        n_signed = psbt.sign_with(privkey)
    except Exception as e:
        print(f"  {RED}ERROR signing: {e}{RST}")
        log_admin("SIGNING_FAILED", str(e))
        clear_key_dict(key_data)
        sys.exit(1)
    finally:
        clear_key_dict(key_data)

    if n_signed == 0:
        print(f"\n  {AMBER}WARNING: K_admin signed 0 inputs.{RST}")
        print(f"  The PSBT must have PSBT_IN_TAP_INTERNAL_KEY set to the admin x-only pubkey.")
        print(f"  If this PSBT was built externally, verify it was created for the admin key path.")
        log_admin("SIGNING_WARN", f"0 inputs signed — psbt={psbt_path}")
    else:
        print(f"  {GREEN}Signed {n_signed} input(s) with K_admin.{RST}")
        log_admin("SIGNED", f"psbt={psbt_path} n_signed={n_signed}")

    # ── Save signed PSBT ──────────────────────────────────────────────────────
    base = os.path.splitext(psbt_path)[0]
    signed_psbt_path = f"{base}_admin_signed.psbt"
    save_psbt(psbt, signed_psbt_path)
    print(f"  Signed PSBT saved: {signed_psbt_path}")

    # ── Finalize → raw hex ────────────────────────────────────────────────────
    raw_hex      = None
    raw_hex_path = None

    try:
        raw_bytes    = psbt_to_raw_tx(psbt)
        raw_hex      = raw_bytes.hex()
        raw_hex_path = f"{base}_admin_signed.hex"
        with open(raw_hex_path, "w", encoding="utf-8") as f:
            f.write(raw_hex)
        print(f"  Raw tx hex saved:  {raw_hex_path} ({len(raw_bytes)} bytes)")
    except ValueError as e:
        # Not all inputs are signed yet
        print(f"\n  {AMBER}Cannot finalize yet: {e}{RST}")
        print(f"  The signed PSBT may need additional signatures from other keyholders.")
        print(f"  Pass it to other signers: {signed_psbt_path}")
        print(f"\n{GREEN}Admin override session complete.{RST}\n")
        return

    # ── Broadcast ─────────────────────────────────────────────────────────────
    print(f"\n  {GREEN}Transaction is fully signed and ready to broadcast.{RST}")

    do_bcast = input(f"\n{AMBER}Broadcast now via mempool.space? [y/N]: {RST}").strip().lower()

    if do_bcast == "y":
        print(f"  Broadcasting ...")
        try:
            txid = broadcast_tx(raw_hex)
        except Exception as e:
            print(f"\n  {RED}Broadcast failed: {e}{RST}")
            print(f"  Broadcast the hex manually from: {raw_hex_path}")
            print(f"  https://mempool.space/tx/push")
            log_admin("BROADCAST_FAILED", str(e))
            print(f"\n{GREEN}Admin override session complete.{RST}\n")
            return

        print(f"\n  {GREEN}SUCCESS — transaction broadcast!{RST}")
        print(f"  TXID: {txid}")
        print(f"  View: https://mempool.space/tx/{txid}")
        log_admin("BROADCAST_SUCCESS", f"txid={txid} psbt={psbt_path}")

        if claim_id:
            update_records(claim_id, txid)
    else:
        print(f"\n  {GREEN}Not broadcast. Submit the hex manually when ready:{RST}")
        print(f"  https://mempool.space/tx/push")
        print(f"  Hex file: {raw_hex_path}")

    print(f"\n{GREEN}Admin override session complete.{RST}\n")


if __name__ == "__main__":
    # Refuse to run if this module is imported rather than executed directly,
    # which prevents accidental calls from coordinator.py.
    main()
