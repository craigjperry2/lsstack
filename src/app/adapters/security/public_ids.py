"""Salted deterministic Sqids public identifiers."""

from hashlib import sha256

from sqids import Sqids


def permute_alphabet(alphabet: str, salt: str) -> str:
    """Derive a stable salt-specific permutation without process-randomized hash()."""
    if len(alphabet) < 3 or len(set(alphabet)) != len(alphabet):
        raise ValueError("The Sqids alphabet requires at least 3 unique characters.")
    if not salt:
        raise ValueError("The Sqids salt must not be empty.")
    salt_bytes = salt.encode("utf-8")
    indexed = enumerate(alphabet)
    ordered = sorted(
        indexed,
        key=lambda pair: (
            sha256(
                salt_bytes
                + b"\0"
                + pair[1].encode("utf-8")
                + pair[0].to_bytes(4, "big")
            ).digest(),
            pair[0],
        ),
    )
    return "".join(character for _, character in ordered)


class SqidsPublicIdCodec:
    def __init__(self, *, salt: str, alphabet: str, min_length: int) -> None:
        if min_length < 1:
            raise ValueError("Sqids minimum length must be positive.")
        self._sqids = Sqids(
            alphabet=permute_alphabet(alphabet, salt),
            min_length=min_length,
        )

    def encode(self, internal_id: int) -> str:
        if internal_id < 1:
            raise ValueError("Only positive internal identifiers can be encoded.")
        return self._sqids.encode([internal_id])

    def decode(self, public_id: str) -> int | None:
        if not public_id:
            return None
        try:
            decoded = self._sqids.decode(public_id)
        except ValueError:
            return None
        if len(decoded) != 1 or decoded[0] < 1:
            return None
        # Reject aliases/non-canonical strings accepted by a decoder.
        if self._sqids.encode(decoded) != public_id:
            return None
        return decoded[0]
