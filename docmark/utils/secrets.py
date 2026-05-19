"""Thin wrapper around `keyring` for storing the OpenAI API key in the OS keychain."""

import keyring

SERVICE = "docmark"
ACCOUNT = "openai_api_key"


def get_openai_key() -> str | None:
    return keyring.get_password(SERVICE, ACCOUNT)


def set_openai_key(key: str) -> None:
    keyring.set_password(SERVICE, ACCOUNT, key)


def delete_openai_key() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass
