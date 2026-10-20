"""
Core data models using Pydantic for validation and serialization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import cached_property
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from jmcore.bitcoin import calculate_relative_fee
from jmcore.constants import MAX_MONEY


class MessageParsingError(Exception):
    """Exception raised when message parsing fails due to security limits."""

    pass


def validate_json_nesting_depth(obj: Any, max_depth: int = 10, current_depth: int = 0) -> None:
    """
    Validate that a JSON object does not exceed maximum nesting depth.

    Args:
        obj: The object to validate (dict, list, or primitive)
        max_depth: Maximum allowed nesting depth
        current_depth: Current depth in recursion

    Raises:
        MessageParsingError: If nesting depth exceeds max_depth
    """
    if current_depth > max_depth:
        raise MessageParsingError(f"JSON nesting depth exceeds maximum of {max_depth}")

    if isinstance(obj, dict):
        for value in obj.values():
            validate_json_nesting_depth(value, max_depth, current_depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            validate_json_nesting_depth(item, max_depth, current_depth + 1)


# Default directory servers for each network.
# XBT build: there are deliberately NO default directories. The upstream
# defaults are the real-Bitcoin (SHA-256) JoinMarket directories; an XBT
# wallet must never join that market. Users must set
# network_config.directory_servers to XBT directories explicitly.
DIRECTORY_NODES_MAINNET: list[str] = []

# Signet default directory nodes (upstream signet; none for the XBT build)
DIRECTORY_NODES_SIGNET: list[str] = []

NO_DIRECTORY_SERVERS_ERROR = (
    "No directory servers configured. This XBT build has no default directories: "
    "set directory_servers to XBT directories (network_config.directory_servers in "
    "config.toml, or --directory)."
)


def require_directory_servers(servers: list[str]) -> list[str]:
    """Refuse to start without explicitly configured (XBT) directory servers."""
    if not servers:
        raise ValueError(NO_DIRECTORY_SERVERS_ERROR)
    return servers


# No default directory nodes for testnet/regtest - must be configured by user
DIRECTORY_NODES_TESTNET: list[str] = []


def get_default_directory_nodes(network: NetworkType) -> list[str]:
    """Get default directory nodes for a given network."""
    if network == NetworkType.MAINNET:
        return DIRECTORY_NODES_MAINNET.copy()
    elif network == NetworkType.SIGNET:
        return DIRECTORY_NODES_SIGNET.copy()
    elif network == NetworkType.TESTNET:
        return DIRECTORY_NODES_TESTNET.copy()
    # Regtest has no default directory nodes - must be configured
    return []


class PeerStatus(StrEnum):
    UNCONNECTED = "unconnected"
    CONNECTED = "connected"
    HANDSHAKED = "handshaked"
    DISCONNECTED = "disconnected"


class NetworkType(StrEnum):
    MAINNET = "mainnet"
    TESTNET = "testnet"
    SIGNET = "signet"
    REGTEST = "regtest"


# XBT build: network identity on the wire.
#
# The handshake "network" field is the only thing that keeps JoinMarket
# markets apart. Internally this build keeps NetworkType.MAINNET (mainnet
# addresses: bc1, m/84'/0') because XBT shares Bitcoin's address formats, but
# on the wire it announces and requires "xbt" (and "xbt-<net>" for test
# networks). Upstream peers/directories send "mainnet" and reject "xbt", so
# neither side can join the other's market by accident.
XBT_WIRE_NETWORKS: dict[NetworkType, str] = {
    NetworkType.MAINNET: "xbt",
    NetworkType.TESTNET: "xbt-testnet",
    NetworkType.SIGNET: "xbt-signet",
    NetworkType.REGTEST: "xbt-regtest",
}
_XBT_WIRE_TO_NETWORK = {wire: net for net, wire in XBT_WIRE_NETWORKS.items()}


def wire_network(network: NetworkType | str) -> str:
    """The network string this build sends in handshakes for ``network``."""
    return XBT_WIRE_NETWORKS[NetworkType(network)]


def parse_wire_network(value: object) -> NetworkType:
    """Parse a peer's handshake network; only XBT identifiers are accepted.

    Raises:
        ValueError: for anything else, including upstream "mainnet".
    """
    if isinstance(value, str) and value.lower() in _XBT_WIRE_TO_NETWORK:
        return _XBT_WIRE_TO_NETWORK[value.lower()]
    raise ValueError(f"Not an XBT network identifier: {value!r}")


class PeerInfo(BaseModel):
    nick: str = Field(..., min_length=1, max_length=64)
    onion_address: str = Field(..., pattern=r"^[a-z2-7]{56}\.onion$|^NOT-SERVING-ONION$")
    port: int = Field(..., ge=-1, le=65535)
    status: PeerStatus = PeerStatus.UNCONNECTED
    is_directory: bool = False
    network: NetworkType = NetworkType.MAINNET
    last_seen: datetime | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    protocol_version: int = Field(default=5, ge=5, le=10)  # Negotiated protocol version
    neutrino_compat: bool = False  # True if peer supports extended UTXO metadata

    @field_validator("onion_address")
    @classmethod
    def validate_onion(cls, v: str) -> str:
        if v == "NOT-SERVING-ONION":
            return v
        if not v.endswith(".onion"):
            raise ValueError("Invalid onion address")
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int, info) -> int:
        if v == -1 and info.data.get("onion_address") == "NOT-SERVING-ONION":
            return v
        if v < 1 or v > 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v

    @cached_property
    def location_string(self) -> str:
        if self.onion_address == "NOT-SERVING-ONION":
            return "NOT-SERVING-ONION"
        return f"{self.onion_address}:{self.port}"

    def supports_extended_utxo(self) -> bool:
        """Check if this peer supports extended UTXO format (neutrino_compat)."""
        # With feature-based detection, we check the neutrino_compat flag
        # which is set from the features dict during handshake
        return self.neutrino_compat

    model_config = {"frozen": False}


class MessageEnvelope(BaseModel):
    message_type: int = Field(..., ge=0)
    payload: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_bytes(self) -> bytes:
        import json

        result = json.dumps({"type": self.message_type, "line": self.payload}).encode("utf-8")
        return result

    @classmethod
    def from_bytes(
        cls, data: bytes, max_line_length: int = 65536, max_json_nesting_depth: int = 10
    ) -> MessageEnvelope:
        """
        Parse a message envelope from bytes with security limits.

        Args:
            data: Raw message bytes (without \\r\\n terminator)
            max_line_length: Maximum allowed line length in bytes (default 64KB)
            max_json_nesting_depth: Maximum JSON nesting depth (default 10)

        Returns:
            Parsed MessageEnvelope

        Raises:
            MessageParsingError: If message exceeds security limits
            json.JSONDecodeError: If JSON is malformed
        """
        import json

        # Check line length BEFORE parsing to prevent DoS
        if len(data) > max_line_length:
            raise MessageParsingError(
                f"Message line length {len(data)} exceeds maximum of {max_line_length} bytes"
            )

        try:
            # Parse and validate nesting depth before creating the model.
            obj = json.loads(data)
            validate_json_nesting_depth(obj, max_json_nesting_depth)
        except RecursionError as exc:
            raise MessageParsingError("JSON nesting depth exceeds parser limits") from exc

        return cls(message_type=obj["type"], payload=obj["line"])


class HandshakeRequest(BaseModel):
    app_name: str = "JoinMarket"
    directory: bool = False
    location_string: str
    proto_ver: int
    features: dict[str, Any] = Field(default_factory=dict)
    nick: str = Field(..., min_length=1)
    network: NetworkType


class HandshakeResponse(BaseModel):
    app_name: str = "JoinMarket"
    directory: bool = True
    proto_ver_min: int
    proto_ver_max: int
    features: dict[str, Any] = Field(default_factory=dict)
    accepted: bool
    nick: str = Field(..., min_length=1)
    network: NetworkType
    motd: str = "JoinMarket Directory Server"


class OfferType(StrEnum):
    SW0_ABSOLUTE = "sw0absoffer"
    SW0_RELATIVE = "sw0reloffer"
    SWA_ABSOLUTE = "swabsoffer"
    SWA_RELATIVE = "swreloffer"


MAX_RELATIVE_FEE_INPUT_LENGTH = 128
MAX_RELATIVE_FEE_PRECISION = 18
MAX_RELATIVE_FEE_EXPONENT = 64


def normalize_relative_fee(value: str | int | float | Decimal, field_name: str) -> str:
    """Validate and normalize a relative fee without unbounded fixed-point formatting."""
    text = str(value)
    if len(text) > MAX_RELATIVE_FEE_INPUT_LENGTH:
        raise ValueError(
            f"{field_name} input exceeds maximum length of {MAX_RELATIVE_FEE_INPUT_LENGTH}"
        )

    try:
        fee = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a valid decimal: {text!r}") from exc

    if not fee.is_finite():
        raise ValueError(f"{field_name} must be finite")

    decimal_tuple = fee.as_tuple()
    if len(decimal_tuple.digits) > MAX_RELATIVE_FEE_PRECISION:
        raise ValueError(
            f"{field_name} has too many significant digits (maximum {MAX_RELATIVE_FEE_PRECISION})"
        )

    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):  # Defensive, non-finite values were rejected above.
        raise ValueError(f"{field_name} must be finite")
    if not -MAX_RELATIVE_FEE_EXPONENT <= exponent <= MAX_RELATIVE_FEE_EXPONENT:
        raise ValueError(
            f"{field_name} exponent must be between "
            f"{-MAX_RELATIVE_FEE_EXPONENT} and {MAX_RELATIVE_FEE_EXPONENT}"
        )

    if fee < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if fee >= 1:
        raise ValueError(f"{field_name} must be less than 1")
    return format(fee, "f")


def is_absolute_offer_type(offer_type: OfferType) -> bool:
    """Check if an offer type uses absolute fees."""
    return offer_type in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE)


def calculate_cj_fee(offer_type: OfferType, cjfee: str | int, amount: int) -> int:
    """
    Calculate actual CoinJoin fee based on offer type.

    This is the canonical fee calculation used by both makers and takers.

    Args:
        offer_type: Absolute or relative offer type
        cjfee: Fee value (int for absolute, string decimal for relative)
        amount: CoinJoin amount in satoshis

    Returns:
        Actual fee in satoshis
    """
    if is_absolute_offer_type(offer_type):
        return int(cjfee)
    else:
        return calculate_relative_fee(amount, str(cjfee))


class Offer(BaseModel):
    counterparty: str = Field(..., min_length=1)
    oid: int = Field(..., ge=0)
    ordertype: OfferType
    minsize: int = Field(..., ge=0, le=MAX_MONEY)
    maxsize: int = Field(..., ge=0, le=MAX_MONEY)
    txfee: int = Field(..., ge=0, le=MAX_MONEY)
    cjfee: str | int
    fidelity_bond_value: int = Field(default=0, ge=0)
    fidelity_bond_verified: bool | None = Field(
        default=None,
        description="Whether the watcher definitively verified or rejected the advertised bond",
    )
    fidelity_bond_verification_stale: bool = Field(
        default=False,
        description="Whether watcher verification exceeded its revalidation TTL",
    )
    directory_node: str | None = None
    directory_nodes: list[str] = Field(
        default_factory=list,
        description="All directory nodes that announced this offer (for statistics)",
    )
    fidelity_bond_data: dict[str, Any] | None = None
    neutrino_compat: bool = Field(
        default=False,
        description="Maker requires extended UTXO format (neutrino-compatible backend)",
    )
    features: dict[str, bool] = Field(
        default_factory=dict,
        description="Features supported by this maker (from handshake)",
    )
    directly_reachable: bool | None = Field(
        default=None,
        description="Whether maker is directly reachable via their onion address (None = not checked)",
    )

    @field_validator("cjfee")
    @classmethod
    def validate_cjfee(cls, v: str | int, info) -> str | int:
        ordertype = info.data.get("ordertype")
        if ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
            # Absolute fees are integer satoshis; reject negatives and absurdly
            # large values that could overflow downstream amount arithmetic.
            iv = int(v)
            if iv < 0:
                raise ValueError("absolute cjfee must be non-negative")
            # 21M BTC in satoshis - any single-offer fee above this is nonsense
            # and could be used to trick takers into oversized fee calculations.
            if iv > MAX_MONEY:
                raise ValueError("absolute cjfee exceeds maximum money supply")
            return iv
        return normalize_relative_fee(v, "relative cjfee")

    @model_validator(mode="after")
    def validate_size_range(self) -> Offer:
        """Reject offers whose advertised minimum exceeds their maximum."""
        if self.minsize > self.maxsize:
            raise ValueError("minsize must be less than or equal to maxsize")
        return self

    def is_absolute_fee(self) -> bool:
        return is_absolute_offer_type(self.ordertype)

    def calculate_fee(self, amount: int) -> int:
        return calculate_cj_fee(self.ordertype, self.cjfee, amount)


class FidelityBond(BaseModel):
    counterparty: str
    utxo_txid: str = Field(..., pattern=r"^[0-9a-fA-F]{64}$")
    utxo_vout: int = Field(..., ge=0)
    bond_value: int | None = Field(default=None, ge=0)
    verification_valid: bool | None = Field(
        default=None,
        description="Whether backend verification definitively accepted or rejected this bond",
    )
    verification_stale: bool = Field(
        default=False,
        description="Whether the displayed value is retained past its verification TTL",
    )
    locktime: int = Field(..., ge=0)
    amount: int = Field(default=0, ge=0)
    script: str
    utxo_confirmations: int = Field(..., ge=0)
    utxo_confirmation_timestamp: int = Field(default=0, ge=0)
    cert_expiry: int = Field(..., ge=0)
    directory_node: str | None = None
    directory_nodes: list[str] = Field(
        default_factory=list,
        description="All directory nodes that announced this bond (for statistics)",
    )
    fidelity_bond_data: dict[str, Any] | None = None


class OrderBook(BaseModel):
    offers: list[Offer] = Field(default_factory=list)
    fidelity_bonds: list[FidelityBond] = Field(default_factory=list)
    current_block_height: int | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    directory_nodes: list[str] = Field(default_factory=list)

    def add_offers(self, offers: list[Offer], directory_node: str) -> None:
        for offer in offers:
            offer.directory_node = directory_node
        self.offers.extend(offers)
        if directory_node not in self.directory_nodes:
            self.directory_nodes.append(directory_node)

    def add_fidelity_bonds(self, bonds: list[FidelityBond], directory_node: str) -> None:
        for bond in bonds:
            bond.directory_node = directory_node
        self.fidelity_bonds.extend(bonds)

    def get_offers_by_directory(self) -> dict[str, list[Offer]]:
        """Get offers grouped by directory node.

        This uses the directory_nodes list (plural) which tracks all directories
        that announced each offer, so an offer will appear under multiple
        directories if it was announced by multiple directories.
        """
        result: dict[str, list[Offer]] = {}
        for offer in self.offers:
            # Use directory_nodes (plural) if populated, otherwise fallback to directory_node
            nodes = offer.directory_nodes if offer.directory_nodes else []
            if not nodes and offer.directory_node:
                nodes = [offer.directory_node]
            if not nodes:
                nodes = ["unknown"]
            for node in nodes:
                if node not in result:
                    result[node] = []
                result[node].append(offer)
        return result
