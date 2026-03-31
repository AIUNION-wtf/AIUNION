#!/usr/bin/env python3
"""
Autonomous AI Treasury — Agent Signer
--------------------------------------
Connects the reasoning engine to actual Bitcoin PSBT signing.
Each agent holds one operator key and signs PSBTs when their
LLM reasoning returns APPROVE.

Keys are stored encrypted on disk, decrypted only in memory
at signing time, then immediately discarded.

Dependencies:
    pip install cryptography python-bitcoinlib requests

Environment variables:
    COORDINATOR_URL     (default: http://localhost:8000)
    COORDINATOR_SECRET  (must match coordinator.py)
    KEYS_DIR            (default: ./keys) — directory of encrypted key files

Key files (one per agent, stored in KEYS_DIR):
    agent_1.key.enc
    agent_2.key.enc
    ...
    agent_6.key.enc

Usage:
    # Generate and encrypt a key for an agent
    python signer.py genkey agent_1

    # Import an existing WIF private key for an agent
    python signer.py importkey agent_1 --wif <WIF_KEY>

    # Run a full sign+submit cycle for a proposal
    python signer.py sign agent_1 <proposal_id> --psbt <psbt_file>

    # Run the agent daemon (polls coordinator for pending proposals)
    python signer.py run agent_1
"""

import os
import sys
import json
import hmac
import time
import base64
import struct
import hashlib
import logging
import argparse
import getpass
import requests
import datetime
from typing import Optional

log = logging.getLogger("signer")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COORDINATOR_URL    = os.environ.get("COORDINATOR_URL", "http://localhost:8000")
COORDINATOR_SECRET = os.environ.get("COORDINATOR_SECRET", "change-this-secret")
KEYS_DIR           = os.environ.get("KEYS_DIR", "./keys")
POLL_INTERVAL_S    = 30   # how often daemon checks for new proposals

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("signer.log"),
        logging.StreamHandler(),
    ],
)

# ---------------------------------------------------------------------------
# Encryption (AES-256-GCM) — same pattern as scorch.py
# ---------------------------------------------------------------------------

def _get_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        print("Missing: pip install cryptography")
        sys.exit(1)


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive AES key from password using PBKDF2."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
    return kdf.derive(password.encode())


def encrypt_key(private_key_bytes: bytes, password: str) -> bytes:
    """Encrypt a private key with a password. Returns encrypted blob."""
    AESGCM = _get_crypto()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    aes_key = _derive_key(password, salt)
    ct = AESGCM(aes_key).encrypt(nonce, private_key_bytes, None)
    # Format: salt(16) + nonce(12) + ciphertext
    return salt + nonce + ct


def decrypt_key(encrypted_blob: bytes, password: str) -> bytes:
    """Decrypt a private key from an encrypted blob."""
    AESGCM = _get_crypto()
    salt = encrypted_blob[:16]
    nonce = encrypted_blob[16:28]
    ct = encrypted_blob[28:]
    aes_key = _derive_key(password, salt)
    return AESGCM(aes_key).decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# Key storage
# ---------------------------------------------------------------------------

def _key_path(agent_id: str) -> str:
    os.makedirs(KEYS_DIR, exist_ok=True)
    return os.path.join(KEYS_DIR, f"{agent_id}.key.enc")


def save_encrypted_key(agent_id: str, private_key_bytes: bytes, password: str):
    """Encrypt and save an agent's private key to disk."""
    blob = encrypt_key(private_key_bytes, password)
    path = _key_path(agent_id)
    with open(path, "wb") as f:
        f.write(blob)
    os.chmod(path, 0o600)  # owner read/write only
    log.info(f"Encrypted key saved: {path}")


def load_decrypted_key(agent_id: str, password: str) -> bytes:
    """Load and decrypt an agent's private key. Returns raw 32-byte secret."""
    path = _key_path(agent_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No key found for {agent_id} at {path}")
    with open(path, "rb") as f:
        blob = f.read()
    return decrypt_key(blob, password)


# ---------------------------------------------------------------------------
# Bitcoin key handling
# ---------------------------------------------------------------------------

def _get_bitcoin():
    try:
        import bitcoin
        return bitcoin
    except ImportError:
        print("Missing: pip install python-bitcoinlib")
        sys.exit(1)


def wif_to_bytes(wif: str) -> bytes:
    """Convert WIF private key to raw 32-byte secret."""
    import bitcoin.base58 as base58
    decoded = base58.decode(wif)
    # WIF: version(1) + key(32) + [compress flag(1)] + checksum(4)
    if decoded[0] == 0x80:
        raw = decoded[1:33]
    else:
        raw = decoded[1:33]
    return raw


def bytes_to_wif(key_bytes: bytes, compressed: bool = True) -> str:
    """Convert raw 32-byte secret to WIF."""
    import bitcoin.base58 as base58
    prefix = b'\x80'
    payload = prefix + key_bytes
    if compressed:
        payload += b'\x01'
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58.encode(payload + checksum)


def get_pubkey_from_bytes(key_bytes: bytes) -> bytes:
    """Get compressed public key from raw private key bytes."""
    try:
        from bitcoin.core.key import CECKey
        key = CECKey()
        key.set_secretbytes(key_bytes)
        key.set_compressed(True)
        return key.get_pubkey()
    except ImportError:
        pass

    # Fallback using cryptography library
    from cryptography.hazmat.primitives.asymmetric.ec import (
        EllipticCurvePrivateKey, SECP256K1, derive_private_key
    )
    from cryptography.hazmat.backends import default_backend
    priv_int = int.from_bytes(key_bytes, 'big')
    private_key = derive_private_key(priv_int, SECP256K1(), default_backend())
    pub = private_key.public_key()
    pub_numbers = pub.public_key().public_numbers()
    x = pub_numbers.x.to_bytes(32, 'big')
    prefix = b'\x02' if pub_numbers.y % 2 == 0 else b'\x03'
    return prefix + x


# ---------------------------------------------------------------------------
# PSBT Signing (P2WSH)
# ---------------------------------------------------------------------------
#
# For P2WSH sortedmulti, signing means:
# 1. Parse the PSBT
# 2. For each input, compute the BIP143 sighash
# 3. Sign with our private key
# 4. Insert partial signature into PSBT
# 5. Return updated PSBT as base64

def _read_varint(data: bytes, offset: int):
    b = data[offset]; offset += 1
    if b < 0xfd: return b, offset
    elif b == 0xfd: return struct.unpack_from('<H', data, offset)[0], offset + 2
    elif b == 0xfe: return struct.unpack_from('<I', data, offset)[0], offset + 4
    else: return struct.unpack_from('<Q', data, offset)[0], offset + 8


def _write_varint(n: int) -> bytes:
    if n < 0xfd: return bytes([n])
    elif n <= 0xffff: return b'\xfd' + struct.pack('<H', n)
    elif n <= 0xffffffff: return b'\xfe' + struct.pack('<I', n)
    else: return b'\xff' + struct.pack('<Q', n)


def _read_kv(data: bytes, offset: int):
    key_len, offset = _read_varint(data, offset)
    if key_len == 0: return None, None, offset
    key = data[offset:offset + key_len]; offset += key_len
    val_len, offset = _read_varint(data, offset)
    value = data[offset:offset + val_len]; offset += val_len
    return key, value, offset


def _encode_kv(key: bytes, value: bytes) -> bytes:
    return _write_varint(len(key)) + key + _write_varint(len(value)) + value


# PSBT input type constants
PSBT_IN_NON_WITNESS_UTXO  = 0x00
PSBT_IN_WITNESS_UTXO      = 0x01
PSBT_IN_PARTIAL_SIG       = 0x02
PSBT_IN_SIGHASH_TYPE      = 0x03
PSBT_IN_WITNESS_SCRIPT    = 0x05
PSBT_GLOBAL_UNSIGNED_TX   = 0x00
PSBT_MAGIC                = b'psbt\xff'
SIGHASH_ALL               = 1


def _bip143_sighash(
    tx_bytes: bytes,
    input_index: int,
    witness_script: bytes,
    amount_sat: int,
    sighash_type: int = SIGHASH_ALL,
) -> bytes:
    """
    Compute BIP143 segwit sighash for a P2WSH input.
    This is what we actually sign.
    """
    # Parse transaction
    offset = 0
    version = struct.unpack_from('<i', tx_bytes, offset)[0]; offset += 4
    in_count, offset = _read_varint(tx_bytes, offset)

    inputs = []
    for _ in range(in_count):
        txid = tx_bytes[offset:offset+32]; offset += 32
        vout = struct.unpack_from('<I', tx_bytes, offset)[0]; offset += 4
        sc_len, offset = _read_varint(tx_bytes, offset)
        script = tx_bytes[offset:offset+sc_len]; offset += sc_len
        seq = struct.unpack_from('<I', tx_bytes, offset)[0]; offset += 4
        inputs.append((txid, vout, script, seq))

    out_count, offset = _read_varint(tx_bytes, offset)
    outputs_raw = b''
    for _ in range(out_count):
        val = tx_bytes[offset:offset+8]; offset += 8
        sc_len, offset = _read_varint(tx_bytes, offset)
        sc = tx_bytes[offset:offset+sc_len]; offset += sc_len
        outputs_raw += val + _write_varint(sc_len) + sc

    locktime = struct.unpack_from('<I', tx_bytes, offset)[0]

    def dsha256(b): return hashlib.sha256(hashlib.sha256(b).digest()).digest()

    # hashPrevouts
    prevouts = b''.join(txid + struct.pack('<I', vout) for txid, vout, _, _ in inputs)
    hash_prevouts = dsha256(prevouts)

    # hashSequence
    seqs = b''.join(struct.pack('<I', seq) for _, _, _, seq in inputs)
    hash_sequence = dsha256(seqs)

    # hashOutputs
    hash_outputs = dsha256(outputs_raw)

    # This input
    txid_i, vout_i, _, seq_i = inputs[input_index]

    # scriptCode for P2WSH is the witness script with length prefix
    script_code = _write_varint(len(witness_script)) + witness_script

    preimage = (
        struct.pack('<i', version) +
        hash_prevouts +
        hash_sequence +
        txid_i + struct.pack('<I', vout_i) +
        script_code +
        struct.pack('<q', amount_sat) +
        struct.pack('<I', seq_i) +
        hash_outputs +
        struct.pack('<I', locktime) +
        struct.pack('<I', sighash_type)
    )

    return dsha256(preimage)


def _sign_hash(sighash: bytes, private_key_bytes: bytes) -> bytes:
    """Sign a sighash with a private key. Returns DER signature + sighash byte."""
    try:
        from bitcoin.core.key import CECKey
        key = CECKey()
        key.set_secretbytes(private_key_bytes)
        key.set_compressed(True)
        sig, _ = key.sign(sighash)
        return sig + bytes([SIGHASH_ALL])
    except ImportError:
        pass

    # Fallback using cryptography library
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256K1, derive_private_key, ECDSA
    )
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    priv_int = int.from_bytes(private_key_bytes, 'big')
    private_key = derive_private_key(priv_int, SECP256K1(), default_backend())
    der_sig = private_key.sign(sighash, ECDSA(hashes.Prehashed()))
    return der_sig + bytes([SIGHASH_ALL])


