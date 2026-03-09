"""
Wallet and mempool helpers for autonomous AIUNION payments.

This module uses bdkpython for PSBT creation/finalization/signing compatibility
and uses mempool.space's Esplora API for chain data and broadcasting.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import bdkpython as bdk


DEFAULT_MEMPOOL_API = "https://mempool.space/api"


class PaymentError(Exception):
    """Raised when automated payment creation/signing/broadcast fails."""


def _network_from_string(network_name: str) -> bdk.Network:
    value = (network_name or "bitcoin").strip().lower()
    mapping = {
        "bitcoin": bdk.Network.BITCOIN,
        "mainnet": bdk.Network.BITCOIN,
        "testnet": bdk.Network.TESTNET,
        "testnet4": bdk.Network.TESTNET4,
        "signet": bdk.Network.SIGNET,
        "regtest": bdk.Network.REGTEST,
    }
    if value not in mapping:
        raise PaymentError(f"Unsupported BITCOIN_NETWORK '{network_name}'")
    return mapping[value]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_policy_path(raw: Any) -> Optional[Dict[str, List[int]]]:
    if raw in (None, "", {}):
        return None
    if isinstance(raw, dict):
        return {str(k): [int(i) for i in v] for k, v in raw.items()}
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise PaymentError("PAYMENT_POLICY_PATH JSON must decode to an object")
        return {str(k): [int(i) for i in v] for k, v in loaded.items()}
    raise PaymentError("PAYMENT_POLICY_PATH must be dict, JSON string, or null")


def _http_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def mempool_address_balance_btc(address: str, api_base: str = DEFAULT_MEMPOOL_API) -> float:
    """Read confirmed + mempool net balance from mempool Esplora API."""
    base = api_base.rstrip("/")
    data = _http_json(f"{base}/address/{address}")

    chain = data.get("chain_stats", {}) or {}
    mempool = data.get("mempool_stats", {}) or {}

    funded = int(chain.get("funded_txo_sum", 0)) + int(mempool.get("funded_txo_sum", 0))
    spent = int(chain.get("spent_txo_sum", 0)) + int(mempool.get("spent_txo_sum", 0))
    sats = max(0, funded - spent)
    return sats / 100_000_000


def mempool_address_transactions(
    address: str,
    count: int = 10,
    api_base: str = DEFAULT_MEMPOOL_API,
) -> List[Dict[str, Any]]:
    """Return simplified transaction history for dashboard consumption."""
    base = api_base.rstrip("/")
    txs = _http_json(f"{base}/address/{address}/txs")
    if not isinstance(txs, list):
        return []

    try:
        tip_height_raw = urllib.request.urlopen(
            urllib.request.Request(f"{base}/blocks/tip/height", method="GET"),
            timeout=10,
        ).read()
        tip_height = int(tip_height_raw.decode("utf-8").strip())
    except Exception:
        tip_height = None

    out: List[Dict[str, Any]] = []
    for tx in txs[: max(0, int(count))]:
        vouts = tx.get("vout", []) or []
        vins = tx.get("vin", []) or []

        received = 0
        for vout in vouts:
            if vout.get("scriptpubkey_address") == address:
                received += int(vout.get("value", 0))

        spent = 0
        for vin in vins:
            prevout = vin.get("prevout", {}) or {}
            if prevout.get("scriptpubkey_address") == address:
                spent += int(prevout.get("value", 0))

        net_sats = received - spent
        status = tx.get("status", {}) or {}
        confirmed = bool(status.get("confirmed", False))
        block_height = status.get("block_height")
        confirmations = 0
        if confirmed and tip_height is not None and isinstance(block_height, int):
            confirmations = max(0, tip_height - block_height + 1)

        out.append(
            {
                "txid": tx.get("txid"),
                "category": "receive" if net_sats >= 0 else "send",
                "amount": round(net_sats / 100_000_000, 8),
                "confirmations": confirmations,
                "blockheight": block_height,
                "blocktime": status.get("block_time"),
            }
        )

    return out


@dataclass
class PaymentBuildResult:
    psbt: str
    fee_rate_sat_vb: float


class TreasuryWallet:
    """BDK-backed watch/signing wallet for payment PSBT generation and broadcast."""

    def __init__(
        self,
        descriptor: str,
        network: bdk.Network,
        *,
        change_descriptor: Optional[str] = None,
        esplora_url: str = DEFAULT_MEMPOOL_API,
        fee_target_blocks: int = 3,
        min_fee_rate_sat_vb: float = 1.0,
        fee_rate_override_sat_vb: Optional[float] = None,
        policy_path: Optional[Dict[str, List[int]]] = None,
        stop_gap: int = 50,
        parallel_requests: int = 4,
    ):
        self.network = network
        self.esplora_url = esplora_url.rstrip("/")
        self.fee_target_blocks = max(1, int(fee_target_blocks))
        self.min_fee_rate_sat_vb = max(1.0, float(min_fee_rate_sat_vb))
        self.fee_rate_override_sat_vb = (
            float(fee_rate_override_sat_vb)
            if fee_rate_override_sat_vb is not None
            else None
        )
        self.policy_path = policy_path
        self.stop_gap = max(10, int(stop_gap))
        self.parallel_requests = max(1, int(parallel_requests))

        self._descriptor = bdk.Descriptor(descriptor, network)
        self._change_descriptor = (
            bdk.Descriptor(change_descriptor, network) if change_descriptor else None
        )
        self._persister = bdk.Persister.new_in_memory()
        self._wallet = self._build_wallet()
        self._esplora = bdk.EsploraClient(self.esplora_url)

    @classmethod
    def from_config(cls, config_module: Any) -> "TreasuryWallet":
        descriptor = (
            getattr(config_module, "TREASURY_DESCRIPTOR_PUBLIC", None)
            or getattr(config_module, "TREASURY_DESCRIPTOR", None)
            or ""
        )
        if not descriptor:
            raise PaymentError(
                "Missing TREASURY_DESCRIPTOR_PUBLIC (or TREASURY_DESCRIPTOR) in config.py"
            )

        network = _network_from_string(getattr(config_module, "BITCOIN_NETWORK", "bitcoin"))
        change_descriptor = getattr(config_module, "TREASURY_CHANGE_DESCRIPTOR", None)
        esplora_url = getattr(config_module, "MEMPOOL_API_BASE", DEFAULT_MEMPOOL_API)
        policy_path = _parse_policy_path(getattr(config_module, "PAYMENT_POLICY_PATH", None))

        return cls(
            descriptor=descriptor,
            change_descriptor=change_descriptor,
            network=network,
            esplora_url=esplora_url,
            fee_target_blocks=int(getattr(config_module, "PAYMENT_FEE_TARGET_BLOCKS", 3)),
            min_fee_rate_sat_vb=float(getattr(config_module, "PAYMENT_MIN_FEE_RATE_SAT_VB", 1.0)),
            fee_rate_override_sat_vb=(
                float(getattr(config_module, "PAYMENT_FEE_RATE_SAT_VB"))
                if getattr(config_module, "PAYMENT_FEE_RATE_SAT_VB", None) is not None
                else None
            ),
            policy_path=policy_path,
            stop_gap=int(getattr(config_module, "PAYMENT_STOP_GAP", 50)),
            parallel_requests=int(getattr(config_module, "PAYMENT_PARALLEL_REQUESTS", 4)),
        )

    @property
    def wallet(self) -> bdk.Wallet:
        return self._wallet

    def _build_wallet(self) -> bdk.Wallet:
        if self._descriptor.is_multipath():
            return bdk.Wallet.create_from_two_path_descriptor(
                self._descriptor, self.network, self._persister
            )
        if self._change_descriptor is not None:
            return bdk.Wallet(
                self._descriptor,
                self._change_descriptor,
                self.network,
                self._persister,
            )
        return bdk.Wallet.create_single(self._descriptor, self.network, self._persister)

    def sync(self) -> None:
        request = self._wallet.start_full_scan().build()
        update = self._esplora.full_scan(
            request,
            self.stop_gap,
            self.parallel_requests,
        )
        self._wallet.apply_update(update)

    def _resolved_fee_rate_sat_vb(self) -> float:
        if self.fee_rate_override_sat_vb is not None and self.fee_rate_override_sat_vb > 0:
            return max(self.min_fee_rate_sat_vb, self.fee_rate_override_sat_vb)

        estimates = self._esplora.get_fee_estimates()
        if not isinstance(estimates, dict) or not estimates:
            return self.min_fee_rate_sat_vb

        normalized: Dict[int, float] = {}
        for key, value in estimates.items():
            try:
                normalized[int(key)] = _as_float(value, 0.0)
            except Exception:
                continue

        if self.fee_target_blocks in normalized:
            return max(self.min_fee_rate_sat_vb, normalized[self.fee_target_blocks])

        larger = [k for k in normalized if k >= self.fee_target_blocks]
        if larger:
            best = min(larger)
            return max(self.min_fee_rate_sat_vb, normalized[best])

        best_any = min(normalized.keys())
        return max(self.min_fee_rate_sat_vb, normalized[best_any])

    def create_payment_psbt(
        self,
        recipient_address: str,
        amount_btc: float,
    ) -> PaymentBuildResult:
        amount_btc = _as_float(amount_btc, 0.0)
        if amount_btc <= 0:
            raise PaymentError(f"Invalid payout amount_btc={amount_btc}")

        recipient = bdk.Address(recipient_address, self.network)
        if not recipient.is_valid_for_network(self.network):
            raise PaymentError(f"Invalid recipient address for configured network: {recipient_address}")

        self.sync()

        amount_sats = max(1, int(round(amount_btc * 100_000_000)))

        builder = bdk.TxBuilder()
        builder = builder.add_recipient(
            recipient.script_pubkey(),
            bdk.Amount.from_sat(amount_sats),
        )
        fee_rate_sat_vb = self._resolved_fee_rate_sat_vb()
        fee_rate_sat_vb_int = max(1, int(round(fee_rate_sat_vb)))
        builder = builder.fee_rate(bdk.FeeRate.from_sat_per_vb(fee_rate_sat_vb_int))

        # Force the expected Miniscript branch for routine treasury payments when configured.
        if self.policy_path:
            builder = builder.policy_path(self.policy_path, bdk.KeychainKind.EXTERNAL)

        try:
            psbt = builder.finish(self._wallet)
        except Exception as exc:
            raise PaymentError(f"Failed to build payment PSBT: {exc}") from exc

        return PaymentBuildResult(
            psbt=psbt.serialize(),
            fee_rate_sat_vb=fee_rate_sat_vb,
        )

    def finalize_psbt(self, psbt_base64: str) -> Tuple[str, str, bdk.Transaction]:
        psbt = bdk.Psbt(psbt_base64)
        finalized = self._wallet.finalize_psbt(psbt)
        if not finalized:
            raise PaymentError("Unable to finalize PSBT after signer collection")

        tx = psbt.extract_tx()
        txid = str(tx.compute_txid())
        return txid, psbt.serialize(), tx

    def broadcast_transaction(self, tx: bdk.Transaction) -> str:
        txid = str(tx.compute_txid())
        try:
            self._esplora.broadcast(tx)
        except Exception as exc:
            raise PaymentError(f"Broadcast failed for {txid}: {exc}") from exc
        return txid

