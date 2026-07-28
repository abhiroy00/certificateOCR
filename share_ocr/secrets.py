"""Safe storage for the OpenAI API key.

Threat model, stated honestly so nobody is misled:

  * We protect against the realistic failure modes: the key ending up in
    settings.json, in the log file, in a screenshot, in the CSV, in a support
    zip, or committed to git by an operator.
  * We do NOT protect against an attacker who already has code execution as
    your Windows/macOS user. Nothing a desktop app can do stops that - whatever
    the app can decrypt, malware running as you can decrypt too.

Storage backends, in the order they are read:

  1. Environment variable (OPENAI_API_KEY). Highest priority, never written by
     us. This is what you use on a server / in CI.
  2. OS credential store via `keyring`: Windows Credential Manager, macOS
     Keychain, or Secret Service on Linux. The OS encrypts it against your
     login. This is the recommended option and what the GUI defaults to.
  3. Local file `<workdir>/credentials.json`, chmod 0600, value obfuscated
     with a machine-derived XOR pad. This is a FALLBACK for machines with no
     keyring (bare Linux boxes, portable installs). It is obfuscation, not
     encryption - the UI says so out loud.

The key is never written to settings.json and never passed to the logger.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import platform
import stat
from pathlib import Path
from typing import Optional, Tuple

SERVICE = "share_ocr"
ACCOUNT = "openai_api_key"
CRED_FILENAME = "credentials.json"

SOURCE_ENV = "env"
SOURCE_KEYRING = "keyring"
SOURCE_FILE = "file"
SOURCE_NONE = "none"

SOURCE_LABELS = {
    SOURCE_ENV: "Environment variable",
    SOURCE_KEYRING: "OS credential store",
    SOURCE_FILE: "Local file (obfuscated)",
    SOURCE_NONE: "Not configured",
}


# ----------------------------------------------------------- keyring ------

def keyring_available() -> bool:
    """True if a *working* OS credential store is present.

    `import keyring` succeeding is not enough - on a headless Linux box it
    imports fine and then fails at runtime, so we probe it.
    """
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        backend = keyring.get_keyring()
        if isinstance(backend, FailKeyring):
            return False
        # Probe: a read on a missing entry must not raise.
        keyring.get_password(SERVICE, "__probe__")
        return True
    except Exception:                                      # noqa: BLE001
        return False


def keyring_backend_name() -> str:
    try:
        import keyring

        return type(keyring.get_keyring()).__name__
    except Exception:                                      # noqa: BLE001
        return "unavailable"


# ------------------------------------------------------ file fallback -----

def _pad(length: int) -> bytes:
    """Machine-and-user derived pad. Moving the file to another box or user
    account makes it undecodable, which is the point: a stolen file alone is
    not enough."""
    seed = "|".join([
        platform.node(),
        platform.machine(),
        getpass.getuser(),
        SERVICE,
    ]).encode("utf-8")
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _obfuscate(value: str) -> str:
    raw = value.encode("utf-8")
    xored = bytes(a ^ b for a, b in zip(raw, _pad(len(raw))))
    return base64.b64encode(xored).decode("ascii")


def _deobfuscate(blob: str) -> Optional[str]:
    try:
        xored = base64.b64decode(blob.encode("ascii"))
        raw = bytes(a ^ b for a, b in zip(xored, _pad(len(xored))))
        return raw.decode("utf-8")
    except Exception:                                      # noqa: BLE001
        return None


def _cred_path(workdir: Path) -> Path:
    return Path(workdir) / CRED_FILENAME


def _lock_down(path: Path) -> None:
    """Owner-only permissions. No-op semantics differ on Windows, where NTFS
    inheritance from the user profile folder already restricts access."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)        # 0600
    except Exception:                                      # noqa: BLE001
        pass
    try:
        os.chmod(path.parent, stat.S_IRWXU)                # 0700
    except Exception:                                      # noqa: BLE001
        pass


def _read_file_key(workdir: Path) -> Optional[str]:
    p = _cred_path(workdir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text("utf-8"))
        blob = data.get(ACCOUNT)
        return _deobfuscate(blob) if blob else None
    except Exception:                                      # noqa: BLE001
        return None


def _write_file_key(workdir: Path, value: str) -> None:
    p = _cred_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "Obfuscated, machine-bound. Do NOT commit or copy to "
                    "another machine. Prefer the OS credential store.",
        ACCOUNT: _obfuscate(value),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _lock_down(p)


def _delete_file_key(workdir: Path) -> bool:
    p = _cred_path(workdir)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:                                  # noqa: BLE001
            return False
    return False


# --------------------------------------------------------- public API -----

def resolve(settings) -> Tuple[Optional[str], str]:
    """Return (key, source). Precedence: env > keyring > file."""
    env_name = getattr(settings, "api_key_env", "OPENAI_API_KEY")
    val = os.environ.get(env_name)
    if val and val.strip():
        return val.strip(), SOURCE_ENV

    if keyring_available():
        try:
            import keyring

            val = keyring.get_password(SERVICE, ACCOUNT)
            if val and val.strip():
                return val.strip(), SOURCE_KEYRING
        except Exception:                                  # noqa: BLE001
            pass

    val = _read_file_key(Path(settings.workdir))
    if val and val.strip():
        return val.strip(), SOURCE_FILE

    return None, SOURCE_NONE


def get_api_key(settings) -> Optional[str]:
    return resolve(settings)[0]


def save_api_key(settings, key: str, prefer_keyring: bool = True) -> str:
    """Persist the key. Returns the source it was written to.

    Raises RuntimeError if nothing could be written.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("Empty key")

    if prefer_keyring and keyring_available():
        try:
            import keyring

            keyring.set_password(SERVICE, ACCOUNT, key)
            # If a stale plaintext-ish fallback exists, remove it so there is
            # exactly one copy on disk.
            _delete_file_key(Path(settings.workdir))
            return SOURCE_KEYRING
        except Exception:                                  # noqa: BLE001
            pass

    _write_file_key(Path(settings.workdir), key)
    return SOURCE_FILE


def delete_api_key(settings) -> list:
    """Remove the key from every place we control. Returns what was cleared.
    The environment variable is not ours to unset - we report it instead."""
    cleared = []
    if keyring_available():
        try:
            import keyring

            if keyring.get_password(SERVICE, ACCOUNT):
                keyring.delete_password(SERVICE, ACCOUNT)
                cleared.append(SOURCE_KEYRING)
        except Exception:                                  # noqa: BLE001
            pass
    if _delete_file_key(Path(settings.workdir)):
        cleared.append(SOURCE_FILE)
    return cleared


def mask(key: Optional[str]) -> str:
    """sk-proj-abc...WXYZ - safe to show on screen and in logs."""
    if not key:
        return "—"
    key = key.strip()
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:7]}…{key[-4:]}"


def looks_like_openai_key(key: str) -> bool:
    key = (key or "").strip()
    return key.startswith(("sk-", "sess-")) and len(key) >= 20


def describe(settings) -> dict:
    """Everything the UI needs to render the key status."""
    key, source = resolve(settings)
    return {
        "configured": bool(key),
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "masked": mask(key),
        "env_name": getattr(settings, "api_key_env", "OPENAI_API_KEY"),
        "keyring": keyring_available(),
        "keyring_backend": keyring_backend_name(),
        "file_path": str(_cred_path(Path(settings.workdir))),
    }


def test_key(settings, key: Optional[str] = None, timeout: int = 20) -> Tuple[bool, str]:
    """Cheapest possible live check: list models. Returns (ok, message)."""
    key = (key or get_api_key(settings) or "").strip()
    if not key:
        return False, "No API key configured."
    try:
        from openai import OpenAI
    except Exception:                                      # noqa: BLE001
        return False, "The `openai` package is not installed (pip install openai)."
    try:
        kwargs = {"api_key": key, "timeout": timeout}
        if getattr(settings, "base_url", ""):
            kwargs["base_url"] = settings.base_url
        client = OpenAI(**kwargs)
        models = client.models.list()
        names = [m.id for m in list(models)[:200]]
        want = getattr(settings, "model", "")
        if want and want not in names:
            return True, (f"Key works. Note: '{want}' was not in the list of "
                          f"models this key can see.")
        return True, f"Key works. {len(names)} model(s) available."
    except Exception as e:                                 # noqa: BLE001
        msg = str(e)
        if "401" in msg or "invalid_api_key" in msg or "Incorrect API key" in msg:
            return False, "Rejected: the key is invalid or revoked."
        if "429" in msg:
            return False, "Key is valid but rate-limited / out of quota."
        # Never echo the key back, even if the SDK put it in the message.
        return False, msg.replace(key, mask(key))[:300]
