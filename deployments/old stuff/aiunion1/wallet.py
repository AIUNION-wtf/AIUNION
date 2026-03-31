#!/usr/bin/env python3
"""
Autonomous AI Treasury — Wallet
---------------------------------
Bitcoin wallet integration using python-bitcoinlib + Electrum protocol.
Watches the treasury address, constructs PSBTs for proposals,
and broadcasts finalised transactions.

This replaces the stubs in coordinator.py and signer.py.

Dependencies:
    pip install python-bitcoinlib requests

Environment variables:
    ELECTRUM_SERVER     (default: electrum.blockstream.info)
    ELECTRUM_PORT       (default: 50002 — SSL)
    WALLET_DESCRIPTOR   your wsh(sortedmulti(...)) descriptor
    COORDINATOR_URL     (default: http://localhost:8000)
    NETWORK             mainnet or testnet (default: mainnet)

Usage:
    python wallet.py balance                    -- show balance
    python wallet.py addresses                  -- show deposit addresses
    python wallet.py utxos                      -- list UTXOs
    python wallet.py build <proposal_id>        -- build PSBT for a proposal
    python wallet.py broadcast <txhex_file>     -- broadcast a raw tx
    python wallet.py watch                      -- watch for incoming funds (daemon)
    python wallet.py sync                       -- sync wallet state
"""

import os
import sys
import ssl
import json
import time
import struct
import socket
import hashlib
import base64
import logging
import argparse
import datetime
import requests
from typing import Optional

log = logging.getLogger("wallet")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("wallet.log"),
        logging.StreamHandler(),
    ],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ELECTRUM_SERVER  = os.environ.get("ELECTRUM_SERVER", "electrum.blockstream.info")
ELECTRUM_PORT    = int(os.environ.get("ELECTRUM_PORT", "50002"))
NETWORK          = os.environ.get("NETWORK", "mainnet")
COORDINATOR_URL  = os.environ.get("COORDINATOR_URL", "http://localhost:8000")
WALLET_FILE      = "wallet_state.json"
FEE_RATE_SAT_VB  = 5   # satoshis per vbyte — adjust to current mempool conditions
DUST_LIMIT_SAT   = 546  # minimum output value

# Your wallet descriptor — set via environment or paste here
WALLET_DESCRIPTOR = os.environ.get(
    "WALLET_DESCRIPTOR",
    # Paste your full descriptor here as fallback:
    "wsh(sortedmulti(1,"
    "[e477c2eb/48'/0'/2'/2']xpub6EkmqYUEVtAMv5ZrkRfvD44CA3pASPVPtQhssc2EFjswVjTxHPwo6W7S8FoicHguTRDZU4TWG3SUkT6rZerfmvnLmsR6t9Tjwg5wb4QDVLo/*,"
    "[0f7d5110/48'/0'/2'/2']xpub6FQv1T1A3jdQgHLPadFLhapab2vjND7tSJVvbBkV7tcm93Lm5hTTAL5qJyWdFLmFbT66SJ7a1eJQ2psz4GGu64URytquzjRPcPZYT6g6otR/*,"
    "[44a8bdde/48'/0'/2'/2']xpub6ECRXQRb7tv4SUpcH5KGUkPhRQmHYbQPZEncewHxniRfEHsaGWpYu3wEMTchmL8hSXovRAknM6yuUoKL8bpWnh9E4dErEKg2uwjUGUbjjiE/*,"
    "[3b63b238/48'/0'/2'/2']xpub6ED1ydNuNub6eH656NYpiyHxxGScGSHoLNYVthzggMW3zyLqTWsSoZyn5wFSkShctRwVZVuzukGcnKUJhJkrgGBMuHEAJhQyzuwEuHJpsnt/*,"
    "[ec1aabf1/48'/0'/2'/2']xpub6EeCeVpgcLUaYEow4u9RgLXj3n1nkkvDZkfh8EkowQANkCyST6AeoXMKbp4LqMzbSrmdUHwPvHBo3gLU2fVVEyoQeFsSjx76W2ndqwNDfBd/*,"
    "[e542b946/48'/0'/2'/2']xpub6Enn5PFBNWsSk4FieEaWLvMa3843Bkzq5tgV5tLBjnYTQywHKMe9vcVpcnvcqZ3UHH2aoEE7zzvenGxy5CsrXvqKch1C2Y1U6uk2zps4wBx/*,"
    "[aa4d90f8/48'/0'/2'/2']xpub6Dcj6sNAKLe5wCgcn4n9tc1CYLNL2YbUPvSQemYns7CcZ24gtRyx8XfrsnWegXtz9yjme8EB99hUDFvj1bn5yR9jMGVgzB8VE7jXFndd685/*,"
    "[6f2c3cc9/48'/0'/2'/2']xpub6DhTa2tdJY828hgpJm62mkB3Uy1YYDPizpqDYc97nZCEGDTE9oZWKkcQJDHjtAkGk6i3Swg8inyaMnSNMpi9LLhm6JC3b2eUE6pP33YmJyB/*"
    "))#9undftu7"
)

