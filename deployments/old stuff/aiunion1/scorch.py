#!/usr/bin/env python3
"""
K_scorch Dead Man's Switch
--------------------------
Accepts a Nunchuk P2WSH PSBT export, finalises it, encrypts it,
and broadcasts it automatically if no check-in occurs within 364 days.

Dependencies:
    pip install cryptography python-bitcoinlib

Usage:
  python scorch.py genkey                   -- generate key file (store offline)
  python scorch.py setup --psbt <file>      -- load, finalise, encrypt and arm
  python scorch.py checkin                  -- reset the countdown timer
  python scorch.py status                   -- show time remaining
  python scorch.py run --key-file <path>    -- start the daemon
  python scorch.py trigger --key-file <path>-- manually broadcast now
"""

import os
import sys
import json
import time
import base64
import struct
import hashlib
import logging
import argparse
import datetime
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COUNTDOWN_DAYS   = 364
STATE_FILE       = "state.json"
PSBT_FILE        = "burn.psbt.enc"
KEY_FILE         = "scorch.key"
LOG_FILE         = "scorch.log"
CHECK_INTERVAL_S = 3600

BROADCAST_APIS = [
    "https://mempool.space/api/tx",
    "https://blockstream.info/api/tx",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scorch")

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _get_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        print("Missing dependency. Run: pip install cryptography")
        sys.exit(1)

# ---------------------------------------------------------------------------
# PSBT Parsing (BIP174 P2WSH)
# ---------------------------------------------------------------------------
#
# Nunchuk exports PSBTs as base64. For a 1-of-8 P2WSH sortedmulti,
# after K_scorch signs, each input contains:
#   - PSBT_IN_PARTIAL_SIG: {pubkey -> DER signature}
#   - PSBT_IN_WITNESS_SCRIPT: the redeem script
#
# Finalisation builds the witness stack:
#   [OP_0, <sig>, <witness_script>]
# (OP_0 is required by CHECKMULTISIG bug, then 1 sig, then the script)

PSBT_MAGIC             = b'psbt\xff'
PSBT_GLOBAL_UNSIGNED_TX = 0x00
PSBT_IN_NON_WITNESS_UTXO  = 0x00
PSBT_IN_WITNESS_UTXO      = 0x01
PSBT_IN_PARTIAL_SIG       = 0x02
PSBT_IN_SIGHASH_TYPE      = 0x03
PSBT_IN_REDEEM_SCRIPT     = 0x04
PSBT_IN_WITNESS_SCRIPT    = 0x05
PSBT_IN_FINAL_SCRIPTSIG   = 0x07
PSBT_IN_FINAL_SCRIPTWITNESS = 0x08


def _read_varint(data: bytes, offset: int):
    b = data[offset]
    offset += 1
    if b < 0xfd:
        return b, offset
    elif b == 0xfd:
        return struct.unpack_from('<H', data, offset)[0], offset + 2
    elif b == 0xfe:
        return struct.unpack_from('<I', data, offset)[0], offset + 4
    else:
        return struct.unpack_from('<Q', data, offset)[0], offset + 8


def _write_varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    else:
        return b'\xff' + struct.pack('<Q', n)


def _read_kv(data: bytes, offset: int):
    """Read one PSBT key-value pair. Returns (key, value, offset) or (None, None, offset) at separator."""
    key_len, offset = _read_varint(data, offset)
    if key_len == 0:
        return None, None, offset
    key = data[offset:offset + key_len]
    offset += key_len
    val_len, offset = _read_varint(data, offset)
    value = data[offset:offset + val_len]
    offset += val_len
    return key, value, offset


def _parse_psbt(raw: bytes) -> dict:
    """
    Parse a BIP174 PSBT binary.
    Returns { 'unsigned_tx_bytes': bytes, 'inputs': [{ type -> value }] }
    """
    if not raw.startswith(PSBT_MAGIC):
        raise ValueError("Not a valid PSBT — magic bytes missing.")

    offset = len(PSBT_MAGIC)
    unsigned_tx_bytes = None

    # --- Global map ---
    while offset < len(raw):
        key, value, offset = _read_kv(raw, offset)
        if key is None:
            break
        key_type = key[0]
        if key_type == PSBT_GLOBAL_UNSIGNED_TX:
            unsigned_tx_bytes = value

    if unsigned_tx_bytes is None:
        raise ValueError("PSBT missing unsigned transaction.")

    # Count inputs from the unsigned tx
    tx_offset = 4  # skip version
    in_count, tx_offset = _read_varint(unsigned_tx_bytes, tx_offset)

    # --- Input maps ---
    inputs = []
    for _ in range(in_count):
        inp = {
            'partial_sigs': {},   # pubkey_bytes -> sig_bytes
            'witness_script': None,
            'witness_utxo': None,
            'final_scriptwitness': None,
        }
        while offset < len(raw):
            key, value, offset = _read_kv(raw, offset)
            if key is None:
                break
            key_type = key[0]
            if key_type == PSBT_IN_PARTIAL_SIG:
                pubkey = key[1:]
                inp['partial_sigs'][pubkey] = value
            elif key_type == PSBT_IN_WITNESS_SCRIPT:
                inp['witness_script'] = value
            elif key_type == PSBT_IN_WITNESS_UTXO:
                inp['witness_utxo'] = value
            elif key_type == PSBT_IN_FINAL_SCRIPTWITNESS:
                inp['final_scriptwitness'] = value
        inputs.append(inp)

    return {
        'unsigned_tx_bytes': unsigned_tx_bytes,
        'inputs': inputs,
        'in_count': in_count,
    }


def _parse_unsigned_tx(tx_bytes: bytes) -> dict:
    """Parse the unsigned tx from a PSBT (no witness data)."""
    offset = 0
    version = struct.unpack_from('<i', tx_bytes, offset)[0]
    offset += 4
    in_count, offset = _read_varint(tx_bytes, offset)
    vin = []
    for _ in range(in_count):
        txid = tx_bytes[offset:offset + 32]
        offset += 32
        vout = struct.unpack_from('<I', tx_bytes, offset)[0]
        offset += 4
        script_len, offset = _read_varint(tx_bytes, offset)
        script = tx_bytes[offset:offset + script_len]
        offset += script_len
        sequence = struct.unpack_from('<I', tx_bytes, offset)[0]
        offset += 4
        vin.append({'txid': txid, 'vout': vout, 'script': script, 'sequence': sequence})
    out_count, offset = _read_varint(tx_bytes, offset)
    vout = []
    for _ in range(out_count):
        value = struct.unpack_from('<q', tx_bytes, offset)[0]
        offset += 8
        script_len, offset = _read_varint(tx_bytes, offset)
        script = tx_bytes[offset:offset + script_len]
        offset += script_len
        vout.append({'value': value, 'script': script})
    locktime = struct.unpack_from('<I', tx_bytes, offset)[0]
    return {'version': version, 'vin': vin, 'vout': vout, 'locktime': locktime}


def _serialise_final_tx(tx: dict, witnesses: list) -> bytes:
    """Serialise a finalised segwit transaction."""
    out = struct.pack('<i', tx['version'])
    out += b'\x00\x01'  # segwit marker + flag
    out += _write_varint(len(tx['vin']))
    for inp in tx['vin']:
        out += inp['txid']
        out += struct.pack('<I', inp['vout'])
        out += _write_varint(len(inp['script']))
        out += inp['script']
        out += struct.pack('<I', inp['sequence'])
    out += _write_varint(len(tx['vout']))
    for o in tx['vout']:
        out += struct.pack('<q', o['value'])
        out += _write_varint(len(o['script']))
        out += o['script']
    # Witness
    for witness_stack in witnesses:
        out += _write_varint(len(witness_stack))
        for item in witness_stack:
            out += _write_varint(len(item))
            out += item
    out += struct.pack('<I', tx['locktime'])
    return out


def _encode_witness(stack: list) -> bytes:
    """Encode a witness stack to bytes (for PSBT_IN_FINAL_SCRIPTWITNESS)."""
    out = _write_varint(len(stack))
    for item in stack:
        out += _write_varint(len(item))
        out += item
    return out


def finalise_p2wsh_psbt(psbt_input: str) -> str:
    """
    Accept a Nunchuk PSBT export (base64 or hex), finalise it for
    P2WSH sortedmulti, and return the final raw transaction as hex.

    For 1-of-8 sortedmulti the witness stack is:
        OP_0 (empty bytes — CHECKMULTISIG bug)
        <one DER signature>
        <witness script>
    """
    # Decode input — Nunchuk exports base64
    psbt_input = psbt_input.strip()
    try:
        raw = base64.b64decode(psbt_input)
        if not raw.startswith(PSBT_MAGIC):
            raise ValueError()
    except Exception:
        try:
            raw = bytes.fromhex(psbt_input)
        except Exception:
            raise ValueError("Could not decode PSBT — expected base64 or hex.")

    log.info(f"Parsing PSBT ({len(raw)} bytes)...")
    psbt = _parse_psbt(raw)
    tx = _parse_unsigned_tx(psbt['unsigned_tx_bytes'])

    witnesses = []
    for i, inp in enumerate(psbt['inputs']):
        # If already finalised, use existing witness
        if inp['final_scriptwitness'] is not None:
            log.info(f"Input {i}: already finalised.")
            # Decode existing witness stack
            fw = inp['final_scriptwitness']
            stack_count, off = _read_varint(fw, 0)
            stack = []
            for _ in range(stack_count):
                item_len, off = _read_varint(fw, off)
                stack.append(fw[off:off + item_len])
                off += item_len
            witnesses.append(stack)
            continue

        partial_sigs = inp['partial_sigs']
        witness_script = inp['witness_script']

        if not partial_sigs:
            raise ValueError(
                f"Input {i} has no signatures. "
                "Make sure K_scorch has signed the PSBT in Nunchuk before exporting."
            )
        if witness_script is None:
            raise ValueError(
                f"Input {i} missing witness script. "
                "Export the PSBT from the wallet coordinator (not just the signer)."
            )

        # For sortedmulti, signatures must be ordered by pubkey sort order
        # python-bitcoinlib sorts automatically, but we do it manually here
        # to avoid the dependency for this step
        if len(partial_sigs) < 1:
            raise ValueError(f"Input {i}: need at least 1 signature, found 0.")

        # Take the first (and for 1-of-N, only) signature
        sig_bytes = list(partial_sigs.values())[0]

        # P2WSH witness stack for 1-of-N multisig:
        # [b'' (OP_0 bug padding), sig, witness_script]
        witness_stack = [
            b'',           # OP_0 — required by CHECKMULTISIG off-by-one bug
            sig_bytes,     # DER-encoded signature + sighash byte
            witness_script,
        ]
        witnesses.append(witness_stack)
        log.info(f"Input {i}: finalised with 1 signature.")

    final_tx_bytes = _serialise_final_tx(tx, witnesses)
    tx_hex = final_tx_bytes.hex()
    log.info(f"Finalised transaction: {len(final_tx_bytes)} bytes")
    return tx_hex


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def derive_key(key_file_path: str) -> bytes:
    if not os.path.exists(key_file_path):
        raise FileNotFoundError(f"Key file not found: {key_file_path}")
    with open(key_file_path, 'rb') as f:
        return hashlib.sha256(f.read()).digest()

def encrypt_tx(tx_hex: str, key_file_path: str, output_path: str):
    AESGCM = _get_crypto()
    key = derive_key(key_file_path)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, tx_hex.encode(), None)
    with open(output_path, 'w') as f:
        f.write(base64.b64encode(nonce + ct).decode())
    log.info(f"Encrypted transaction saved to {output_path}")

