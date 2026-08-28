"""Gasless settlement primitives for the live engine.

ABI encoding for the ConditionalTokens contract, collection/position id
derivation over alt_bn128, and EIP-712 batch signing. Pure computation only —
no I/O, no venue calls. The relayer submit path and the payout-denominator RPC
read stay in engine.live_exec next to the CLI verbs that use them.

Extracted from engine.live_exec (Session 62-era code, previously the one
3,370-line module). Signatures are unchanged; this is relocation for locality,
not a redesign.

Credential rule, inherited from live_exec: a key is a parameter, never a
module-level value. Nothing here prints, logs, or writes a key.
"""
from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

# ConditionalTokens contract on Polygon mainnet. Home of redeemPositions /
# mergePositions; the payout read and the relayer payload also target it, so
# live_exec imports this constant rather than defining a second copy.
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# Collateral token (USDC.e on Polygon) and the empty bytes32 parent collection.
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

ALT_BN128_P = 21888242871839275222246405745257275088696311157297823662689037894645226208583
ALT_BN128_B = 3


def _alt_bn128_sqrt(x: int) -> int:
    """Modular square root on F_P for alt_bn128 (P % 4 == 3)."""
    return pow(x, (ALT_BN128_P + 1) // 4, ALT_BN128_P)


def _alt_bn128_add(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int]:
    """Affine point addition on alt_bn128 (E: y^2 = x^3 + 3 over F_P).
    Equivalent to EVM ecAdd precompile at address(6).
    """
    p = ALT_BN128_P
    if x1 == 0 and y1 == 0:
        return x2, y2
    if x2 == 0 and y2 == 0:
        return x1, y1
    if x1 == x2:
        if (y1 + y2) % p == 0:
            return 0, 0
        slope = (3 * x1 * x1) * pow(2 * y1, p - 2, p) % p
    else:
        slope = (y2 - y1) * pow(x2 - x1, p - 2, p) % p
    x3 = (slope * slope - x1 - x2) % p
    y3 = (slope * (x1 - x3) - y1) % p
    return x3, y3


def encode_redeem_positions(collateral_token: str, parent_collection_id: str,
                            condition_id: str, index_sets: list[int]) -> str:
    """Encode ABI call for ConditionalTokens.redeemPositions(address,bytes32,bytes32,uint256[])
    Selector: 0x01b7037c
    """
    selector = "01b7037c"
    p_col = collateral_token.lower().replace("0x", "").zfill(64)
    p_parent = parent_collection_id.lower().replace("0x", "").zfill(64)
    p_cond = condition_id.lower().replace("0x", "").zfill(64)
    offset = hex(128)[2:].zfill(64)
    len_idx = hex(len(index_sets))[2:].zfill(64)
    elem_idx = "".join(hex(idx)[2:].zfill(64) for idx in index_sets)
    return "0x" + selector + p_col + p_parent + p_cond + offset + len_idx + elem_idx


def get_collection_id(parent_collection_id: str, condition_id: str, index_set: int) -> str:
    """Construct an outcome collection ID from a parent collection and an outcome collection.
    Canonical port of CTHelpers.sol:392-424 (gnosis/conditional-tokens-contracts).
    """
    p = ALT_BN128_P
    b = ALT_BN128_B

    cond_bytes = bytes.fromhex(condition_id.lower().replace("0x", "").zfill(64))
    idx_bytes = int(index_set).to_bytes(32, byteorder="big")
    raw_hash = keccak(cond_bytes + idx_bytes)
    x1 = int.from_bytes(raw_hash, byteorder="big")
    odd = (x1 >> 255) != 0

    while True:
        x1 = (x1 + 1) % p
        yy = (pow(x1, 3, p) + b) % p
        y1 = _alt_bn128_sqrt(yy)
        if (y1 * y1) % p == yy:
            break

    if (odd and y1 % 2 == 0) or (not odd and y1 % 2 == 1):
        y1 = p - y1

    x2 = int(parent_collection_id, 16) if parent_collection_id else 0
    if x2 != 0:
        odd_parent = (x2 >> 254) != 0
        x2 = x2 & ((1 << 254) - 1)
        yy_parent = (pow(x2, 3, p) + b) % p
        y2 = _alt_bn128_sqrt(yy_parent)
        if (odd_parent and y2 % 2 == 0) or (not odd_parent and y2 % 2 == 1):
            y2 = p - y2
        if (y2 * y2) % p != yy_parent:
            raise ValueError("invalid parent collection ID")
        x1, y1 = _alt_bn128_add(x1, y1, x2, y2)

    if y1 % 2 == 1:
        x1 ^= 1 << 254

    return "0x" + hex(x1)[2:].zfill(64)


def get_position_id(collateral_token: str, collection_id: str) -> str:
    """Compute positionId = uint256(keccak256(abi.encodePacked(collateralToken, collectionId))).
    Source: CTHelpers.sol getPositionId (gnosis/conditional-tokens-contracts).
    """
    col_bytes = bytes.fromhex(collateral_token.lower().replace("0x", "").zfill(40))
    coll_bytes = bytes.fromhex(collection_id.lower().replace("0x", "").zfill(64))
    return str(int.from_bytes(keccak(col_bytes + coll_bytes), byteorder="big"))


def encode_merge_positions(collateral_token: str, parent_collection_id: str,
                           condition_id: str, index_sets: list[int],
                           amount: int) -> str:
    """Encode ABI call for ConditionalTokens.mergePositions(address,bytes32,bytes32,uint256[],uint256)
    Selector: 0x9e7212ad (keccak256(b"mergePositions(address,bytes32,bytes32,uint256[],uint256)")[:4])
    Source: ConditionalTokens.sol:165-171 (gnosis/conditional-tokens-contracts).
    """
    selector = "9e7212ad"
    p_col = collateral_token.lower().replace("0x", "").zfill(64)
    p_parent = parent_collection_id.lower().replace("0x", "").zfill(64)
    p_cond = condition_id.lower().replace("0x", "").zfill(64)
    offset = hex(160)[2:].zfill(64)  # 5 static words in head * 32 bytes = 160 = 0xa0
    p_amount = hex(int(amount))[2:].zfill(64)
    len_idx = hex(len(index_sets))[2:].zfill(64)
    elem_idx = "".join(hex(int(idx))[2:].zfill(64) for idx in index_sets)
    return "0x" + selector + p_col + p_parent + p_cond + offset + p_amount + len_idx + elem_idx


def build_redeem_typed_data(funder: str, nonce: int, deadline: int, call_data: str) -> tuple[dict, dict, dict]:
    """Build EIP-712 typed data structures for DepositWallet.Batch."""
    domain = {
        "name": "DepositWallet",
        "version": "1",
        "chainId": 137,
        "verifyingContract": funder,
    }
    types = {
        "Call": [
            {"name": "target", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "Batch": [
            {"name": "wallet", "type": "address"},
            {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "calls", "type": "Call[]"},
        ],
    }
    call_bytes = bytes.fromhex(call_data[2:] if call_data.startswith("0x") else call_data)
    message = {
        "wallet": funder,
        "nonce": int(nonce),
        "deadline": int(deadline),
        "calls": [
            {
                "target": CTF_CONTRACT,
                "value": 0,
                "data": call_bytes,
            }
        ],
    }
    return domain, types, message


def sign_redeem_transaction(key: str, funder: str, nonce: int, deadline: int, call_data: str) -> tuple[str, str]:
    """Sign DepositWallet EIP-712 Batch transaction with EOA key."""
    domain, types, message = build_redeem_typed_data(funder, nonce, deadline, call_data)
    typed = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
    signer_acc = Account.from_key(key)
    signed = signer_acc.sign_message(typed)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return signer_acc.address, sig
