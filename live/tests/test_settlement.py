"""Tests for engine.settlement — ConditionalTokens ABI encoding, alt_bn128
collection/position id derivation, and EIP-712 batch signing.

Moved verbatim from test_live_exec.py and test_live_exec_merge.py when the
cluster was extracted out of the 3,370-line CLI module; only the import target
changed (`engine.settlement as s`). The relayer submit path and the payout
denominator RPC read stay in engine.live_exec and keep their tests there.
"""
from __future__ import annotations

import pytest
from eth_account import Account
from eth_utils import keccak

import engine.settlement as s


def test_encode_redeem_positions():
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    res = s.encode_redeem_positions(
        s.USDC_E_CONTRACT,
        s.ZERO_BYTES32,
        cond_id,
        [1, 2]
    )
    assert res.startswith("0x01b7037c")
    assert len(res) == 458  # 2 + 8 (selector) + 7 * 64 (params)
    assert s.USDC_E_CONTRACT.lower().replace("0x", "") in res
    assert cond_id.lower().replace("0x", "") in res


def test_build_redeem_typed_data():
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    nonce = 121
    deadline = 1786855000
    call_data = "0x01b7037c" + "00" * 224

    domain, types, message = s.build_redeem_typed_data(funder, nonce, deadline, call_data)
    assert domain["name"] == "DepositWallet"
    assert domain["version"] == "1"
    assert domain["chainId"] == 137
    assert domain["verifyingContract"] == funder

    assert "Call" in types and "Batch" in types
    assert len(types["Call"]) == 3
    assert len(types["Batch"]) == 4

    assert message["wallet"] == funder
    assert isinstance(message["nonce"], int)
    assert message["nonce"] == nonce
    assert isinstance(message["deadline"], int)
    assert message["deadline"] == deadline
    assert len(message["calls"]) == 1
    assert message["calls"][0]["target"] == s.CTF_CONTRACT
    assert isinstance(message["calls"][0]["value"], int)
    assert message["calls"][0]["value"] == 0


def test_sign_redeem_transaction():
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    nonce = 121
    deadline = 1786855000
    call_data = "0x01b7037c" + "00" * 224

    signer_addr, sig = s.sign_redeem_transaction(
        acc.key.hex(),
        funder,
        nonce,
        deadline,
        call_data
    )
    assert signer_addr == acc.address
    assert sig.startswith("0x")
    assert len(sig) == 132  # 0x + 130 hex chars


def test_derived_position_ids_match_canonical_cthelpers():
    """Round-trip test verifying get_collection_id and get_position_id against real Polymarket CLOB token IDs.

    Provenance:
      Fetched from live Gamma API (https://gamma-api.polymarket.com/markets)
      Market: 'Xi Jinping out before 2027?'
      conditionId: '0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7'
      clobTokenIds:
        indexSet 1 (Yes): '32338220190071351435772801779725302244575775216413325951443816017994629993401'
        indexSet 2 (No):  '25659310674993675562345759665114759892400026242514633218387667107987341231962'
    """
    cond_id = "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"
    expected_token_id_1 = "32338220190071351435772801779725302244575775216413325951443816017994629993401"
    expected_token_id_2 = "25659310674993675562345759665114759892400026242514633218387667107987341231962"

    collection_id_1 = s.get_collection_id(s.ZERO_BYTES32, cond_id, 1)
    pos_id_1 = s.get_position_id(s.USDC_E_CONTRACT, collection_id_1)

    collection_id_2 = s.get_collection_id(s.ZERO_BYTES32, cond_id, 2)
    pos_id_2 = s.get_position_id(s.USDC_E_CONTRACT, collection_id_2)

    assert pos_id_1 == expected_token_id_1
    assert pos_id_2 == expected_token_id_2


# The round-trip test above only ever passes ZERO_BYTES32 as the parent, which is
# all Polymarket itself uses. That leaves the entire `x2 != 0` branch of
# get_collection_id — parent point decode, parity restore, curve check, and the
# alt_bn128 point addition — with no coverage at all. The three tests below enter
# that branch. Constants are conditionId-shaped but synthetic: the branch is pure
# curve arithmetic and does not care whether a condition exists on chain.

_COND_A = "0x" + "11" * 32
_COND_B = "0x" + "22" * 32


