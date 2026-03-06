"""
Encrypted agent signer management for AIUNION autonomous PSBT flow.

Each agent signer file is encrypted at rest (AES-GCM + PBKDF2) and contains
descriptor material used to produce a valid script-path signature.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bdkpython as bdk
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DEFAULT_PASSPHRASE_ENV = "AIUNION_SIGNER_PASSPHRASE"
DEFAULT_ITERATIONS = 210_000
FORBIDDEN_SIGNER_IDS = {"admin", "k_admin", "scorch", "k_scorch"}


class SignerError(Exception):
    """Raised when signer key access or signing fails."""


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
        raise SignerError(f"Unsupported BITCOIN_NETWORK '{network_name}'")
    return mapping[value]


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_signer_material(
    plaintext: str,
    passphrase: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, Any]:
    """One-time helper for provisioning encrypted signer payload files."""
    if not plaintext:
        raise SignerError("Cannot encrypt empty signer material")
    if not passphrase:
        raise SignerError("Passphrase required for signer material encryption")

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        "version": 1,
        "kdf": "pbkdf2_sha256",
        "iterations": int(iterations),
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_signer_material(payload: Dict[str, Any], passphrase: str) -> str:
    if not passphrase:
        raise SignerError("Signer passphrase is missing")
    try:
        salt = base64.b64decode(payload["salt_b64"])
        nonce = base64.b64decode(payload["nonce_b64"])
        ciphertext = base64.b64decode(payload["ciphertext_b64"])
        iterations = int(payload.get("iterations", DEFAULT_ITERATIONS))
    except Exception as exc:
        raise SignerError(f"Malformed encrypted signer payload: {exc}") from exc

    key = _derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise SignerError("Failed to decrypt signer payload (wrong passphrase or corrupted file)") from exc
    return plaintext.decode("utf-8")


@dataclass
class SignAttempt:
    agent_id: str
    signed: bool


class AgentPsbtSigner:
    """Loads encrypted agent signer descriptors and signs PSBTs in sequence."""

    def __init__(
        self,
        *,
        signer_files: Dict[str, str],
        network: bdk.Network,
        passphrase: Optional[str] = None,
        passphrase_env: str = DEFAULT_PASSPHRASE_ENV,
        signer_order: Optional[List[str]] = None,
        descriptor_template: Optional[str] = None,
        default_change_descriptor: Optional[str] = None,
        signer_base_dir: Optional[str] = None,
    ):
        if not signer_files:
            raise SignerError("No AGENT_SIGNER_FILES configured")
        self.signer_files = {str(k): str(v) for k, v in signer_files.items()}
        self.network = network
        self.passphrase = passphrase
        self.passphrase_env = passphrase_env or DEFAULT_PASSPHRASE_ENV
        self.signer_order = signer_order or list(self.signer_files.keys())
        self.descriptor_template = descriptor_template
        self.default_change_descriptor = default_change_descriptor
        self.signer_base_dir = Path(signer_base_dir or ".")

    @classmethod
    def from_config(cls, config_module: Any) -> "AgentPsbtSigner":
        signer_files = getattr(config_module, "AGENT_SIGNER_FILES", None) or {}
        network = _network_from_string(getattr(config_module, "BITCOIN_NETWORK", "bitcoin"))
        passphrase = getattr(config_module, "SIGNER_PASSPHRASE", None)
        passphrase_env = getattr(config_module, "SIGNER_PASSPHRASE_ENV", DEFAULT_PASSPHRASE_ENV)
        signer_order = getattr(config_module, "PAYMENT_SIGNER_ORDER", None)
        descriptor_template = getattr(config_module, "AGENT_SIGNER_DESCRIPTOR_TEMPLATE", None)
        default_change_descriptor = getattr(config_module, "AGENT_SIGNER_CHANGE_DESCRIPTOR", None)
        signer_base_dir = getattr(config_module, "AGENT_SIGNER_BASE_DIR", ".")
        return cls(
            signer_files=signer_files,
            network=network,
            passphrase=passphrase,
            passphrase_env=passphrase_env,
            signer_order=signer_order,
            descriptor_template=descriptor_template,
            default_change_descriptor=default_change_descriptor,
            signer_base_dir=signer_base_dir,
        )

    def _resolved_passphrase(self) -> str:
        if self.passphrase:
            return self.passphrase
        value = os.getenv(self.passphrase_env, "").strip()
        if value:
            return value
        raise SignerError(
            f"Signer passphrase missing. Set {self.passphrase_env} or SIGNER_PASSPHRASE in config.py"
        )

    def _read_encrypted_payload(self, agent_id: str) -> Dict[str, Any]:
        if agent_id not in self.signer_files:
            raise SignerError(f"No encrypted signer file configured for '{agent_id}'")
        path = Path(self.signer_files[agent_id])
        if not path.is_absolute():
            path = self.signer_base_dir / path
        if not path.exists():
            raise SignerError(f"Encrypted signer file not found for '{agent_id}': {path}")
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise SignerError(f"Encrypted signer payload must be a JSON object: {path}")
        return payload

    def _material_to_descriptors(self, plaintext: str) -> Tuple[str, Optional[str]]:
        text = plaintext.strip()
        if not text:
            raise SignerError("Signer payload decrypts to empty content")

        if text.startswith("{"):
            data = json.loads(text)
            descriptor = data.get("descriptor")
            if descriptor:
                return str(descriptor), data.get("change_descriptor")

            secret_value = (
                data.get("descriptor_secret_key")
                or data.get("secret_key")
                or data.get("private_key")
            )
            if secret_value and self.descriptor_template:
                descriptor = self.descriptor_template.replace("{secret_key}", str(secret_value))
                descriptor = descriptor.replace("{key}", str(secret_value))
                return descriptor, data.get("change_descriptor")

            raise SignerError(
                "Signer JSON payload must contain 'descriptor', or a secret key with AGENT_SIGNER_DESCRIPTOR_TEMPLATE"
            )

        if self.descriptor_template and ("xprv" not in text and "tr(" not in text):
            descriptor = self.descriptor_template.replace("{secret_key}", text).replace("{key}", text)
            return descriptor, self.default_change_descriptor

        return text, self.default_change_descriptor

    def _wallet_for_agent(self, agent_id: str) -> bdk.Wallet:
        payload = self._read_encrypted_payload(agent_id)
        plaintext = decrypt_signer_material(payload, self._resolved_passphrase())
        descriptor_str, change_descriptor_str = self._material_to_descriptors(plaintext)

        descriptor = bdk.Descriptor(descriptor_str, self.network)
        change_descriptor = (
            bdk.Descriptor(change_descriptor_str, self.network)
            if change_descriptor_str
            else None
        )
        persister = bdk.Persister.new_in_memory()

        if descriptor.is_multipath():
            return bdk.Wallet.create_from_two_path_descriptor(descriptor, self.network, persister)
        if change_descriptor is not None:
            return bdk.Wallet(descriptor, change_descriptor, self.network, persister)
        return bdk.Wallet.create_single(descriptor, self.network, persister)

    def sign_psbt(self, psbt_base64: str, agent_id: str) -> Tuple[SignAttempt, str]:
        signer_id = str(agent_id)
        if signer_id.lower() in FORBIDDEN_SIGNER_IDS:
            raise SignerError(f"Forbidden signer id: {signer_id}")
        if signer_id not in self.signer_files:
            raise SignerError(f"Unknown signer id '{signer_id}'")

        wallet = self._wallet_for_agent(signer_id)
        psbt = bdk.Psbt(psbt_base64)
        try:
            signed = wallet.sign(psbt)
        except Exception as exc:
            raise SignerError(f"Signer '{signer_id}' failed to sign PSBT: {exc}") from exc

        return SignAttempt(
            agent_id=signer_id,
            signed=bool(signed),
        ), psbt.serialize()

    def select_signers(self, votes: Dict[str, Dict[str, Any]], minimum: int = 3) -> List[str]:
        minimum = max(1, int(minimum))
        available = [s for s in self.signer_order if s in self.signer_files]
        if len(available) < minimum:
            raise SignerError(f"Need at least {minimum} agent signer files; configured {len(available)}")

        yes_voters = []
        for signer_id in available:
            vote_obj = (votes or {}).get(signer_id, {}) or {}
            if str(vote_obj.get("vote", "")).upper() == "YES":
                yes_voters.append(signer_id)

        chosen: List[str] = []
        for signer_id in yes_voters:
            if signer_id not in chosen:
                chosen.append(signer_id)
            if len(chosen) >= minimum:
                return chosen

        for signer_id in available:
            if signer_id not in chosen:
                chosen.append(signer_id)
            if len(chosen) >= minimum:
                break
        return chosen