def decrypt_tx(key_file_path: str, encrypted_path: str) -> str:
    AESGCM = _get_crypto()
    key = derive_key(key_file_path)
    with open(encrypted_path, 'r') as f:
        raw = base64.b64decode(f.read())
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()

# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------

def generate_key_file(path: str):
    if os.path.exists(path):
        c = input(f"Key file {path} already exists. Overwrite? (yes/no): ")
        if c.strip().lower() != 'yes':
            print("Aborted.")
            return
    key_bytes = os.urandom(64)
    with open(path, 'wb') as f:
        f.write(key_bytes)
    b64 = base64.b64encode(key_bytes).decode()
    print(f"\n✓ Key file written: {path}")
    print(f"\nBase64 (for offline backup):\n{b64}\n")
    print("⚠  Store this key file OFFLINE and SEPARATE from the server.")
    print("⚠  Without it the encrypted transaction cannot be decrypted.")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {'last_checkin': None}

def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def now_ts() -> float:
    return time.time()

def deadline_ts(last_checkin: float) -> float:
    return last_checkin + (COUNTDOWN_DAYS * 86400)

def time_remaining(last_checkin: float) -> datetime.timedelta:
    return datetime.timedelta(seconds=max(0, deadline_ts(last_checkin) - now_ts()))

# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def broadcast_transaction(tx_hex: str) -> str:
    bytes.fromhex(tx_hex)  # validate hex
    for api_url in BROADCAST_APIS:
        try:
            log.info(f"Broadcasting to {api_url} ...")
            req = urllib.request.Request(
                api_url,
                data=tx_hex.encode(),
                headers={'Content-Type': 'text/plain'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                txid = resp.read().decode().strip()
                log.info(f"✓ Broadcast success. txid: {txid}")
                return txid
        except urllib.error.HTTPError as e:
            log.warning(f"HTTP {e.code} from {api_url}: {e.read().decode()}")
        except Exception as e:
            log.warning(f"Failed {api_url}: {e}")
    raise RuntimeError("All broadcast APIs failed.")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_genkey(args):
    generate_key_file(args.key_file)

def cmd_setup(args):
    """Load a Nunchuk PSBT, finalise it, encrypt it, and arm the switch."""
    if not os.path.exists(args.key_file):
        print(f"Key file not found: {args.key_file}")
        print("Run: python scorch.py genkey")
        sys.exit(1)

    # Read PSBT — from file or stdin
    if args.psbt and os.path.exists(args.psbt):
        with open(args.psbt, 'r') as f:
            psbt_data = f.read().strip()
        log.info(f"Loaded PSBT from file: {args.psbt}")
    elif args.psbt:
        psbt_data = args.psbt.strip()
    else:
        print("Paste your Nunchuk PSBT export (base64), then press Enter + Ctrl-D:")
        psbt_data = sys.stdin.read().strip()

    if not psbt_data:
        print("No PSBT provided. Aborted.")
        sys.exit(1)

    # Finalise
    log.info("Finalising P2WSH PSBT...")
    try:
        tx_hex = finalise_p2wsh_psbt(psbt_data)
    except Exception as e:
        log.error(f"Finalisation failed: {e}")
        sys.exit(1)

    log.info(f"Final tx hex ({len(tx_hex) // 2} bytes): {tx_hex[:64]}...")

    # Encrypt and store
    encrypt_tx(tx_hex, args.key_file, PSBT_FILE)

    # Arm
    state = {'last_checkin': now_ts(), 'created': now_ts()}
    save_state(state)
    remaining = time_remaining(state['last_checkin'])

    print(f"\n✓ Switch armed. Will fire in {remaining} if no check-in.")
    print(f"  Encrypted tx: {PSBT_FILE}  ← stays on server")
    print(f"  Key file:     {args.key_file}  ← move OFFLINE now")
    print(f"\n  Annual check-in command:")
    print(f"  python scorch.py checkin\n")

def cmd_checkin(args):
    state = load_state()
    if not state.get('last_checkin'):
        print("Switch not armed. Run setup first.")
        sys.exit(1)
    state['last_checkin'] = now_ts()
    save_state(state)
    remaining = time_remaining(state['last_checkin'])
    log.info(f"Check-in recorded. Next deadline in {remaining}.")
    print(f"✓ Check-in recorded. Timer reset to {remaining}.")

def cmd_status(args):
    state = load_state()
    if not state.get('last_checkin'):
        print("Switch not armed.")
        return
    last = datetime.datetime.fromtimestamp(state['last_checkin'])
    remaining = time_remaining(state['last_checkin'])
    deadline = datetime.datetime.fromtimestamp(deadline_ts(state['last_checkin']))
    fired = remaining.total_seconds() == 0
    print(f"\n  Status:         {'⚠ EXPIRED' if fired else '✓ Armed'}")
    print(f"  Last check-in:  {last.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Deadline:       {deadline.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Time remaining: {remaining}")
    print(f"  Encrypted tx:   {'✓ present' if os.path.exists(PSBT_FILE) else '✗ MISSING'}\n")

def cmd_trigger(args):
    confirm = input("⚠ This permanently destroys all funds. Type BURN to confirm: ")
    if confirm.strip() != 'BURN':
        print("Aborted.")
        return
    _execute_burn(args.key_file)

def cmd_run(args):
    log.info(f"Daemon started. Checking every {CHECK_INTERVAL_S}s. Countdown: {COUNTDOWN_DAYS} days.")
    while True:
        state = load_state()
        if not state.get('last_checkin'):
            log.warning("Switch not armed.")
        else:
            remaining = time_remaining(state['last_checkin'])
            log.info(f"Time remaining: {remaining}")
            if remaining.total_seconds() <= 0:
                log.critical("DEADLINE PASSED — initiating burn.")
                _execute_burn(args.key_file)
                sys.exit(0)
        time.sleep(CHECK_INTERVAL_S)

def _execute_burn(key_file: str):
    if not os.path.exists(PSBT_FILE):
        log.critical(f"Encrypted tx not found: {PSBT_FILE}")
        sys.exit(1)
    if not os.path.exists(key_file):
        log.critical(f"Key file not found: {key_file}")
        sys.exit(1)
    log.critical("Decrypting transaction...")
    tx_hex = decrypt_tx(key_file, PSBT_FILE)
    log.critical("Broadcasting burn transaction...")
    try:
        txid = broadcast_transaction(tx_hex)
        log.critical(f"BURN COMPLETE. txid: {txid}")
        with open('burn_record.txt', 'w') as f:
            f.write(f"Burned: {datetime.datetime.utcnow().isoformat()}\n")
            f.write(f"txid: {txid}\n")
    except Exception as e:
        log.critical(f"Broadcast failed: {e}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="K_scorch Dead Man's Switch — P2WSH")
    parser.add_argument('--key-file', default=KEY_FILE, help='Path to decryption key file')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('genkey',  help='Generate a new key file (store offline)')
    p = sub.add_parser('setup', help='Load Nunchuk PSBT, finalise, encrypt and arm')
    p.add_argument('--psbt', default=None, help='Path to PSBT file, or raw base64 string')
    sub.add_parser('checkin', help='Reset the 364-day countdown')
    sub.add_parser('status',  help='Show time remaining')
    sub.add_parser('run',     help='Start the daemon')
    sub.add_parser('trigger', help='Manually broadcast now (emergency)')

    args = parser.parse_args()
    if args.command not in ('genkey', 'setup', 'checkin', 'status', 'run', 'trigger'):
        parser.print_help()
        sys.exit(1)

    {
        'genkey':  cmd_genkey,
        'setup':   cmd_setup,
        'checkin': cmd_checkin,
        'status':  cmd_status,
        'run':     cmd_run,
        'trigger': cmd_trigger,
    }[args.command](args)

if __name__ == '__main__':
    main()
