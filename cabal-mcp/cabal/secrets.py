"""Secret retrieval: macOS Keychain primary, AWS SSM Parameter Store backup.

Lookup order for any secret `name`:
  1. Environment variable `CABAL_<NAME_UPPER>` (overrides everything, useful for tests/CI).
  2. macOS Keychain — service `cabal-mcp`, account `<name>`.
  3. AWS SSM Parameter Store — `/cabal-mcp/<name>` (SecureString).

If none match, raises SecretNotFoundError with the canonical names tried.

Writes go to *both* keychain and SSM (so they stay in sync). One-shot
`store` is for initial setup; the server itself only reads.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

SERVICE = "cabal-mcp"
SSM_PREFIX = "/cabal-mcp"


class SecretNotFoundError(RuntimeError):
    pass


@dataclass
class SecretLookup:
    name: str
    value: str
    source: str  # "env" | "keychain" | "ssm"


def env_var_name(name: str) -> str:
    return "CABAL_" + name.upper().replace("-", "_")


def _from_env(name: str) -> str | None:
    return os.environ.get(env_var_name(name))


def _from_keychain(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0:
            return out.stdout.rstrip("\n")
    except FileNotFoundError:
        # `security` not on PATH — not macOS, or stripped image.
        pass
    return None


def _from_ssm(name: str) -> str | None:
    # Lazy import: don't pay the boto3 import cost unless we actually fall through.
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return None
    try:
        ssm = boto3.client("ssm")
        resp = ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)
        return resp["Parameter"]["Value"]
    except ClientError:
        return None
    except Exception:
        return None


def get(name: str) -> SecretLookup:
    """Look up a secret. Raises SecretNotFoundError if missing everywhere."""
    if (v := _from_env(name)) is not None:
        return SecretLookup(name, v, "env")
    if (v := _from_keychain(name)) is not None:
        return SecretLookup(name, v, "keychain")
    if (v := _from_ssm(name)) is not None:
        return SecretLookup(name, v, "ssm")
    raise SecretNotFoundError(
        f"Secret '{name}' not found. Tried env({env_var_name(name)}), "
        f"keychain(service={SERVICE}, account={name}), "
        f"SSM({SSM_PREFIX}/{name})."
    )


def get_optional(name: str) -> str | None:
    try:
        return get(name).value
    except SecretNotFoundError:
        return None


def store(name: str, value: str, *, ssm_too: bool = True) -> None:
    """Write `value` to keychain (and optionally SSM). For one-shot setup, not the server hot path."""
    # Keychain — delete first so we don't fail on a duplicate.
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", name],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["security", "add-generic-password",
         "-s", SERVICE, "-a", name, "-w", value, "-U"],
        check=True,
    )
    if ssm_too:
        try:
            import boto3
            ssm = boto3.client("ssm")
            ssm.put_parameter(
                Name=f"{SSM_PREFIX}/{name}",
                Value=value,
                Type="SecureString",
                Overwrite=True,
            )
        except Exception as e:
            # SSM is the backup; failing to write it shouldn't block the keychain write.
            print(f"warning: SSM mirror write failed for {name}: {e}")
