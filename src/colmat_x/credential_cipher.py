"""Sobre sellado para credenciales de IA guardadas en la base.

Diseño: cifrado asimétrico X25519 + HKDF-SHA256 + AES-256-GCM (AEAD). El
proceso que recibe una clave (panel web o webhook de Telegram) solo necesita la
clave pública para sellarla; únicamente el worker, que posee la clave privada,
puede abrir el sobre. Así una filtración del entorno web más la base no basta
para recuperar credenciales: hace falta además la clave privada del worker.

El contexto (workspace y proveedor) se liga como dato autenticado adicional, de
modo que un sobre copiado a otra fila no se abre. Si faltan ambas claves, el
cifrador arranca en modo degradado y lo declara; nunca guarda claves en claro.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PUBLIC_KEY_ENV = "COLMAT_CREDENTIAL_PUBLIC_KEY"
PRIVATE_KEY_ENV = "COLMAT_CREDENTIAL_PRIVATE_KEY"
_VERSION = b"C1"
_INFO = b"colmat-credential-envelope-v1"
_RAW_KEY_LENGTH = 32
_NONCE_LENGTH = 12
MAX_PLAINTEXT_BYTES = 4_096


class CredentialCipherError(RuntimeError):
    """Error base del sobre de credenciales."""


class CredentialCipherUnavailable(CredentialCipherError):
    """El proceso no tiene la clave necesaria para sellar o abrir sobres."""


class CredentialCipherConfigurationError(CredentialCipherError):
    """Las claves del entorno no tienen el formato esperado."""


class CredentialEnvelopeError(CredentialCipherError):
    """El sobre está corrupto, fue alterado o pertenece a otro contexto."""


@dataclass(frozen=True)
class CredentialCipher:
    public_key: X25519PublicKey | None
    private_key: X25519PrivateKey | None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> CredentialCipher:
        """Lee las claves del entorno; sin ninguna, devuelve un cifrador degradado."""

        source = os.environ if environ is None else environ
        private_raw = _decode_key(source.get(PRIVATE_KEY_ENV), PRIVATE_KEY_ENV)
        public_raw = _decode_key(source.get(PUBLIC_KEY_ENV), PUBLIC_KEY_ENV)
        private_key = X25519PrivateKey.from_private_bytes(private_raw) if private_raw else None
        public_key = X25519PublicKey.from_public_bytes(public_raw) if public_raw else None
        if private_key is not None:
            derived = private_key.public_key()
            if public_key is not None and _raw_public(public_key) != _raw_public(derived):
                raise CredentialCipherConfigurationError(
                    f"{PUBLIC_KEY_ENV} no corresponde a {PRIVATE_KEY_ENV}"
                )
            public_key = derived
        return cls(public_key=public_key, private_key=private_key)

    @classmethod
    def degraded(cls) -> CredentialCipher:
        return cls(public_key=None, private_key=None)

    @staticmethod
    def generate_keypair() -> tuple[str, str]:
        """Devuelve (privada, pública) en base64url para los archivos 0600."""

        private_key = X25519PrivateKey.generate()
        private_raw = private_key.private_bytes_raw()
        public_raw = _raw_public(private_key.public_key())
        return _encode(private_raw), _encode(public_raw)

    @property
    def can_encrypt(self) -> bool:
        return self.public_key is not None

    @property
    def can_decrypt(self) -> bool:
        return self.private_key is not None

    @property
    def key_id(self) -> str | None:
        """Identificador público del par de claves; no revela material secreto."""

        if self.public_key is None:
            return None
        return hashlib.sha256(_raw_public(self.public_key)).hexdigest()[:16]

    def seal(self, plaintext: str, *, context: str) -> str:
        if self.public_key is None:
            raise CredentialCipherUnavailable(
                f"El almacén cifrado no está disponible: falta {PUBLIC_KEY_ENV}"
            )
        if not isinstance(plaintext, str) or not plaintext:
            raise CredentialEnvelopeError("No se puede sellar un valor vacío")
        payload = plaintext.encode("utf-8")
        if len(payload) > MAX_PLAINTEXT_BYTES:
            raise CredentialEnvelopeError("El valor supera el tamaño máximo sellable")
        ephemeral = X25519PrivateKey.generate()
        ephemeral_public = _raw_public(ephemeral.public_key())
        recipient_public = _raw_public(self.public_key)
        key = _derive_key(ephemeral.exchange(self.public_key), ephemeral_public, recipient_public)
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = AESGCM(key).encrypt(nonce, payload, _aad(context))
        return _encode(_VERSION + ephemeral_public + nonce + ciphertext)

    def open(self, token: str, *, context: str) -> str:
        if self.private_key is None:
            raise CredentialCipherUnavailable(
                f"Este proceso no puede descifrar credenciales: falta {PRIVATE_KEY_ENV}"
            )
        raw = _decode_token(token)
        ephemeral_public = raw[2 : 2 + _RAW_KEY_LENGTH]
        nonce = raw[2 + _RAW_KEY_LENGTH : 2 + _RAW_KEY_LENGTH + _NONCE_LENGTH]
        ciphertext = raw[2 + _RAW_KEY_LENGTH + _NONCE_LENGTH :]
        recipient_public = _raw_public(self.private_key.public_key())
        try:
            shared = self.private_key.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
        except ValueError as exc:
            raise CredentialEnvelopeError("El sobre de credencial es inválido") from exc
        key = _derive_key(shared, ephemeral_public, recipient_public)
        try:
            payload = AESGCM(key).decrypt(nonce, ciphertext, _aad(context))
        except InvalidTag as exc:
            raise CredentialEnvelopeError(
                "El sobre de credencial no corresponde a esta clave o a este contexto"
            ) from exc
        return payload.decode("utf-8")


def credential_fingerprint(plaintext: str) -> str:
    """SHA-256 completa; el panel solo muestra un prefijo truncado."""

    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _derive_key(shared: bytes, ephemeral_public: bytes, recipient_public: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_INFO + ephemeral_public + recipient_public,
    ).derive(shared)


def _aad(context: str) -> bytes:
    if not isinstance(context, str) or not context.strip():
        raise CredentialEnvelopeError("El contexto del sobre es obligatorio")
    return context.encode("utf-8")


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes_raw()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_key(value: str | None, name: str) -> bytes | None:
    if value is None or not value.strip():
        return None
    try:
        raw = base64.urlsafe_b64decode(value.strip().encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CredentialCipherConfigurationError(f"{name} debe ser base64url de 32 bytes") from exc
    if len(raw) != _RAW_KEY_LENGTH:
        raise CredentialCipherConfigurationError(f"{name} debe ser base64url de 32 bytes")
    return raw


def _decode_token(token: str) -> bytes:
    if not isinstance(token, str) or not token:
        raise CredentialEnvelopeError("El sobre de credencial está vacío")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CredentialEnvelopeError("El sobre de credencial es inválido") from exc
    minimum = len(_VERSION) + _RAW_KEY_LENGTH + _NONCE_LENGTH + 16
    if len(raw) < minimum or raw[:2] != _VERSION:
        raise CredentialEnvelopeError("El sobre de credencial es inválido")
    return raw