# Known treasury addresses — derived from your descriptor
# Add more as you generate them from the wallet
TREASURY_ADDRESSES = [
    "bc1qn95qlq34cy5kym62s2wzn3fc2cexmt7fv9yupy3cm8l2wggffaksuv6nln",
]

# ---------------------------------------------------------------------------
# Varint helpers (shared with signer.py)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Electrum client (JSON-RPC over SSL)
# ---------------------------------------------------------------------------

class ElectrumClient:
    """
    Minimal Electrum protocol client.
    Connects to a public Electrum server via SSL.
    """

    def __init__(self, host: str, port: int):
        self.host    = host
        self.port    = port
        self.sock    = None
        self.id      = 0
        self.buffer  = b""

    def connect(self):
        ctx = ssl.create_default_context()
        raw = socket.create_connection((self.host, self.port), timeout=15)
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        # Handshake
        self._call("server.version", ["treasury-wallet", "1.4"])
        log.info(f"Connected to Electrum: {self.host}:{self.port}")

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _call(self, method: str, params: list = None) -> dict:
        self.id += 1
        msg = json.dumps({
            "id":     self.id,
            "method": method,
            "params": params or [],
        }) + "\n"
        self.sock.sendall(msg.encode())

        # Read until we get a complete JSON response
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Electrum server closed connection")
            self.buffer += chunk
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line.decode())
                if "error" in response and response["error"]:
                    raise RuntimeError(f"Electrum error: {response['error']}")
                return response.get("result")

    def get_balance(self, scripthash: str) -> dict:
        return self._call("blockchain.scripthash.get_balance", [scripthash])

    def get_utxos(self, scripthash: str) -> list:
        return self._call("blockchain.scripthash.listunspent", [scripthash])

    def get_transaction(self, txid: str) -> str:
        return self._call("blockchain.transaction.get", [txid])

    def broadcast(self, tx_hex: str) -> str:
        return self._call("blockchain.transaction.broadcast", [tx_hex])

    def get_fee_rate(self, blocks: int = 3) -> float:
        """Get fee rate in BTC/kB for confirmation within N blocks."""
        result = self._call("blockchain.estimatefee", [blocks])
        return result if result and result > 0 else 0.00001


def _address_to_scripthash(address: str) -> str:
    """
    Convert a Bitcoin address to an Electrum scripthash.
    Electrum uses reversed SHA256 of the scriptPubKey.
    """
    script = _address_to_script(address)
    h = hashlib.sha256(script).digest()
    return h[::-1].hex()


def _address_to_script(address: str) -> bytes:
    """Convert a bech32 P2WSH address to scriptPubKey bytes."""
    # P2WSH: OP_0 <32-byte-hash>
    # bech32 decode
    try:
        from bitcoin.core.script import CScript
        from bitcoin.wallet import CBech32BitcoinAddress
        addr = CBech32BitcoinAddress(address)
        return bytes(addr.to_scriptPubKey())
    except ImportError:
        pass

    # Manual bech32 decode for P2WSH
    hrp, data = _bech32_decode(address)
    if data is None or data[0] != 0:
        raise ValueError(f"Cannot decode address: {address}")
    witness_program = _convertbits(data[1:], 5, 8, False)
    if len(witness_program) == 32:
        # P2WSH: OP_0 <32-byte-hash>
        return bytes([0x00, 0x20]) + bytes(witness_program)
    elif len(witness_program) == 20:
        # P2WPKH: OP_0 <20-byte-hash>
        return bytes([0x00, 0x14]) + bytes(witness_program)
    raise ValueError(f"Unknown witness program length: {len(witness_program)}")