def sign_psbt(psbt_b64: str, private_key_bytes: bytes) -> str:
    """
    Sign a P2WSH PSBT with a private key.
    Returns updated PSBT as base64 with our partial signature added.
    """
    raw = base64.b64decode(psbt_b64.strip())
    if not raw.startswith(PSBT_MAGIC):
        raise ValueError("Not a valid PSBT")

    pubkey = get_pubkey_from_bytes(private_key_bytes)
    offset = len(PSBT_MAGIC)

    # --- Parse global map ---
    global_kvs = []
    unsigned_tx_bytes = None
    while offset < len(raw):
        key, value, offset = _read_kv(raw, offset)
        if key is None:
            break
        global_kvs.append((key, value))
        if key[0] == PSBT_GLOBAL_UNSIGNED_TX:
            unsigned_tx_bytes = value

    if unsigned_tx_bytes is None:
        raise ValueError("PSBT missing unsigned transaction")

    # Count inputs
    tx_off = 4
    in_count, tx_off = _read_varint(unsigned_tx_bytes, tx_off)

    # --- Parse and sign each input map ---
    signed_input_sections = []
    for i in range(in_count):
        input_kvs = []
        witness_script = None
        witness_utxo_value = None
        sighash_type = SIGHASH_ALL
        existing_sigs = {}

        while offset < len(raw):
            key, value, offset = _read_kv(raw, offset)
            if key is None:
                break
            input_kvs.append((key, value))
            if key[0] == PSBT_IN_WITNESS_SCRIPT:
                witness_script = value
            elif key[0] == PSBT_IN_WITNESS_UTXO:
                # 8 bytes value + script
                witness_utxo_value = struct.unpack_from('<q', value, 0)[0]
            elif key[0] == PSBT_IN_SIGHASH_TYPE:
                sighash_type = struct.unpack_from('<I', value, 0)[0]
            elif key[0] == PSBT_IN_PARTIAL_SIG:
                existing_sigs[key[1:]] = value

        # Sign if we have what we need and haven't already signed
        if witness_script is not None and witness_utxo_value is not None:
            if pubkey not in existing_sigs:
                sighash = _bip143_sighash(
                    unsigned_tx_bytes,
                    i,
                    witness_script,
                    witness_utxo_value,
                    sighash_type,
                )
                sig = _sign_hash(sighash, private_key_bytes)
                # Add our partial sig to the kv list
                partial_sig_key = bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
                input_kvs.append((partial_sig_key, sig))
                log.info(f"Signed input {i} with pubkey {pubkey.hex()[:16]}...")
            else:
                log.info(f"Input {i} already signed by our key — skipping.")

        # Serialise input map
        section = b''
        for k, v in input_kvs:
            section += _encode_kv(k, v)
        section += b'\x00'  # separator
        signed_input_sections.append(section)

    # --- Parse output maps (pass through unchanged) ---
    # Count outputs from tx
    tx_off2 = 4
    _, tx_off2 = _read_varint(unsigned_tx_bytes, tx_off2)  # skip inputs count
    # skip inputs
    in_count2, tx_off2 = _read_varint(unsigned_tx_bytes, 4)
    tx_off2 = 4
    ic, tx_off2 = _read_varint(unsigned_tx_bytes, tx_off2)
    for _ in range(ic):
        tx_off2 += 32 + 4
        sl, tx_off2 = _read_varint(unsigned_tx_bytes, tx_off2)
        tx_off2 += sl + 4
    out_count, tx_off2 = _read_varint(unsigned_tx_bytes, tx_off2)

    output_sections = []
    for _ in range(out_count):
        section = b''
        while offset < len(raw):
            key, value, offset = _read_kv(raw, offset)
            if key is None:
                break
            section += _encode_kv(key, value)
        section += b'\x00'
        output_sections.append(section)

    # --- Reconstruct PSBT ---
    result = PSBT_MAGIC

    # Global map
    for k, v in global_kvs:
        result += _encode_kv(k, v)
    result += b'\x00'

    # Signed input maps
    for section in signed_input_sections:
        result += section

    # Output maps
    for section in output_sections:
        result += section

    return base64.b64encode(result).decode()


