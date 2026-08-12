"""Safe storage for the OpenAI API key(s).

Threat model, stated honestly so nobody is misled:

  * We protect against the realistic failure modes: the key ending up in
    settings.json, in the log file, in a screenshot, in the CSV, in a support
    zip, or committed to git by an operator.
  * We do NOT protect against an attacker who already has code execution as
    your Windows/macOS user. Nothing a desktop app can do stops that - whatever
    the app can decrypt, malware running as you can decrypt too.

Storage backends, in the order they are read:

  1. Environment variable (OPENAI_API_KEYS, comma/semicolon/newline separated,
     or the single-key OPENAI_API_KEY). Highest priority, never written by us.
     This is what you use on a server / in CI.
  2. OS credential store via `keyring`: Windows Credential Manager, macOS
     Keychain, or Secret Service on Linux. The OS encrypts it against your
     login. This is the recommended option and what the GUI defaults to.
  3. Local file `<workdir>/credentials.json`, chmod 0600, value obfuscated
     with a machine-derived XOR pad. This is a FALLBACK for machines with no
     keyring (bare Linux boxes, portable installs). It is obfuscation, not
     encryption - the UI says so out loud.

Multiple keys can be stored at once (the GUI's "API keys" dialog supports as
many as the operator wants to add). extractor.KeyPool round-robins across all
of them and moves a request to the next key when one is rate-limited or out
of quota, so a run does not stall or dead-letter a file just because one key
hit its limit - the point being that with several keys in the pool, a
"Retry failed" click should never be necessary.

The key(s) are never written to settings.json and never passed to the logger.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import platform
import re
import stat
from pathlib import Path
from typing import List, Optional, Tuple

SERVICE = "share_ocr"
ACCOUNT = "openai_api_key"           # legacy single-key entry, read-only now
ACCOUNT_KEYS = "openai_api_keys"     # current entry: a JSON array of keys
CRED_FILENAME = "credentials.json"
_SPLIT_RE = re.compile(r"[,\n;]+")

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


def _read_legacy_file_key(workdir: Path) -> Optional[str]:
    """The single-key entry a pre-multi-key install may still have on disk."""
    p = _cred_path(workdir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text("utf-8"))
        blob = data.get(ACCOUNT)
        return _deobfuscate(blob) if blob else None
    except Exception:                                      # noqa: BLE001
        return None


def _read_file_keys(workdir: Path) -> List[str]:
    p = _cred_path(workdir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:                                      # noqa: BLE001
        return []
    blob = data.get(ACCOUNT_KEYS)
    if blob:
        plain = _deobfuscate(blob)
        if plain:
            try:
                parsed = json.loads(plain)
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except Exception:                              # noqa: BLE001
                pass
    legacy = _read_legacy_file_key(workdir)
    return [legacy] if legacy and legacy.strip() else []


def _write_file_keys(workdir: Path, keys: List[str]) -> None:
    p = _cred_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "Obfuscated, machine-bound. Do NOT commit or copy to "
                    "another machine. Prefer the OS credential store.",
        ACCOUNT_KEYS: _obfuscate(json.dumps(keys)),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _lock_down(p)


def _delete_file_keys(workdir: Path) -> bool:
    p = _cred_path(workdir)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:                                  # noqa: BLE001
            return False
    return False


def _dedupe(keys) -> List[str]:
    seen, out = set(), []
    for k in keys:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


# --------------------------------------------------------- public API -----

def _keys_from_env(settings) -> List[str]:
    multi = os.environ.get("OPENAI_API_KEYS")
    if multi and multi.strip():
        return _dedupe(_SPLIT_RE.split(multi))
    env_name = getattr(settings, "api_key_env", "OPENAI_API_KEY")
    single = os.environ.get(env_name)
    return [single.strip()] if single and single.strip() else []


def _keys_from_keyring() -> List[str]:
    try:
        import keyring

        raw = keyring.get_password(SERVICE, ACCOUNT_KEYS)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    keys = [str(k).strip() for k in parsed if str(k).strip()]
                    if keys:
                        return keys
            except Exception:                              # noqa: BLE001
                pass
        legacy = keyring.get_password(SERVICE, ACCOUNT)
        return [legacy.strip()] if legacy and legacy.strip() else []
    except Exception:                                      # noqa: BLE001
        return []


def resolve_all(settings) -> Tuple[List[str], str]:
    """Return (keys, source). Precedence: env > keyring > file.

    Every worker draws from this same list via extractor.KeyPool, which
    rotates across all of them and moves a request to the next key rather
    than failing the file when one key is rate-limited or out of quota.
    """
    keys = _keys_from_env(settings)
    if keys:
        return keys, SOURCE_ENV

    if keyring_available():
        keys = _keys_from_keyring()
        if keys:
            return keys, SOURCE_KEYRING

    keys = _read_file_keys(Path(settings.workdir))
    if keys:
        return keys, SOURCE_FILE

    return [], SOURCE_NONE


def list_api_keys(settings) -> List[str]:
    return resolve_all(settings)[0]


def resolve(settings) -> Tuple[Optional[str], str]:
    """Return (first key, source) - kept for callers that only need to know
    whether *a* key is available (e.g. the "Extract" button's guard)."""
    keys, source = resolve_all(settings)
    return (keys[0], source) if keys else (None, SOURCE_NONE)


def get_api_key(settings) -> Optional[str]:
    return resolve(settings)[0]


def save_api_keys(settings, keys: List[str], prefer_keyring: bool = True) -> str:
    """Persist the whole key list, replacing whatever was stored before.
    Returns the source it was written to. Raises ValueError if `keys` is
    empty - use delete_all_api_keys() to clear the pool instead."""
    keys = _dedupe(keys)
    if not keys:
        raise ValueError("No keys to save")

    if prefer_keyring and keyring_available():
        try:
            import keyring

            keyring.set_password(SERVICE, ACCOUNT_KEYS, json.dumps(keys))
            try:                                # drop the legacy single entry
                if keyring.get_password(SERVICE, ACCOUNT):
                    keyring.delete_password(SERVICE, ACCOUNT)
            except Exception:                                  # noqa: BLE001
                pass
            # exactly one copy on disk once the keyring holds the pool
            _delete_file_keys(Path(settings.workdir))
            return SOURCE_KEYRING
        except Exception:                                      # noqa: BLE001
            pass

    _write_file_keys(Path(settings.workdir), keys)
    return SOURCE_FILE


def add_api_key(settings, key: str) -> str:
    """Add one key to the pool (a no-op if it is already there)."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Empty key")
    current = list_api_keys(settings)
    if key in current:
        return resolve_all(settings)[1]
    return save_api_keys(settings, current + [key])


def remove_api_key(settings, key: str) -> str:
    """Drop one key from the pool. Returns the resulting source (SOURCE_NONE
    if that was the last key)."""
    remaining = [k for k in list_api_keys(settings) if k != key]
    if not remaining:
        delete_all_api_keys(settings)
        return SOURCE_NONE
    return save_api_keys(settings, remaining)


def delete_all_api_keys(settings) -> list:
    """Remove every stored key from every place we control. Returns what was
    cleared. The environment variable is not ours to unset - callers should
    treat it as still authoritative if it is set."""
    cleared = []
    if keyring_available():
        try:
            import keyring

            for account in (ACCOUNT_KEYS, ACCOUNT):
                if keyring.get_password(SERVICE, account):
                    keyring.delete_password(SERVICE, account)
                    if SOURCE_KEYRING not in cleared:
                        cleared.append(SOURCE_KEYRING)
        except Exception:                                      # noqa: BLE001
            pass
    if _delete_file_keys(Path(settings.workdir)):
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
    """Everything the UI needs to render the key pool status."""
    keys, source = resolve_all(settings)
    return {
        "configured": bool(keys),
        "count": len(keys),
        "keys": keys,                              # in-process only; never logged
        "masked_list": [mask(k) for k in keys],
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "masked": mask(keys[0]) if keys else "—",
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
