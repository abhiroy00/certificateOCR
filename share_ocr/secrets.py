"""Safe storage for API key(s) - OpenAI and NVIDIA NIM.

Threat model, stated honestly so nobody is misled:

  * We protect against the realistic failure modes: a key ending up in
    settings.json, in the log file, in a screenshot, in the CSV, in a support
    zip, or committed to git by an operator.
  * We do NOT protect against an attacker who already has code execution as
    your Windows/macOS user. Nothing a desktop app can do stops that - whatever
    the app can decrypt, malware running as you can decrypt too.

Two independent providers, each with its own key pool, stored under their own
namespace (never mixed up, never overwriting each other):

  * "openai" - api.openai.com. Kept as the default `provider` everywhere so
    every pre-existing call site (that predates NVIDIA support) still works
    unchanged.
  * "nvidia" - NVIDIA's OpenAI-compatible NIM endpoint
    (integrate.api.nvidia.com), typically much cheaper per image than OpenAI.

extractor.KeyPool draws from BOTH pools at once and round-robins across every
key from either provider - see extractor.py's module docstring for why that
is safe (never two calls for the same file) and how it keeps the total cost
down without the operator having to babysit which provider is used when.

Storage backends, in the order they are read (per provider):

  1. Environment variable (<PROVIDER>_API_KEYS, comma/semicolon/newline
     separated, or the single-key <PROVIDER>_API_KEY / OPENAI_API_KEY for
     the openai provider specifically, kept for backward compatibility).
     Highest priority, never written by us. This is what you use on a
     server / in CI.
  2. OS credential store via `keyring`: Windows Credential Manager, macOS
     Keychain, or Secret Service on Linux. The OS encrypts it against your
     login. This is the recommended option and what the GUI defaults to.
  3. Local file `<workdir>/credentials.json`, chmod 0600, value obfuscated
     with a machine-derived XOR pad. This is a FALLBACK for machines with no
     keyring (bare Linux boxes, portable installs). It is obfuscation, not
     encryption - the UI says so out loud. Both providers' keys live in this
     one file, under separate keys, so there is still only one file to worry
     about.

Multiple keys can be stored at once per provider (the GUI's "API keys"
dialog supports as many as the operator wants to add, for either provider).
extractor.KeyPool round-robins across all of them - from both providers at
once - and moves a request to the next key when one is rate-limited or out
of quota, so a run does not stall or dead-letter a file just because one key
hit its limit - the point being that with several keys in the pool, a
"Retry failed" click should never be necessary.

No key is ever written to settings.json or passed to the logger.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SERVICE = "share_ocr"
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


@dataclass(frozen=True)
class ProviderInfo:
    label: str                # shown in the GUI
    key_prefixes: tuple       # for the "does this look right" hint, not enforced
    account_keys: str         # keyring account name / credentials.json key for the pool
    account_legacy: str       # legacy single-key keyring account (read-only)
    env_multi: str            # env var: comma/semicolon/newline separated list
    env_single: str           # env var: one key (back-compat / simple setups)
    default_base_url: str     # "" means "use the SDK's own default" (OpenAI)
    default_model: str


PROVIDERS: Dict[str, ProviderInfo] = {
    "openai": ProviderInfo(
        label="OpenAI",
        key_prefixes=("sk-", "sess-"),
        account_keys="openai_api_keys",
        account_legacy="openai_api_key",
        env_multi="OPENAI_API_KEYS",
        env_single="OPENAI_API_KEY",
        default_base_url="",
        default_model="gpt-4o-mini",
    ),
    "nvidia": ProviderInfo(
        label="NVIDIA",
        key_prefixes=("nvapi-",),
        account_keys="nvidia_api_keys",
        account_legacy="nvidia_api_key",
        env_multi="NVIDIA_API_KEYS",
        env_single="NVIDIA_API_KEY",
        default_base_url="https://integrate.api.nvidia.com/v1",
        default_model="meta/llama-3.2-90b-vision-instruct",
    ),
}
DEFAULT_PROVIDER = "openai"


def provider_info(provider: str) -> ProviderInfo:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown provider {provider!r} - expected one of {list(PROVIDERS)}"
        ) from None


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


def _read_legacy_file_key(workdir: Path, info: ProviderInfo) -> Optional[str]:
    """The single-key entry a pre-multi-key install may still have on disk."""
    p = _cred_path(workdir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text("utf-8"))
        blob = data.get(info.account_legacy)
        return _deobfuscate(blob) if blob else None
    except Exception:                                      # noqa: BLE001
        return None


def _read_file_keys(workdir: Path, info: ProviderInfo) -> List[str]:
    """All providers share one credentials.json, each under its own key, so
    there is still only one file on disk to reason about."""
    p = _cred_path(workdir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:                                      # noqa: BLE001
        return []
    blob = data.get(info.account_keys)
    if blob:
        plain = _deobfuscate(blob)
        if plain:
            try:
                parsed = json.loads(plain)
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except Exception:                              # noqa: BLE001
                pass
    legacy = _read_legacy_file_key(workdir, info)
    return [legacy] if legacy and legacy.strip() else []


def _write_file_keys(workdir: Path, info: ProviderInfo, keys: List[str]) -> None:
    p = _cred_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:                                      # noqa: BLE001
        data = {}
    data["_comment"] = ("Obfuscated, machine-bound. Do NOT commit or copy to "
                        "another machine. Prefer the OS credential store.")
    data[info.account_keys] = _obfuscate(json.dumps(keys))
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _lock_down(p)


def _delete_file_keys(workdir: Path, info: ProviderInfo) -> bool:
    """Drop just this provider's entry - the other provider's keys (if any)
    stay in the shared credentials.json."""
    p = _cred_path(workdir)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:                                      # noqa: BLE001
        return False
    changed = False
    for k in (info.account_keys, info.account_legacy):
        if data.pop(k, None) is not None:
            changed = True
    if not changed:
        return False
    remaining = {k: v for k, v in data.items() if k != "_comment"}
    if remaining:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _lock_down(p)
    else:
        try:
            p.unlink()
        except Exception:                                  # noqa: BLE001
            return False
    return True


def _dedupe(keys) -> List[str]:
    seen, out = set(), []
    for k in keys:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


# --------------------------------------------------------- public API -----

def _keys_from_env(settings, info: ProviderInfo) -> List[str]:
    multi = os.environ.get(info.env_multi)
    if multi and multi.strip():
        return _dedupe(_SPLIT_RE.split(multi))
    env_name = info.env_single
    if info.env_single == "OPENAI_API_KEY":
        # honour a custom env var name if the operator set one for openai
        env_name = getattr(settings, "api_key_env", "OPENAI_API_KEY")
    single = os.environ.get(env_name)
    return [single.strip()] if single and single.strip() else []


def _keys_from_keyring(info: ProviderInfo) -> List[str]:
    try:
        import keyring

        raw = keyring.get_password(SERVICE, info.account_keys)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    keys = [str(k).strip() for k in parsed if str(k).strip()]
                    if keys:
                        return keys
            except Exception:                              # noqa: BLE001
                pass
        legacy = keyring.get_password(SERVICE, info.account_legacy)
        return [legacy.strip()] if legacy and legacy.strip() else []
    except Exception:                                      # noqa: BLE001
        return []


def resolve_all(settings, provider: str = DEFAULT_PROVIDER) -> Tuple[List[str], str]:
    """Return (keys, source) for one provider. Precedence: env > keyring > file.

    Every worker draws from the COMBINED pool of every provider's keys via
    extractor.KeyPool (see its module docstring), which rotates across all
    of them and moves a request to the next key rather than failing the file
    when one key is rate-limited or out of quota.
    """
    info = provider_info(provider)
    keys = _keys_from_env(settings, info)
    if keys:
        return keys, SOURCE_ENV

    if keyring_available():
        keys = _keys_from_keyring(info)
        if keys:
            return keys, SOURCE_KEYRING

    keys = _read_file_keys(Path(settings.workdir), info)
    if keys:
        return keys, SOURCE_FILE

    return [], SOURCE_NONE


def list_api_keys(settings, provider: str = DEFAULT_PROVIDER) -> List[str]:
    return resolve_all(settings, provider)[0]


def list_all_providers_keys(settings) -> Dict[str, List[str]]:
    """{"openai": [...], "nvidia": [...]} - what extractor._pool_for combines."""
    return {p: list_api_keys(settings, p) for p in PROVIDERS}


def resolve(settings, provider: str = DEFAULT_PROVIDER) -> Tuple[Optional[str], str]:
    """Return (first key, source) - kept for callers that only need to know
    whether *a* key is available (e.g. the "Extract" button's guard)."""
    keys, source = resolve_all(settings, provider)
    return (keys[0], source) if keys else (None, SOURCE_NONE)


def get_api_key(settings, provider: str = DEFAULT_PROVIDER) -> Optional[str]:
    return resolve(settings, provider)[0]


def save_api_keys(settings, keys: List[str], prefer_keyring: bool = True,
                  provider: str = DEFAULT_PROVIDER) -> str:
    """Persist the whole key list for one provider, replacing whatever was
    stored before for THAT provider only. Returns the source it was written
    to. Raises ValueError if `keys` is empty - use delete_all_api_keys()
    to clear a pool instead."""
    info = provider_info(provider)
    keys = _dedupe(keys)
    if not keys:
        raise ValueError("No keys to save")

    if prefer_keyring and keyring_available():
        try:
            import keyring

            keyring.set_password(SERVICE, info.account_keys, json.dumps(keys))
            try:                                # drop the legacy single entry
                if keyring.get_password(SERVICE, info.account_legacy):
                    keyring.delete_password(SERVICE, info.account_legacy)
            except Exception:                                  # noqa: BLE001
                pass
            # exactly one copy on disk once the keyring holds the pool
            _delete_file_keys(Path(settings.workdir), info)
            return SOURCE_KEYRING
        except Exception:                                      # noqa: BLE001
            pass

    _write_file_keys(Path(settings.workdir), info, keys)
    return SOURCE_FILE


def add_api_key(settings, key: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Add one key to a provider's pool (a no-op if it is already there)."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Empty key")
    current = list_api_keys(settings, provider)
    if key in current:
        return resolve_all(settings, provider)[1]
    return save_api_keys(settings, current + [key], provider=provider)


def remove_api_key(settings, key: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Drop one key from a provider's pool. Returns the resulting source
    (SOURCE_NONE if that was the last key for this provider)."""
    remaining = [k for k in list_api_keys(settings, provider) if k != key]
    if not remaining:
        delete_all_api_keys(settings, provider)
        return SOURCE_NONE
    return save_api_keys(settings, remaining, provider=provider)


def delete_all_api_keys(settings, provider: str = DEFAULT_PROVIDER) -> list:
    """Remove every stored key for one provider from every place we control.
    Returns what was cleared. The environment variable is not ours to unset -
    callers should treat it as still authoritative if it is set."""
    info = provider_info(provider)
    cleared = []
    if keyring_available():
        try:
            import keyring

            for account in (info.account_keys, info.account_legacy):
                if keyring.get_password(SERVICE, account):
                    keyring.delete_password(SERVICE, account)
                    if SOURCE_KEYRING not in cleared:
                        cleared.append(SOURCE_KEYRING)
        except Exception:                                      # noqa: BLE001
            pass
    if _delete_file_keys(Path(settings.workdir), info):
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


def looks_like_key(provider: str, key: str) -> bool:
    key = (key or "").strip()
    prefixes = provider_info(provider).key_prefixes
    return key.startswith(prefixes) and len(key) >= 20


def looks_like_openai_key(key: str) -> bool:
    """Kept for existing call sites; equivalent to looks_like_key('openai', key)."""
    return looks_like_key("openai", key)


def describe(settings, provider: str = DEFAULT_PROVIDER) -> dict:
    """Everything the UI needs to render one provider's key pool status."""
    keys, source = resolve_all(settings, provider)
    info = provider_info(provider)
    return {
        "provider": provider,
        "provider_label": info.label,
        "configured": bool(keys),
        "count": len(keys),
        "keys": keys,                              # in-process only; never logged
        "masked_list": [mask(k) for k in keys],
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "masked": mask(keys[0]) if keys else "—",
        "env_name": (getattr(settings, "api_key_env", "OPENAI_API_KEY")
                    if provider == "openai" else info.env_single),
        "keyring": keyring_available(),
        "keyring_backend": keyring_backend_name(),
        "file_path": str(_cred_path(Path(settings.workdir))),
    }


def test_key(settings, key: Optional[str] = None, timeout: int = 20,
            provider: str = DEFAULT_PROVIDER) -> Tuple[bool, str]:
    """Cheapest possible live check: list models. Returns (ok, message).

    Works for both providers - NVIDIA's NIM endpoint is OpenAI-compatible,
    so the same `openai` SDK call works against it with the right base_url.
    """
    info = provider_info(provider)
    key = (key or get_api_key(settings, provider) or "").strip()
    if not key:
        return False, f"No {info.label} API key configured."
    try:
        from openai import OpenAI
    except Exception:                                      # noqa: BLE001
        return False, "The `openai` package is not installed (pip install openai)."
    try:
        kwargs = {"api_key": key, "timeout": timeout}
        base_url = info.default_base_url
        if provider == "openai" and getattr(settings, "base_url", ""):
            base_url = settings.base_url          # explicit override wins
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        models = client.models.list()
        names = [m.id for m in list(models)[:200]]
        want = (getattr(settings, "model", "") if provider == "openai"
               else getattr(settings, "nvidia_model", ""))
        if want and names and want not in names:
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