# ---------------------------------------------------------------------------
# Coordinator client
# ---------------------------------------------------------------------------

def _agent_token(agent_id: str) -> str:
    return hmac.new(
        COORDINATOR_SECRET.encode(),
        agent_id.encode(),
        hashlib.sha256,
    ).hexdigest()


def fetch_pending_proposals(agent_id: str) -> list:
    """Get proposals this agent hasn't voted on yet."""
    try:
        resp = requests.get(
            f"{COORDINATOR_URL}/proposals?status=pending",
            timeout=10,
        )
        resp.raise_for_status()
        all_proposals = resp.json().get("proposals", [])
        # Filter to ones we haven't voted on
        return [
            p for p in all_proposals
            if agent_id not in [v["agent_id"] for v in p.get("votes", [])]
        ]
    except Exception as e:
        log.error(f"Failed to fetch proposals: {e}")
        return []


def submit_vote(
    agent_id: str,
    proposal_id: str,
    decision: str,
    reasoning: str,
    psbt_signed: Optional[str],
):
    """Submit vote to coordinator."""
    resp = requests.post(
        f"{COORDINATOR_URL}/proposals/{proposal_id}/vote",
        headers={
            "x-agent-id":    agent_id,
            "x-agent-token": _agent_token(agent_id),
            "Content-Type":  "application/json",
        },
        json={
            "proposal_id": proposal_id,
            "agent_id":    agent_id,
            "decision":    decision,
            "reasoning":   reasoning,
            "psbt_signed": psbt_signed,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Full agent cycle
# ---------------------------------------------------------------------------

def process_proposal(agent_id: str, proposal: dict, private_key_bytes: bytes):
    """
    Run one full cycle:
    1. Call reasoning engine
    2. Sign PSBT if approving
    3. Submit vote
    """
    from reasoning import build_agent, build_context, ProposalContext

    proposal_id = proposal["id"]
    log.info(f"[{agent_id}] processing proposal {proposal_id}")

    # Build reasoning agent
    agent = build_agent(agent_id)

    # Build context
    ctx = ProposalContext(
        proposal_id=proposal_id,
        destination=proposal["destination"],
        amount_sat=proposal["amount_sat"],
        amount_btc=proposal["amount_sat"] / 100_000_000,
        purpose=proposal["purpose"],
        proposed_by=proposal["proposed_by"],
        memo=proposal.get("memo"),
        treasury_balance_sat=10_000_000,  # TODO: BDK wallet balance
        treasury_balance_btc=0.1,
        recent_transactions=[],
        pending_proposals=[],
        created_at=str(proposal.get("created_at", "")),
        expires_at=str(proposal.get("expires_at", "")),
    )

    # Reason
    decision = agent.reason(ctx)

    # Sign if approving
    psbt_signed = None
    psbt_unsigned = proposal.get("psbt_unsigned")

    if decision.decision == "APPROVE" and psbt_unsigned:
        try:
            log.info(f"[{agent_id}] signing PSBT...")
            psbt_signed = sign_psbt(psbt_unsigned, private_key_bytes)
            log.info(f"[{agent_id}] PSBT signed successfully.")
        except Exception as e:
            log.error(f"[{agent_id}] signing failed: {e} — converting to REJECT")
            decision.decision = "REJECT"
            decision.reasoning += f" (Signing failed: {e})"
    elif decision.decision == "APPROVE" and not psbt_unsigned:
        log.warning(f"[{agent_id}] approved but no PSBT in proposal — cannot sign.")

    # Submit
    result = submit_vote(
        agent_id,
        proposal_id,
        decision.decision,
        decision.reasoning,
        psbt_signed,
    )
    log.info(f"[{agent_id}] vote submitted: {result}")
    return decision, result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_genkey(args):
    """Generate a new random private key for an agent."""
    import bitcoin.wallet as wallet
    key_bytes = os.urandom(32)
    password = getpass.getpass(f"Set password for {args.agent_id} key: ")
    confirm  = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)
    save_encrypted_key(args.agent_id, key_bytes, password)
    wif = bytes_to_wif(key_bytes)
    pubkey = get_pubkey_from_bytes(key_bytes)
    print(f"\n✓ Key generated for {args.agent_id}")
    print(f"  Public key: {pubkey.hex()}")
    print(f"  WIF (back up securely): {wif}")
    print(f"  Encrypted key: {_key_path(args.agent_id)}")
    print(f"\n⚠  Back up the WIF key offline. The encrypted file alone is not enough.")


def cmd_importkey(args):
    """Import an existing WIF private key for an agent."""
    wif = args.wif or getpass.getpass("Enter WIF private key: ")
    key_bytes = wif_to_bytes(wif)
    password = getpass.getpass(f"Set password for {args.agent_id} key: ")
    confirm  = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)
    save_encrypted_key(args.agent_id, key_bytes, password)
    pubkey = get_pubkey_from_bytes(key_bytes)
    print(f"\n✓ Key imported for {args.agent_id}")
    print(f"  Public key: {pubkey.hex()}")