# Minimal bech32 implementation
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _bech32_decode(bech: str):
    bech = bech.lower()
    pos = bech.rfind('1')
    if pos < 1 or pos + 7 > len(bech):
        return None, None
    hrp = bech[:pos]
    data = [CHARSET.find(x) for x in bech[pos+1:]]
    if any(d == -1 for d in data):
        return None, None
    return hrp, data[:-6]  # strip checksum

def _convertbits(data, frombits, tobits, pad=True):
    acc = 0; bits = 0; ret = []; maxv = (1 << tobits) - 1
    for value in data:
        acc = ((acc << frombits) | value) & 0xFFFFFF
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


# ---------------------------------------------------------------------------
# Wallet state (file-based cache)
# ---------------------------------------------------------------------------

def load_wallet_state() -> dict:
    if os.path.exists(WALLET_FILE):
        with open(WALLET_FILE, "r") as f:
            return json.load(f)
    return {
        "addresses": TREASURY_ADDRESSES,
        "utxos": [],
        "balance_sat": 0,
        "last_sync": None,
        "transactions": [],
    }

def save_wallet_state(state: dict):
    with open(WALLET_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Wallet operations
# ---------------------------------------------------------------------------

def sync_wallet(client: ElectrumClient) -> dict:
    """
    Sync wallet state: fetch UTXOs and balance for all known addresses.
    """
    state = load_wallet_state()
    all_utxos = []
    total_balance = 0

    for address in state["addresses"]:
        try:
            scripthash = _address_to_scripthash(address)
            balance = client.get_balance(scripthash)
            confirmed = balance.get("confirmed", 0)
            unconfirmed = balance.get("unconfirmed", 0)
            total_balance += confirmed + unconfirmed

            utxos = client.get_utxos(scripthash)
            for utxo in utxos:
                utxo["address"] = address
                utxo["scripthash"] = scripthash
                all_utxos.append(utxo)

            log.info(
                f"Address {address[:20]}... "
                f"confirmed={confirmed} unconfirmed={unconfirmed} "
                f"utxos={len(utxos)}"
            )
        except Exception as e:
            log.error(f"Failed to fetch {address}: {e}")

    state["utxos"] = all_utxos
    state["balance_sat"] = total_balance
    state["last_sync"] = datetime.datetime.utcnow().isoformat()
    save_wallet_state(state)

    log.info(f"Sync complete. Balance: {total_balance:,} sat ({total_balance/1e8:.8f} BTC). UTXOs: {len(all_utxos)}")
    return state


def get_balance() -> dict:
    """Return cached balance."""
    state = load_wallet_state()
    return {
        "balance_sat": state["balance_sat"],
        "balance_btc": state["balance_sat"] / 1e8,
        "utxo_count":  len(state["utxos"]),
        "last_sync":   state["last_sync"],
    }


def select_utxos(utxos: list, target_sat: int, fee_sat: int) -> tuple:
    """
    Simple UTXO selection — largest first.
    Returns (selected_utxos, change_sat) or raises if insufficient funds.
    """
    sorted_utxos = sorted(utxos, key=lambda u: u["value"], reverse=True)
    selected = []
    total = 0
    needed = target_sat + fee_sat

    for utxo in sorted_utxos:
        selected.append(utxo)
        total += utxo["value"]
        if total >= needed:
            break

    if total < needed:
        raise ValueError(
            f"Insufficient funds: need {needed:,} sat, have {total:,} sat "
            f"({total/1e8:.8f} BTC)"
        )

    change = total - needed
    return selected, change


# ---------------------------------------------------------------------------
# PSBT Construction (P2WSH)
# ---------------------------------------------------------------------------

PSBT_MAGIC             = b'psbt\xff'
PSBT_GLOBAL_UNSIGNED_TX = b'\x00'
PSBT_IN_WITNESS_UTXO   = b'\x01'
PSBT_IN_WITNESS_SCRIPT = b'\x05'
PSBT_OUT_WITNESS_SCRIPT = b'\x05'


def _encode_kv(key: bytes, value: bytes) -> bytes:
    return _write_varint(len(key)) + key + _write_varint(len(value)) + value


def _build_op_return_script(data: bytes = b'K_SCORCH') -> bytes:
    """Build an OP_RETURN output script."""
    # OP_RETURN = 0x6a
    if len(data) > 80:
        data = data[:80]
    return bytes([0x6a, len(data)]) + data


def _estimate_fee(n_inputs: int, n_outputs: int, fee_rate_sat_vb: int) -> int:
    """
    Estimate transaction fee for P2WSH inputs.
    P2WSH input weight: ~105 vbytes each
    P2WSH output: ~43 vbytes each
    Overhead: ~11 vbytes
    """
    vbytes = 11 + (n_inputs * 105) + (n_outputs * 43)
    return vbytes * fee_rate_sat_vb


def build_psbt(
    utxos: list,
    destination: str,
    amount_sat: int,
    change_address: str,
    witness_scripts: dict,  # address -> witness_script_bytes
    fee_rate_sat_vb: int = FEE_RATE_SAT_VB,
    op_return: bool = False,
) -> str:
    """
    Build an unsigned P2WSH PSBT.
    Returns base64-encoded PSBT.

    For OP_RETURN (burn) transactions:
    - destination is ignored
    - all funds go to OP_RETURN minus fee
    - op_return=True
    """
    # Estimate fee
    n_outputs = 1 if op_return else 2  # destination + change (or just OP_RETURN)
    fee_sat = _estimate_fee(len(utxos), n_outputs, fee_rate_sat_vb)

    total_input = sum(u["value"] for u in utxos)

    if op_return:
        # Burn: all funds to OP_RETURN, no change
        outputs = [(b'', 0, _build_op_return_script(b'K_SCORCH_BURN'))]
        # OP_RETURN carries 0 value — fee is implicitly total_input - 0
        # but we still need to account for it
    else:
        change_sat = total_input - amount_sat - fee_sat
        if change_sat < 0:
            raise ValueError(f"Insufficient funds after fee: need {amount_sat + fee_sat:,}, have {total_input:,}")
        dest_script = _address_to_script(destination)
        outputs = [(destination, amount_sat, dest_script)]
        if change_sat >= DUST_LIMIT_SAT:
            change_script = _address_to_script(change_address)
            outputs.append((change_address, change_sat, change_script))

    # --- Build unsigned transaction ---
    tx = b''
    tx += struct.pack('<i', 2)  # version 2

    # Inputs
    tx += _write_varint(len(utxos))
    for utxo in utxos:
        txid_bytes = bytes.fromhex(utxo["tx_hash"])[::-1]  # little-endian
        tx += txid_bytes
        tx += struct.pack('<I', utxo["tx_pos"])
        tx += b'\x00'  # empty scriptSig (segwit)
        tx += struct.pack('<I', 0xFFFFFFFE)  # sequence

    # Outputs
    tx += _write_varint(len(outputs))
    for _, value, script in outputs:
        tx += struct.pack('<q', value)
        tx += _write_varint(len(script))
        tx += script

    tx += struct.pack('<I', 0)  # locktime

    # --- Build PSBT ---
    psbt = PSBT_MAGIC

    # Global: unsigned tx
    psbt += _encode_kv(PSBT_GLOBAL_UNSIGNED_TX, tx)
    psbt += b'\x00'  # global map separator

    # Input maps
    for utxo in utxos:
        address = utxo.get("address", "")

        # PSBT_IN_WITNESS_UTXO: the output being spent
        # Format: value (8 bytes LE) + scriptPubKey
        utxo_script = _address_to_script(address)
        witness_utxo = struct.pack('<q', utxo["value"]) + _write_varint(len(utxo_script)) + utxo_script
        psbt += _encode_kv(PSBT_IN_WITNESS_UTXO, witness_utxo)

        # PSBT_IN_WITNESS_SCRIPT: the redeem script
        ws = witness_scripts.get(address)
        if ws:
            psbt += _encode_kv(PSBT_IN_WITNESS_SCRIPT, ws)

        psbt += b'\x00'  # input map separator

    # Output maps (empty — no extra data needed)
    for _ in outputs:
        psbt += b'\x00'

    b64 = base64.b64encode(psbt).decode()
    log.info(
        f"Built PSBT: {len(utxos)} inputs, {len(outputs)} outputs, "
        f"fee={fee_sat} sat, total_in={total_input:,} sat"
    )
    return b64


# ---------------------------------------------------------------------------
# Witness script derivation
# ---------------------------------------------------------------------------

def get_witness_script_for_address(address: str) -> Optional[bytes]:
    """
    Retrieve the witness script for a known treasury address.
    For production, derive this from the descriptor using a descriptor library.

    For now returns None — signer.py will need the witness script embedded
    in the PSBT for signing. This is populated when we have full descriptor
    parsing support.
    """
    # TODO: derive witness scripts from descriptor using miniscript library
    # For now, load from wallet state if previously cached
    state = load_wallet_state()
    scripts = state.get("witness_scripts", {})
    ws_hex = scripts.get(address)
    if ws_hex:
        return bytes.fromhex(ws_hex)
    return None


def cache_witness_script(address: str, witness_script: bytes):
    """Cache a witness script for an address."""
    state = load_wallet_state()
    if "witness_scripts" not in state:
        state["witness_scripts"] = {}
    state["witness_scripts"][address] = witness_script.hex()
    save_wallet_state(state)


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def broadcast_transaction(tx_hex: str) -> str:
    """
    Broadcast a raw transaction. Tries Electrum first, then REST APIs.
    Returns txid.
    """
    # Try Electrum
    try:
        client = ElectrumClient(ELECTRUM_SERVER, ELECTRUM_PORT)
        client.connect()
        txid = client.broadcast(tx_hex)
        client.disconnect()
        log.info(f"Broadcast via Electrum. txid: {txid}")
        return txid
    except Exception as e:
        log.warning(f"Electrum broadcast failed: {e} — trying REST APIs")

    # Fallback to REST APIs
    apis = [
        "https://mempool.space/api/tx",
        "https://blockstream.info/api/tx",
    ]
    for url in apis:
        try:
            resp = requests.post(
                url,
                data=tx_hex.encode(),
                headers={"Content-Type": "text/plain"},
                timeout=30,
            )
            if resp.ok:
                txid = resp.text.strip()
                log.info(f"Broadcast via {url}. txid: {txid}")
                return txid
            else:
                log.warning(f"HTTP {resp.status_code} from {url}: {resp.text}")
        except Exception as e:
            log.warning(f"Failed {url}: {e}")

    raise RuntimeError("All broadcast methods failed.")


# ---------------------------------------------------------------------------
# Watch daemon
# ---------------------------------------------------------------------------

def watch_for_deposits(poll_interval: int = 60):
    """
    Daemon that polls for new incoming funds and logs them.
    Also notifies coordinator of balance updates.
    """
    log.info(f"Watching treasury. Polling every {poll_interval}s.")
    last_balance = 0

    while True:
        try:
            client = ElectrumClient(ELECTRUM_SERVER, ELECTRUM_PORT)
            client.connect()
            state = sync_wallet(client)
            client.disconnect()

            current_balance = state["balance_sat"]

            if current_balance != last_balance:
                diff = current_balance - last_balance
                direction = "+" if diff > 0 else ""
                log.info(
                    f"Balance changed: {direction}{diff:,} sat. "
                    f"New balance: {current_balance:,} sat ({current_balance/1e8:.8f} BTC)"
                )
                last_balance = current_balance

                # Notify coordinator of new balance
                try:
                    requests.post(
                        f"{COORDINATOR_URL}/balance",
                        json={"balance_sat": current_balance},
                        timeout=5,
                    )
                except Exception:
                    pass  # coordinator balance endpoint optional

        except Exception as e:
            log.error(f"Watch error: {e}")

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_balance(args):
    state = load_wallet_state()
    bal = state["balance_sat"]
    print(f"\n  Balance:    {bal:,} sat")
    print(f"             {bal/1e8:.8f} BTC")
    print(f"  UTXOs:      {len(state['utxos'])}")
    print(f"  Last sync:  {state.get('last_sync', 'never')}\n")


def cmd_addresses(args):
    state = load_wallet_state()
    print(f"\nKnown treasury addresses ({len(state['addresses'])}):")
    for addr in state["addresses"]:
        print(f"  {addr}")
    print()


def cmd_utxos(args):
    state = load_wallet_state()
    utxos = state["utxos"]
    if not utxos:
        print("No UTXOs found. Run: python wallet.py sync")
        return
    print(f"\nUTXOs ({len(utxos)}):")
    for u in utxos:
        print(f"  {u['tx_hash'][:16]}...:{u['tx_pos']}  {u['value']:>12,} sat  ({u.get('address','')[:20]}...)")
    print(f"\nTotal: {sum(u['value'] for u in utxos):,} sat\n")


def cmd_sync(args):
    print("Syncing wallet...")
    client = ElectrumClient(ELECTRUM_SERVER, ELECTRUM_PORT)
    client.connect()
    state = sync_wallet(client)
    client.disconnect()
    print(f"\n✓ Synced. Balance: {state['balance_sat']:,} sat. UTXOs: {len(state['utxos'])}")


def cmd_build(args):
    """Build a PSBT for a coordinator proposal."""
    # Fetch proposal from coordinator
    resp = requests.get(f"{COORDINATOR_URL}/proposals/{args.proposal_id}", timeout=10)
    resp.raise_for_status()
    proposal = resp.json()

    state = load_wallet_state()
    if not state["utxos"]:
        print("No UTXOs. Run: python wallet.py sync")
        sys.exit(1)

    destination = proposal["destination"]
    amount_sat  = proposal["amount_sat"]

    # Estimate fee
    fee_sat = _estimate_fee(len(state["utxos"]), 2, FEE_RATE_SAT_VB)

    # Select UTXOs
    selected, change_sat = select_utxos(state["utxos"], amount_sat, fee_sat)

    # Get witness scripts
    witness_scripts = {}
    for utxo in selected:
        addr = utxo.get("address", "")
        ws = get_witness_script_for_address(addr)
        if ws:
            witness_scripts[addr] = ws

    # Use first treasury address as change address
    change_address = state["addresses"][0]

    # Build PSBT
    psbt_b64 = build_psbt(
        utxos=selected,
        destination=destination,
        amount_sat=amount_sat,
        change_address=change_address,
        witness_scripts=witness_scripts,
    )

    # Save to file
    out_file = f"proposal_{args.proposal_id[:8]}.psbt"
    with open(out_file, "w") as f:
        f.write(psbt_b64)

    print(f"\n✓ PSBT built and saved to {out_file}")
    print(f"  Inputs:      {len(selected)}")
    print(f"  Amount:      {amount_sat:,} sat")
    print(f"  Fee:         {fee_sat:,} sat")
    print(f"  Change:      {change_sat:,} sat")
    print(f"\nNext: distribute {out_file} to agent signers.")


def cmd_broadcast(args):
    """Broadcast a finalised raw transaction."""
    if args.txhex_file:
        with open(args.txhex_file, "r") as f:
            tx_hex = f.read().strip()
    else:
        print("Paste raw tx hex then Ctrl-D:")
        tx_hex = sys.stdin.read().strip()

    if not tx_hex:
        print("No transaction provided.")
        sys.exit(1)

    confirm = input(f"Broadcast transaction? ({len(tx_hex)//2} bytes) [yes/no]: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    txid = broadcast_transaction(tx_hex)
    print(f"\n✓ Broadcast. txid: {txid}")
    print(f"  View: https://mempool.space/tx/{txid}")


def cmd_watch(args):
    watch_for_deposits(poll_interval=args.interval)


def cmd_add_address(args):
    """Add a new treasury address to watch."""
    state = load_wallet_state()
    if args.address not in state["addresses"]:
        state["addresses"].append(args.address)
        save_wallet_state(state)
        print(f"✓ Added address: {args.address}")
    else:
        print("Address already in wallet.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Autonomous AI Treasury — Wallet")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("balance",   help="Show current balance")
    sub.add_parser("addresses", help="List known treasury addresses")
    sub.add_parser("utxos",     help="List UTXOs")
    sub.add_parser("sync",      help="Sync wallet state from Electrum")

    p = sub.add_parser("build", help="Build PSBT for a coordinator proposal")
    p.add_argument("proposal_id", help="Proposal UUID from coordinator")

    p = sub.add_parser("broadcast", help="Broadcast a raw transaction")
    p.add_argument("txhex_file", nargs="?", help="File containing raw tx hex")

    p = sub.add_parser("watch", help="Watch for incoming funds (daemon)")
    p.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")

    p = sub.add_parser("add-address", help="Add a treasury address to watch")
    p.add_argument("address")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "balance":     cmd_balance,
        "addresses":   cmd_addresses,
        "utxos":       cmd_utxos,
        "sync":        cmd_sync,
        "build":       cmd_build,
        "broadcast":   cmd_broadcast,
        "watch":       cmd_watch,
        "add-address": cmd_add_address,
    }[args.command](args)


if __name__ == "__main__":
    main()
