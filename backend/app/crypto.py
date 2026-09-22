import os
from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_KEY_ENV_VAR = "RECIPE_APP_ENCRYPTION_KEY"


class EncryptionNotConfiguredError(RuntimeError):
    pass


class DecryptionFailedError(RuntimeError):
    pass


KEY_FILE_NAME = "encryption.key"


class KeyAlreadyConfiguredError(RuntimeError):
    pass


def key_file_path() -> str:
    # Resolved per call, not at import: the test suite points
    # RECIPE_APP_DATA_DIR at a temp dir after this module may already be
    # imported, and the data dir is the only place the key file may live.
    return os.path.join(os.environ.get("RECIPE_APP_DATA_DIR", "/app/data"), KEY_FILE_NAME)


def _read_key_file() -> str:
    try:
        with open(key_file_path(), "r", encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        # Missing, unreadable, or not text: treated as "no key file". A
        # save still fails closed in that case; it never falls back to
        # plaintext.
        return ""


def key_source() -> str | None:
    """Where the active key comes from: "env", "file", "env_invalid", or None.

    The environment variable always wins when it is set, even if it is
    invalid. Silently falling back to the key file in that case would mean a
    typo in docker-compose.yml switches which key is in use without anyone
    noticing -- and stored credentials would stop decrypting for a reason
    nobody could see.
    """
    env_key = os.environ.get(ENCRYPTION_KEY_ENV_VAR, "").strip()
    if env_key:
        try:
            Fernet(env_key.encode())
            return "env"
        except Exception:
            return "env_invalid"
    file_key = _read_key_file()
    if file_key:
        try:
            Fernet(file_key.encode())
            return "file"
        except Exception:
            return None
    return None


def _get_fernet() -> Fernet | None:
    source = key_source()
    if source == "env":
        return Fernet(os.environ[ENCRYPTION_KEY_ENV_VAR].strip().encode())
    if source == "file":
        return Fernet(_read_key_file().encode())
    return None


def encryption_configured() -> bool:
    return _get_fernet() is not None


def generate_key_file() -> None:
    """Create a key in the data directory -- the in-app alternative to
    setting RECIPE_APP_ENCRYPTION_KEY by hand.

    Refuses if any key is already in play (env var set, valid or not, or a
    key file exists). Replacing a key would make every stored credential
    undecryptable, so that is never something one button press should do.

    The trade-off, stated in the README: the key sits next to the database,
    so a copy of the whole data directory carries both. The backup zip does
    not include this file, so a leaked backup still exposes nothing.
    """
    if os.environ.get(ENCRYPTION_KEY_ENV_VAR, "").strip() or os.path.exists(key_file_path()):
        raise KeyAlreadyConfiguredError("An encryption key is already set up.")
    key = Fernet.generate_key().decode()
    # O_EXCL: if two requests race, the second fails instead of overwriting
    # the first key after it may already have been used. 0600: readable only
    # by the app user.
    fd = os.open(key_file_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key + "\n")


def encrypt_secret(plaintext: str) -> str:
    """Encrypts a credential for storage. Fails closed: if
    RECIPE_APP_ENCRYPTION_KEY isn't set (or isn't a valid Fernet key),
    this raises rather than silently storing plaintext or a value that
    can never be decrypted back."""
    fernet = _get_fernet()
    if fernet is None:
        if key_source() == "env_invalid":
            raise EncryptionNotConfiguredError(
                f"The password wasn't saved. {ENCRYPTION_KEY_ENV_VAR} is set in your "
                "compose file but isn't a valid key. Fix or remove that line, then "
                "restart the container."
            )
        raise EncryptionNotConfiguredError(
            "The password wasn't saved. Email passwords are stored encrypted, and "
            "no encryption key is set up yet. Use \"Set up encryption\" at the top "
            "of the email settings, then save again."
        )
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionNotConfiguredError(
            "No encryption key is set up, so the saved email password can't be "
            "read. If you moved or restored the data folder, bring encryption.key "
            f"with it (or set {ENCRYPTION_KEY_ENV_VAR} to the same key as before)."
        )
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise DecryptionFailedError(
            "Stored credential could not be decrypted -- the encryption key may have "
            "changed since it was saved. Re-enter email credentials in Settings."
        )