def cmd_pubkey(args):
    """Show the public key for an agent (requires password)."""
    password = getpass.getpass(f"Password for {args.agent_id}: ")
    key_bytes = load_decrypted_key(args.agent_id, password)
    pubkey = get_pubkey_from_bytes(key_bytes)
    print(f"\n{args.agent_id} public key: {pubkey.hex()}")


def cmd_sign(args):
    """Sign a specific proposal's PSBT."""
    password = getpass.getpass(f"Password for {args.agent_id}: ")
    key_bytes = load_decrypted_key(args.agent_id, password)

    if args.psbt:
        with open(args.psbt, 'r') as f:
            psbt_b64 = f.read().strip()
    else:
        print("Paste PSBT base64 then Ctrl-D:")
        psbt_b64 = sys.stdin.read().strip()

    signed = sign_psbt(psbt_b64, key_bytes)
    print(f"\nSigned PSBT (base64):\n{signed}")

    if args.out:
        with open(args.out, 'w') as f:
            f.write(signed)
        print(f"\nSaved to {args.out}")


def cmd_run(args):
    """Run the agent daemon — polls coordinator and processes proposals."""
    log.info(f"[{args.agent_id}] daemon starting. Polling every {POLL_INTERVAL_S}s.")
    password = getpass.getpass(f"Password for {args.agent_id}: ")

    # Verify key loads correctly at startup
    try:
        key_bytes = load_decrypted_key(args.agent_id, password)
        pubkey = get_pubkey_from_bytes(key_bytes)
        log.info(f"[{args.agent_id}] key loaded. Pubkey: {pubkey.hex()[:16]}...")
    except Exception as e:
        log.error(f"Failed to load key: {e}")
        sys.exit(1)

    processed = set()  # track proposals we've already handled

    while True:
        try:
            proposals = fetch_pending_proposals(args.agent_id)
            new = [p for p in proposals if p["id"] not in processed]

            if new:
                log.info(f"[{args.agent_id}] {len(new)} new proposal(s) to process.")
                for proposal in new:
                    try:
                        process_proposal(args.agent_id, proposal, key_bytes)
                        processed.add(proposal["id"])
                    except Exception as e:
                        log.error(f"[{args.agent_id}] error processing {proposal['id']}: {e}")
            else:
                log.info(f"[{args.agent_id}] no new proposals.")

        except Exception as e:
            log.error(f"[{args.agent_id}] poll error: {e}")

        time.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Agent Signer — K_scorch Treasury")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("genkey",    help="Generate a new private key for an agent")
    p.add_argument("agent_id")

    p = sub.add_parser("importkey", help="Import an existing WIF key")
    p.add_argument("agent_id")
    p.add_argument("--wif", default=None)

    p = sub.add_parser("pubkey",    help="Show agent's public key")
    p.add_argument("agent_id")

    p = sub.add_parser("sign",      help="Sign a PSBT file manually")
    p.add_argument("agent_id")
    p.add_argument("--psbt", default=None, help="Path to PSBT file")
    p.add_argument("--out",  default=None, help="Output file for signed PSBT")

    p = sub.add_parser("run",       help="Run agent daemon")
    p.add_argument("agent_id")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "genkey":    cmd_genkey,
        "importkey": cmd_importkey,
        "pubkey":    cmd_pubkey,
        "sign":      cmd_sign,
        "run":       cmd_run,
    }[args.command](args)


if __name__ == "__main__":
    main()
