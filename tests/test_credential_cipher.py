from __future__ import annotations

import base64

import pytest

from colmat_x.credential_cipher import (
    PRIVATE_KEY_ENV,
    PUBLIC_KEY_ENV,
    CredentialCipher,
    CredentialCipherConfigurationError,
    CredentialCipherUnavailable,
    CredentialEnvelopeError,
    credential_fingerprint,
)


def test_keypair_roundtrip_binds_context_and_hides_plaintext() -> None:
    private_key, public_key = CredentialCipher.generate_keypair()
    sealer = CredentialCipher.from_environment({PUBLIC_KEY_ENV: public_key})
    opener = CredentialCipher.from_environment({PRIVATE_KEY_ENV: private_key})
    assert sealer.can_encrypt and not sealer.can_decrypt
    assert opener.can_encrypt and opener.can_decrypt
    assert sealer.key_id == opener.key_id

    envelope = sealer.seal("sk-live-secret-value", context="colmat:ai-credential:colmat:openai")

    assert "sk-live-secret-value" not in envelope
    assert envelope != sealer.seal(
        "sk-live-secret-value", context="colmat:ai-credential:colmat:openai"
    )
    assert (
        opener.open(envelope, context="colmat:ai-credential:colmat:openai")
        == "sk-live-secret-value"
    )
    with pytest.raises(CredentialEnvelopeError, match="contexto"):
        opener.open(envelope, context="colmat:ai-credential:colmat:gemini")
    with pytest.raises(CredentialCipherUnavailable, match=PRIVATE_KEY_ENV):
        sealer.open(envelope, context="colmat:ai-credential:colmat:openai")


def test_tampered_or_foreign_envelopes_are_rejected() -> None:
    private_key, _public_key = CredentialCipher.generate_keypair()
    other_private, _other_public = CredentialCipher.generate_keypair()
    owner = CredentialCipher.from_environment({PRIVATE_KEY_ENV: private_key})
    stranger = CredentialCipher.from_environment({PRIVATE_KEY_ENV: other_private})
    envelope = owner.seal("another-secret-value", context="ctx")

    raw = bytearray(base64.urlsafe_b64decode(envelope))
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(CredentialEnvelopeError):
        owner.open(tampered, context="ctx")
    with pytest.raises(CredentialEnvelopeError):
        stranger.open(envelope, context="ctx")
    with pytest.raises(CredentialEnvelopeError, match="inválido"):
        owner.open("not-an-envelope", context="ctx")
    with pytest.raises(CredentialEnvelopeError, match="vacío"):
        owner.seal("", context="ctx")


def test_degraded_mode_declares_itself_and_never_stores_plaintext() -> None:
    degraded = CredentialCipher.from_environment({})
    assert degraded.can_encrypt is False
    assert degraded.can_decrypt is False
    assert degraded.key_id is None
    with pytest.raises(CredentialCipherUnavailable, match=PUBLIC_KEY_ENV):
        degraded.seal("secret-value-here", context="ctx")


def test_environment_keys_are_validated() -> None:
    with pytest.raises(CredentialCipherConfigurationError, match="32 bytes"):
        CredentialCipher.from_environment({PUBLIC_KEY_ENV: "short"})
    private_key, _public = CredentialCipher.generate_keypair()
    _other_private, other_public = CredentialCipher.generate_keypair()
    with pytest.raises(CredentialCipherConfigurationError, match="no corresponde"):
        CredentialCipher.from_environment(
            {PRIVATE_KEY_ENV: private_key, PUBLIC_KEY_ENV: other_public}
        )


def test_fingerprint_is_stable_sha256() -> None:
    assert credential_fingerprint("abc") == credential_fingerprint("abc")
    assert len(credential_fingerprint("abc")) == 64
    assert credential_fingerprint("abc") != credential_fingerprint("abd")
