import os
from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_KEY_ENV_VAR = "RECIPE_APP_ENCRYPTION_KEY"


class EncryptionNotConfiguredError(RuntimeError):
    pass


class DecryptionFailedError(RuntimeError):
    pass


def _get_fernet() -> Fernet | None:
    key = os.environ.get(ENCRYPTION_KEY_ENV_VAR, "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def encryption_configured() -> bool:
    return _get_fernet() is not None


def encrypt_secret(plaintext: str) -> str:
    """Encrypts a credential for storage. Fails closed: if
    RECIPE_APP_ENCRYPTION_KEY isn't set (or isn't a valid Fernet key),
    this raises rather than silently storing plaintext or a value that
    can never be decrypted back."""
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionNotConfiguredError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not set (or is invalid) -- refusing to "
            "store a credential without encryption. Generate one with:\n"
            '  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionNotConfiguredError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not set -- cannot decrypt stored credentials."
        )
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise DecryptionFailedError(
            "Stored credential could not be decrypted -- the encryption key may have "
            "changed since it was saved. Re-enter email credentials in Settings."
        )