def test_non_zero_parent_collection_id_is_distinct_and_deterministic():
    """A non-zero parent must change the result and must do so reproducibly."""
    parent = s.get_collection_id(s.ZERO_BYTES32, _COND_A, 1)

    with_parent = s.get_collection_id(parent, _COND_B, 1)
    without_parent = s.get_collection_id(s.ZERO_BYTES32, _COND_B, 1)

    # Well-formed: 0x + 64 hex chars, parseable as a 256-bit integer.
    assert with_parent.startswith("0x")
    assert len(with_parent) == 66
    int(with_parent, 16)

    # The parent point was actually added, not silently dropped.
    assert with_parent != without_parent

    # Same inputs, same output — no dependence on iteration order or state.
    assert s.get_collection_id(parent, _COND_B, 1) == with_parent


def test_collection_id_point_addition_is_order_independent():
    """P_A + P_B == P_B + P_A: which condition acts as parent must not matter.

    Each outcome point derives from (conditionId, indexSet) alone, so combining
    two of them is commutative. If this fails, the parent decode or the point
    addition is wrong, not the test.
    """
    parent_a = s.get_collection_id(s.ZERO_BYTES32, _COND_A, 1)
    parent_b = s.get_collection_id(s.ZERO_BYTES32, _COND_B, 1)

    a_then_b = s.get_collection_id(parent_a, _COND_B, 1)
    b_then_a = s.get_collection_id(parent_b, _COND_A, 1)

    assert a_then_b == b_then_a


def test_parent_collection_id_off_the_curve_is_rejected():
    """A parent x-coordinate with no square root must raise, not silently add garbage.

    x = 4 is the smallest positive integer for which x^3 + 3 is a non-residue mod
    the alt_bn128 field prime, so it cannot be the x-coordinate of any curve point.
    """
    off_curve_parent = "0x" + hex(4)[2:].zfill(64)

    with pytest.raises(ValueError, match="invalid parent collection ID"):
        s.get_collection_id(off_curve_parent, _COND_A, 1)


def test_merge_selector_derivation():
    """Selector derivation matches canonical Solidity signature:
    mergePositions(address,bytes32,bytes32,uint256[],uint256) -> 0x9e7212ad.
    """
    canonical_sig = b"mergePositions(address,bytes32,bytes32,uint256[],uint256)"
    derived_selector = "0x" + keccak(canonical_sig)[:4].hex()
    assert derived_selector == "0x9e7212ad"


def test_encode_merge_positions_hand_constructed_comparison():
    """Encoder output matches hand-constructed expected hex string.
    Verifies selector, static words, dynamic array offset 0xa0 (160 bytes),
    amount, array length, and elements.
    """
    collateral = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    parent_coll = "0x0000000000000000000000000000000000000000000000000000000000000000"
    cond_id = "0x1111111111111111111111111111111111111111111111111111111111111111"
    index_sets = [1, 2]
    amount_base_units = 5000000  # 5 shares = 0x4c4b40

    # Hand-constructed expected layout:
    # 4 bytes selector + 5 words head + 3 words tail = 260 bytes (520 hex chars + '0x')
    expected_hex = (
        "0x"
        "9e7212ad"  # Selector (4 bytes)
        "0000000000000000000000002791bca1f2de4661ed88a30c99a7a9449aa84174"  # Word 0: collateralToken (32 bytes)
        "0000000000000000000000000000000000000000000000000000000000000000"  # Word 1: parentCollectionId (32 bytes)
        "1111111111111111111111111111111111111111111111111111111111111111"  # Word 2: conditionId (32 bytes)
        "00000000000000000000000000000000000000000000000000000000000000a0"  # Word 3: offset to partition array (160 bytes)
        "00000000000000000000000000000000000000000000000000000000004c4b40"  # Word 4: amount (5000000)
        "0000000000000000000000000000000000000000000000000000000000000002"  # Word 5: length of partition (2)
        "0000000000000000000000000000000000000000000000000000000000000001"  # Word 6: partition[0] (1)
        "0000000000000000000000000000000000000000000000000000000000000002"  # Word 7: partition[1] (2)
    )

    encoded = s.encode_merge_positions(
        collateral_token=collateral,
        parent_collection_id=parent_coll,
        condition_id=cond_id,
        index_sets=index_sets,
        amount=amount_base_units,
    )

    assert encoded == expected_hex
    assert len(encoded) == 2 + 8 + (8 * 64)  # 522 characters
