from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.adapters.security.public_ids import SqidsPublicIdCodec, permute_alphabet
from app.adapters.security.sessions import claims_from_session, claims_to_session
from app.application.auth import SessionClaims
from app.config import DEFAULT_SQIDS_ALPHABET


def test_sqids_are_stable_salted_and_canonical() -> None:
    first = SqidsPublicIdCodec(
        salt="first-salt", alphabet=DEFAULT_SQIDS_ALPHABET, min_length=8
    )
    same = SqidsPublicIdCodec(
        salt="first-salt", alphabet=DEFAULT_SQIDS_ALPHABET, min_length=8
    )
    changed = SqidsPublicIdCodec(
        salt="second-salt", alphabet=DEFAULT_SQIDS_ALPHABET, min_length=8
    )
    public_id = first.encode(42)
    assert public_id == same.encode(42)
    assert public_id != changed.encode(42)
    assert first.decode(public_id) == 42
    assert first.decode("") is None
    assert first.decode("malformed!") is None
    assert permute_alphabet(DEFAULT_SQIDS_ALPHABET, "first-salt") != (
        DEFAULT_SQIDS_ALPHABET
    )


def test_session_claim_serialization_rejects_malformed_values() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claims = SessionClaims(7, 3, now, now + timedelta(hours=12))
    assert claims_from_session(claims_to_session(claims)) == claims
    assert claims_from_session({"user_id": True}) is None
    assert (
        claims_from_session(
            {
                "user_id": 1,
                "session_version": 0,
                "issued_at": 100,
                "expires_at": 99,
            }
        )
        is None
    )
