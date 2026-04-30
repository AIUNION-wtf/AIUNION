#!/usr/bin/env python3
"""
AIUNION Scorch Key — Scorched Earth Nuclear Option
====================================================
PERMANENTLY destroys all treasury funds by sending every UTXO to OP_RETURN.
The entire balance becomes the miner fee. No funds are recoverable after
this transaction is confirmed.

THIS SCRIPT MUST NEVER BE CALLED FROM coordinator.py OR ANY AUTOMATED TASK.
THIS SCRIPT DOES NOT BROADCAST AUTOMATICALLY — manual submission required.

Wallet details:
  Policy:  or(pk(K_admin), or(thresh(3,pk(K_1),...,pk(K_5)), pk(K_scorch)))
  Path:    m/87'/0'/0'
  Network: Bitcoin mainnet
  K_scorch spends via Taproot script path (leaf: <K_scorch_xonly> OP_CHECKSIG)

Usage:
  python scorch.py                      # fetch UTXOs live from mempool.space
  python scorch.py --utxos utxos.json   # air-gapped mode — no network needed

Air-gapped UTXO file format:
  [{"txid": "abc123...", "vout": 0, "value": 12345}, ...]
  Download with: curl https://mempool.space/api/address/<addr>/utxo > utxos.json

Keyfile format (K_scorch.json — created separately, NEVER commit to git):
  {
    "version": 1,
    "key_type": "scorch",
    "note": "AIUNION K_scorch key - NUCLEAR OPTION",
    "salt": "<base64-encoded 16-byte random salt>",
    "encrypted_privkey": "<Fernet-encrypted hex private key>",
    "xonly_pubkey_hex": "<32-byte x-only public key, hex>",
    "tap_leaf_script_hex": "20<K_scorch_xonly_32bytes_hex>ac",
    "control_block_hex": "<c0_or_c1><internal_key_xonly_32bytes><sibling_leaf_hash_32bytes>..."
  }

  tap_leaf_script_hex: The locking script for K_scorch's tap leaf.
    Format: 20 <K_scorch_xonly_32bytes> ac
    (PUSH32 <pubkey> OP_CHECKSIG, 34 bytes total)

  control_block_hex: Required for Taproot script path spending.
    Byte 0   : leaf_version (0xC0) OR'd with parity bit of the output key
               (0xC0 if output key y is even, 0xC1 if odd)
    Bytes 1-32: K_admin's x-only public key (the taproot internal key)
    Bytes 33+ : Merkle proof — sibling leaf hash(es) from this leaf to the root
               For a 2-leaf tree: 32 bytes (the sibling thresh-3-of-5 leaf hash)
               Total control block: 65 bytes for a single-depth tree

  These values are computed once during wallet setup and are non-sensitive.
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

_REQUESTS_OK = False
try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    pass  # optional — only needed when not using --utxos

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
SCORCH_MESSAGE   = b"AIUNION SCORCH"   # embedded in OP_RETURN (≤80 bytes)
PBKDF2_ITERS     = 600_000

RED   = "\033[91m"
AMBER = "\033[93m"
GREEN = "\033[92m"
BOLD  = "\033[1m"
RST   = "\033[0m"

# ── Key encryption ────────────────────────────────────────────────────────────

def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=_crypto_hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def load_scorch_keyfile(keyfile_path: str, password: str) -> dict:
    """
    Decrypt and validate the K_scorch keyfile.

    Returns a dict with:
      privkey_bytes    - 32-byte raw private key
      xonly_pubkey     - 32-byte x-only public key
      tap_leaf_script  - bytes: script data for K_scorch's tap leaf (without length prefix)
      control_block    - bytes: control block for script path spend
    """
    if not os.path.isfile(keyfile_path):
        raise FileNotFoundError(f"Keyfile not found: {keyfile_path}")

    with open(keyfile_path, encoding="utf-8") as f:
        kf = json.load(f)

    if kf.get("version") != 1 or kf.get("key_type") != "scorch":
        raise ValueError(
            f"Expected key_type='scorch' version=1, "
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

    if not kf.get("tap_leaf_script_hex"):
        raise ValueError(
            "Keyfile missing 'tap_leaf_script_hex'. "
            "This field is required for Taproot script path spending. "
            "It should be 20<K_scorch_xonly_32bytes>ac (34 bytes hex)."
        )
    if not kf.get("control_block_hex"):
        raise ValueError(
            "Keyfile missing 'control_block_hex'. "
            "This field is required for Taproot script path spending. "
            "It encodes the tap tree Merkle proof for the K_scorch leaf."
        )

    tap_leaf_script = bytes.fromhex(kf["tap_leaf_script_hex"])
    control_block   = bytes.fromhex(kf["control_block_hex"])

    # Sanity: leaf script should be 34 bytes: 0x20 + 32-byte xonly + 0xac
    if len(tap_leaf_script) != 34 or tap_leaf_script[0] != 0x20 or tap_leaf_script[-1] != 0xac:
        raise ValueError(
            f"tap_leaf_script_hex looks wrong (expected 34 bytes: 20<xonly32>ac, "
            f"got {len(tap_leaf_script)} bytes starting with {tap_leaf_script[:2].hex()})."
        )
    # Sanity: x-only in script should match the derived public key
    script_xonly = tap_leaf_script[1:33]
    if script_xonly != xonly:
        raise ValueError(
            "tap_leaf_script_hex embeds a different public key than the decrypted private key. "
            "Verify the keyfile was created for K_scorch."
        )
    # Sanity: control block minimum 33 bytes (leaf_version_parity + 32-byte internal key)
    if len(control_block) < 33:
        raise ValueError(
            f"control_block_hex is too short ({len(control_block)} bytes, minimum 33)."
        )
    leaf_version_byte = control_block[0]
    if (leaf_version_byte & 0xFE) != 0xC0:
        raise ValueError(
            f"control_block_hex first byte 0x{leaf_version_byte:02x} is not a valid "
            "Tapscript leaf version (expected 0xC0 or 0xC1)."
        )

    return {
        "privkey_bytes":   privkey_bytes,
        "xonly_pubkey":    xonly,
        "tap_leaf_script": tap_leaf_script,
        "control_block":   control_block,
    }


def clear_key_dict(d: dict) -> None:
    d.pop("privkey_bytes", None)
    d.clear()


# ── UTXO fetching ─────────────────────────────────────────────────────────────

def fetch_utxos_live(address: str) -> list:
    if not _REQUESTS_OK:
        raise RuntimeError(
            "'requests' is not installed. "
            "Install it with: pip install requests  "
            "Or provide UTXOs offline: python scorch.py --utxos utxos.json"
        )
    r = _requests.get(f"{MEMPOOL_API}/address/{address}/utxo", timeout=20)
    r.raise_for_status()
    return r.json()


def load_utxos_file(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        utxos = json.load(f)
    if not isinstance(utxos, list) or not utxos:
        raise ValueError("UTXOs file must be a non-empty JSON array.")
    for u in utxos:
        if not all(k in u for k in ("txid", "vout", "value")):
            raise ValueError(
                'Each UTXO must have "txid", "vout", and "value" fields. '
                f'Got: {list(u.keys())}'
            )
    return utxos


def fetch_btc_price() -> float:
    try:
        r = _requests.get(f"{MEMPOOL_API}/v1/prices", timeout=10)
        r.raise_for_status()
        return float(r.json().get("USD", 0))
    except Exception:
        return 0.0


# ── Transaction construction ──────────────────────────────────────────────────

def _op_return_script(message: bytes) -> _Script:
    """Build OP_RETURN <message> — value must be 0, unspendable."""
    msg = message[:80]
    if len(msg) <= 75:
        push = bytes([len(msg)])
    elif len(msg) <= 255:
        push = b'\x4c' + bytes([len(msg)])   # OP_PUSHDATA1
    else:
        push = b'\x4d' + len(msg).to_bytes(2, "little")  # OP_PUSHDATA2
    return _Script(b'\x6a' + push + msg)     # OP_RETURN


def build_scorch_psbt(utxos: list, key_data: dict) -> _PSBT:
    """
    Build an unsigned PSBT spending ALL UTXOs to a single OP_RETURN output.

    Total miner fee = sum(all inputs).  OP_RETURN output value = 0.
    The entire treasury balance is permanently destroyed.

    Configures each PSBT input for K_scorch's Taproot script path spend:
      - taproot_internal_key: extracted from control block bytes 1–32
      - taproot_scripts: {control_block: tap_leaf_script + leaf_version_byte}
    """
    treasury_script = _addr2script(TREASURY_ADDRESS)
    op_return       = _op_return_script(SCORCH_MESSAGE)

    vin = [
        _TxIn(bytes.fromhex(u["txid"])[::-1], u["vout"], sequence=0xFFFFFFFE)
        for u in utxos
    ]
    vout = [_TxOut(0, op_return)]   # 0 BTC output; entire input value = miner fee

    tx   = _Tx(vin=vin, vout=vout)
    psbt = _PSBT(tx)

    ctrl            = key_data["control_block"]
    leaf_script     = key_data["tap_leaf_script"]
    leaf_version    = 0xC0                    # Tapscript leaf version
    # The internal key is K_admin's x-only pubkey, encoded in the control block
    internal_xonly  = ctrl[1:33]

    # taproot_scripts value format: script_bytes + bytes([leaf_version])
    tap_scripts_val = leaf_script + bytes([leaf_version])

    for i, u in enumerate(utxos):
        psbt.inputs[i].witness_utxo        = _TxOut(u["value"], treasury_script)
        psbt.inputs[i].taproot_internal_key = _ec.PublicKey.from_xonly(internal_xonly)
        psbt.inputs[i].taproot_scripts[ctrl] = tap_scripts_val

    return psbt


def psbt_to_raw_tx(psbt: _PSBT) -> bytes:
    """
    Finalize the PSBT for script path Taproot spending and return raw tx bytes.

    Assembles the Taproot script path witness: [sig, script, control_block].
    """
    vin = []
    for i, inp in enumerate(psbt.inputs):
        tx_in = inp.vin
        w     = inp.final_scriptwitness  # set for key path; None for script path

        if not w and inp.taproot_sigs:
            # Script path: build witness [sig, script, ctrl_block]
            for (pub, leaf), sig in inp.taproot_sigs.items():
                for ctrl, sc in inp.taproot_scripts.items():
                    lv    = sc[-1]
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
                f"Input {i} is unsigned. "
                "Verify that tap_leaf_script_hex and control_block_hex in the "
                "keyfile are correct for this treasury wallet."
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


# ── UI ────────────────────────────────────────────────────────────────────────

def print_scorch_header() -> None:
    print(f"\n{RED}{BOLD}")
    print("  ╔═══════════════════════════════════════════════════════════╗")
    print("  ║      ☢   AIUNION SCORCH — NUCLEAR OPTION   ☢             ║")
    print("  ║                                                           ║")
    print("  ║  This will PERMANENTLY DESTROY all treasury funds.       ║")
    print("  ║  ALL Bitcoin becomes miner fees. IRREVERSIBLE.           ║")
    print("  ║  This script does NOT broadcast automatically.           ║")
    print("  ╚═══════════════════════════════════════════════════════════╝")
    print(f"{RST}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="AIUNION Scorch Key — NUCLEAR OPTION (manual use only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scorch.py                      # fetch UTXOs live\n"
            "  python scorch.py --utxos utxos.json   # air-gapped mode\n\n"
            "This script does NOT broadcast automatically.\n"
            "Submit the resulting hex file to mempool.space manually."
        ),
    )
    ap.add_argument(
        "--utxos",
        metavar="FILE",
        help=(
            "JSON file with UTXOs for air-gapped use (no network required). "
            'Format: [{"txid":"...","vout":0,"value":12345}, ...]'
        ),
    )
    args = ap.parse_args()

    print_scorch_header()

    # ── Fetch or load UTXOs ───────────────────────────────────────────────────
    if args.utxos:
        print(f"  Loading UTXOs from file: {args.utxos}")
        try:
            utxos = load_utxos_file(args.utxos)
        except Exception as e:
            print(f"  {RED}ERROR loading UTXOs: {e}{RST}")
            sys.exit(1)
        btc_price = 0.0
        print(f"  {AMBER}Air-gapped mode — not connecting to network.{RST}")
    else:
        print(f"  Fetching UTXOs from mempool.space ...")
        try:
            utxos = fetch_utxos_live(TREASURY_ADDRESS)
        except Exception as e:
            print(f"  {RED}ERROR fetching UTXOs: {e}{RST}")
            print(f"  For air-gapped use: python scorch.py --utxos utxos.json")
            sys.exit(1)
        try:
            btc_price = fetch_btc_price()
        except Exception:
            btc_price = 0.0

    if not utxos:
        print(f"\n  {AMBER}No UTXOs found at treasury address. Treasury may already be empty.{RST}")
        sys.exit(0)

    total_sats = sum(u["value"] for u in utxos)
    total_btc  = total_sats / 1e8
    total_usd  = total_btc * btc_price if btc_price else None

    print(f"\n  {AMBER}UTXOs to be destroyed ({len(utxos)} total):{RST}")
    for u in utxos:
        status = u.get("status", {})
        conf   = "confirmed" if status.get("confirmed") else "UNCONFIRMED"
        print(f"    {u['txid'][:16]}...:{u['vout']}  {u['value']:>14,} sat  [{conf}]")

    print(f"\n  {RED}{BOLD}TOTAL TO BE DESTROYED: {total_btc:.8f} BTC", end="")
    if total_usd:
        print(f"  (≈ ${total_usd:,.2f} USD)", end="")
    print(f"{RST}")
    print(f"  {RED}The entire amount becomes miner fees. OP_RETURN = 0 BTC.{RST}")

    # ── Load K_scorch key ─────────────────────────────────────────────────────
    print()
    keyfile_path = input(f"{AMBER}K_scorch keyfile path: {RST}").strip().strip("'\"")
    if not os.path.isfile(keyfile_path):
        print(f"  {RED}ERROR: Keyfile not found: {keyfile_path}{RST}")
        sys.exit(1)

    password = getpass(f"{AMBER}K_scorch passphrase:   {RST}")

    print(f"\n  Loading K_scorch key ... ", end="", flush=True)
    try:
        key_data = load_scorch_keyfile(keyfile_path, password)
    except Exception as e:
        print(f"{RED}FAILED{RST}")
        print(f"\n  {RED}ERROR: {e}{RST}")
        sys.exit(1)
    finally:
        password = None

    print(f"{GREEN}OK{RST}")
    print(f"  Scorch pubkey (x-only): {key_data['xonly_pubkey'].hex()}")
    ctrl = key_data["control_block"]
    print(f"  Internal key (from CB): {ctrl[1:33].hex()}")
    print(f"  Control block ({len(ctrl)} bytes): {ctrl.hex()}")

    # ── Build transaction ─────────────────────────────────────────────────────
    print(f"\n  Building scorch transaction ...")
    try:
        psbt = build_scorch_psbt(utxos, key_data)
    except Exception as e:
        print(f"  {RED}ERROR building transaction: {e}{RST}")
        clear_key_dict(key_data)
        sys.exit(1)

    # ── FINAL WARNING — require exact confirmation phrase ─────────────────────
    print(f"\n{RED}{BOLD}")
    print("  ┌──────────────────────────────────────────────────────────────┐")
    print(f"  │  FINAL WARNING:  {total_btc:.8f} BTC WILL BE PERMANENTLY DESTROYED  │")
    print("  │                                                              │")
    print("  │  Type exactly:   CONFIRM SCORCH                             │")
    print("  │  Anything else aborts immediately.                          │")
    print("  └──────────────────────────────────────────────────────────────┘")
    print(f"{RST}")
    confirm = input(f"{RED}{BOLD}Confirm destruction: {RST}").strip()

    if confirm != "CONFIRM SCORCH":
        print(f"\n  {GREEN}Aborted. No transaction was created.{RST}\n")
        clear_key_dict(key_data)
        sys.exit(0)

    # ── Sign ──────────────────────────────────────────────────────────────────
    print(f"\n  Signing with K_scorch (Taproot script path) ...")
    try:
        privkey  = _ec.PrivateKey(key_data["privkey_bytes"])
        n_signed = psbt.sign_with(privkey)
    except Exception as e:
        print(f"  {RED}ERROR signing: {e}{RST}")
        clear_key_dict(key_data)
        sys.exit(1)
    finally:
        clear_key_dict(key_data)

    if n_signed == 0:
        print(f"\n  {RED}ERROR: K_scorch signed 0 inputs.{RST}")
        print(f"  Check that tap_leaf_script_hex and control_block_hex in the keyfile")
        print(f"  correspond to K_scorch's leaf in the actual treasury wallet.")
        sys.exit(1)

    print(f"  {GREEN}Signed {n_signed} input(s).{RST}")

    # ── Finalize ──────────────────────────────────────────────────────────────
    print(f"  Assembling witness and finalizing ...")
    try:
        raw_bytes = psbt_to_raw_tx(psbt)
        raw_hex   = raw_bytes.hex()
    except Exception as e:
        print(f"  {RED}ERROR finalizing: {e}{RST}")
        sys.exit(1)

    # ── Output ────────────────────────────────────────────────────────────────
    ts       = int(time.time())
    out_file = f"scorch_tx_{ts}.hex"

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(raw_hex)

    # Also dump PSBT for reference
    psbt_out = f"scorch_tx_{ts}.psbt"
    with open(psbt_out, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(psbt.serialize()).decode())

    print(f"\n  {RED}{BOLD}SCORCH TRANSACTION CREATED — NOT YET BROADCAST{RST}")
    print(f"  Raw hex file:  {out_file}  ({len(raw_bytes)} bytes)")
    print(f"  PSBT file:     {psbt_out}")
    print()
    print(f"  {AMBER}BROADCAST INSTRUCTIONS (only proceed if you are certain):{RST}")
    print(f"  1. Open: https://mempool.space/tx/push")
    print(f"  2. Paste the full contents of: {out_file}")
    print(f"  3. Click 'Broadcast Transaction'")
    print()
    print(f"  {RED}{BOLD}Once confirmed, ALL {total_btc:.8f} BTC is GONE FOREVER.{RST}")
    if total_usd:
        print(f"  {RED}That is approximately ${total_usd:,.2f} USD at current prices.{RST}")
    print()


if __name__ == "__main__":
    # Refuse to run if imported rather than executed directly.
    main()
