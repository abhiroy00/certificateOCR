"""Tests for the API key pool / error classification in extractor.py, and
the multi-provider (OpenAI + NVIDIA) key storage in secrets.py.

    python -m tests.test_extractor

Covers three bugs:

  1. An out-of-credits key ("insufficient_quota" / credit_balance_exhausted -
     a billing problem that will not fix itself) was cooled down for the
     same 30s as a plain per-minute rate limit, so the pool kept re-trying a
     permanently broken key and burned through a file's max_attempts on
     nothing but repeats of the same billing error.

  2. Mixing two providers in one pool changes what "not a key-related
     error, don't bother trying other keys" should mean: it is still true
     within one provider (same model, same endpoint - the error will just
     repeat), but not across providers (different model, different
     endpoint - a different provider still deserves its own attempt).

  3. Confirmed live against NVIDIA's real API: meta/llama-3.2-11b-vision-instruct
     read a certificate correctly but, despite response_format={"type":
     "json_object"}, answered in prose bullet points instead of JSON - so
     json.loads() on the raw response failed every single time. A stricter
     system+prompt instruction plus a lenient fallback parser
     (_parse_json_response) fixes it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share_ocr.config import Settings                                  # noqa: E402
from share_ocr.extractor import (KeyPool, OpenAIEngine, ProviderKey,    # noqa: E402
                                 _pool_entries, _parse_json_response,
                                 is_quota_exhausted)
from share_ocr import secrets as S                                     # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, detail))


def main() -> int:
    print("\n[1] is_quota_exhausted classifies real OpenAI error text correctly")
    credit_msg = (
        "RateLimitError: Error code: 429 - {'error': {'message': 'You have "
        "no credits remaining. Add credits to continue using the API at "
        "https://platform.openai.com/settings/organization/billing/.', "
        "'type': 'insufficient_quota', 'param': None, "
        "'code': 'credit_balance_exhausted'}}")
    tpm_msg = (
        "RateLimitError: Error code: 429 - {'error': {'message': 'Rate "
        "limit reached for gpt-4o-mini on tokens per min (TPM): Limit "
        "200000, Used 199434, Requested 2299. Please try again in 519ms.', "
        "'type': 'tokens', 'param': None, 'code': 'rate_limit_exceeded'}}")
    check("out-of-credits message is recognised as quota-exhausted",
          is_quota_exhausted(credit_msg))
    check("a plain per-minute rate limit is NOT quota-exhausted",
          not is_quota_exhausted(tpm_msg))
    check("empty/None input is not quota-exhausted",
          not is_quota_exhausted("") and not is_quota_exhausted(None))

    print("\n[2] cooldown durations are distinct - quota gets the long one")
    check("quota-exhausted cooldown is much longer than a rate limit",
          OpenAIEngine.QUOTA_EXHAUSTED_COOLDOWN_S > OpenAIEngine.RATE_LIMIT_COOLDOWN_S * 60,
          (OpenAIEngine.QUOTA_EXHAUSTED_COOLDOWN_S, OpenAIEngine.RATE_LIMIT_COOLDOWN_S))
    check("quota-exhausted cooldown matches the auth-error cooldown "
          "(both permanent-until-fixed problems)",
          OpenAIEngine.QUOTA_EXHAUSTED_COOLDOWN_S == OpenAIEngine.AUTH_ERROR_COOLDOWN_S)

    print("\n[3] KeyPool.order() deprioritises a cooling-down key but still offers it")
    k1 = ProviderKey("openai", "k1", "", "gpt-4o-mini")
    k2 = ProviderKey("openai", "k2", "", "gpt-4o-mini")
    pool = KeyPool([k1, k2])
    pool.cooldown(k1.identity, OpenAIEngine.QUOTA_EXHAUSTED_COOLDOWN_S)
    order = pool.order()
    check("the healthy key is tried first", order[0] is k2, order)
    check("the exhausted key is still offered as a fallback, not dropped",
          k1 in order, order)

    print("\n[4] a briefly-cooled key looks healthy again quickly; a "
          "quota-exhausted one does not")
    pool2 = KeyPool([k1, k2])
    pool2.cooldown(k1.identity, 0.05)                                    # rate-limit-style
    pool2.cooldown(k2.identity, OpenAIEngine.QUOTA_EXHAUSTED_COOLDOWN_S)  # quota, long
    time.sleep(0.1)
    order2 = pool2.order()
    check("the briefly-cooled key is healthy again", order2[0] is k1, order2)
    check("the quota-exhausted key is still parked at the back",
          order2[-1] is k2, order2)

    print("\n[5] the pool mixes OpenAI and NVIDIA keys together")
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_keys_"))
    s = Settings()
    s.workdir = tmp
    s.model = "gpt-4o-mini"
    s.nvidia_model = "meta/llama-3.2-90b-vision-instruct"
    s.ensure_dirs()
    # Force the file fallback instead of the real OS credential store - this
    # machine's actual, real API keys live in the keyring under these same
    # account names, and this test must never read or overwrite them. Also
    # hide any of the equivalent env vars so a real key set for normal use
    # doesn't leak into what is meant to be an isolated test.
    import os
    real_keyring_available = S.keyring_available
    S.keyring_available = lambda: False
    env_names = ["OPENAI_API_KEY", "OPENAI_API_KEYS", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"]
    saved_env = {k: os.environ.pop(k, None) for k in env_names}
    try:
        S.add_api_key(s, "sk-openai-aaaaaaaaaaaaaaaaaaaa", provider="openai")
        S.add_api_key(s, "sk-openai-bbbbbbbbbbbbbbbbbbbb", provider="openai")
        S.add_api_key(s, "nvapi-ccccccccccccccccccccccccc", provider="nvidia")
        entries = _pool_entries(s)
        by_provider = {}
        for e in entries:
            by_provider.setdefault(e.provider, []).append(e)
        check("both OpenAI keys are in the pool", len(by_provider.get("openai", [])) == 2,
              entries)
        check("the NVIDIA key is in the SAME pool", len(by_provider.get("nvidia", [])) == 1,
              entries)
        check("each entry carries the right model for its provider",
              all(e.model == s.model for e in by_provider["openai"])
              and all(e.model == s.nvidia_model for e in by_provider["nvidia"]),
              entries)
        check("each entry carries the right base_url for its provider",
              by_provider["nvidia"][0].base_url == s.nvidia_base_url, entries)

        print("\n[6] the two providers' keys never collide in storage")
        check("openai list is exactly the openai keys",
              sorted(S.list_api_keys(s, "openai")) ==
              sorted(["sk-openai-aaaaaaaaaaaaaaaaaaaa", "sk-openai-bbbbbbbbbbbbbbbbbbbb"]),
              S.list_api_keys(s, "openai"))
        check("nvidia list is exactly the nvidia key",
              S.list_api_keys(s, "nvidia") == ["nvapi-ccccccccccccccccccccccccc"],
              S.list_api_keys(s, "nvidia"))
        S.remove_api_key(s, "sk-openai-aaaaaaaaaaaaaaaaaaaa", provider="openai")
        check("removing an openai key leaves the nvidia key untouched",
              S.list_api_keys(s, "nvidia") == ["nvapi-ccccccccccccccccccccccccc"],
              S.list_api_keys(s, "nvidia"))
        check("removing an openai key leaves the OTHER openai key",
              S.list_api_keys(s, "openai") == ["sk-openai-bbbbbbbbbbbbbbbbbbbb"],
              S.list_api_keys(s, "openai"))

        print("\n[7] key format hints")
        check("an sk- key looks like an OpenAI key", S.looks_like_key("openai", "sk-" + "a" * 20))
        check("an nvapi- key does NOT look like an OpenAI key",
              not S.looks_like_key("openai", "nvapi-" + "a" * 20))
        check("an nvapi- key looks like an NVIDIA key",
              S.looks_like_key("nvidia", "nvapi-" + "a" * 20))

        print("\n[8] one file is never sent to two keys - a failure on the "
              "first provider tried falls through to the other, and a "
              "success stops the loop immediately")
        S.save_api_keys(s, ["sk-openai-zzzzzzzzzzzzzzzzzzzz"],
                        prefer_keyring=False, provider="openai")
        S.save_api_keys(s, ["nvapi-yyyyyyyyyyyyyyyyyyyyyyyyy"],
                        prefer_keyring=False, provider="nvidia")
        from share_ocr.config import FIELDS

        eng = OpenAIEngine(s)
        calls: list = []
        good_payload = json.dumps({f: None for f in FIELDS})

        class _FakeMessage:
            def __init__(self, content):
                self.content = content

        class _FakeChoice:
            def __init__(self, content):
                self.message = _FakeMessage(content)

        class _FakeResponse:
            def __init__(self, content):
                self.choices = [_FakeChoice(content)]

        def _make_client(provider_that_fails: str):
            class _FakeCompletions:
                def create(_self, **kw):                     # noqa: N805
                    calls.append(kw.get("model"))
                    if kw.get("model") == provider_that_fails:
                        raise RuntimeError(
                            "Error code: 400 - not a rate limit, not a "
                            "quota problem, not an auth problem")
                    return _FakeResponse(good_payload)

            class _FakeChat:
                completions = _FakeCompletions()

            class _FakeClient:
                chat = _FakeChat()

            return _FakeClient()

        # _pool_entries() appends every openai key before any nvidia key, so
        # entries[0] is deterministically the openai one. Reset the pool's
        # round-robin pointer to 0 right before the call so THIS call's
        # internal pool.order() is guaranteed to try entries[0] (openai)
        # first, entries[1] (nvidia) second - no reliance on an extra
        # inspection call, which would itself have advanced the pointer.
        eng.pool._idx = 0
        first_model = eng.pool.entries[0].model
        for pk in eng.pool.entries:
            eng._clients[pk.identity] = _make_client(first_model)

        rec = eng._complete([{"role": "user", "content": [
            {"type": "text", "text": "x"}]}])
        check("extraction succeeded via the other provider after the first failed",
              rec is not None and set(rec.keys()) == set(FIELDS), rec)
        check("exactly one call went to the failing model and exactly one "
              "to the succeeding one - not two calls to the same file",
              len(calls) == 2, calls)

        print("\n[9] a timeout falls through AND cools that key down - it is "
              "not retried at full priority on the very next file")
        eng.pool._idx = 0
        timing_out_entry = eng.pool.entries[0]
        calls.clear()

        def _make_timeout_client():
            class _FakeCompletions:
                def create(_self, **kw):                     # noqa: N805
                    calls.append(kw.get("model"))
                    if kw.get("model") == timing_out_entry.model:
                        raise TimeoutError("Request timed out.")
                    return _FakeResponse(good_payload)

            class _FakeChat:
                completions = _FakeCompletions()

            class _FakeClient:
                chat = _FakeChat()

            return _FakeClient()

        for pk in eng.pool.entries:
            eng._clients[pk.identity] = _make_timeout_client()
        rec2 = eng._complete([{"role": "user", "content": [
            {"type": "text", "text": "x"}]}])
        check("extraction still succeeded via the other provider after a timeout",
              rec2 is not None and set(rec2.keys()) == set(FIELDS), rec2)
        cooled_until = eng.pool._cooldown_until.get(timing_out_entry.identity, 0)
        check("the timed-out key was put on cooldown, not just skipped for "
              "this one call - so the NEXT file does not pay the same "
              "timeout again on a key that is currently having a slow moment",
              cooled_until > time.time(), cooled_until)

        print("\n[10] _parse_json_response recovers a model's answer even "
              "when it ignores the JSON-only instruction")
        clean = '{"company_name": "ACME", "certificate_no": "123"}'
        check("a direct, well-formed JSON response parses normally",
              _parse_json_response(clean) == {"company_name": "ACME",
                                              "certificate_no": "123"})
        wrapped = ('Here is the extracted data:\n```json\n'
                  '{"company_name": "ACME", "certificate_no": "123"}\n```')
        check("JSON wrapped in a markdown fence is still recovered",
              _parse_json_response(wrapped) ==
              {"company_name": "ACME", "certificate_no": "123"})
        prose_no_json = ("The image shows a share certificate from ACME "
                         "Ltd.\n* Certificate No.: 123\n* Folio No.: 45")
        raised = False
        try:
            _parse_json_response(prose_no_json)
        except (json.JSONDecodeError, TypeError):
            raised = True
        check("pure prose with no {...} block anywhere still raises "
              "(nothing to silently guess at)", raised)

        print("\n[11] every request tells the model to answer with JSON "
              "only - extract_image() is what actually adds it, not "
              "_complete() itself, so this must go through the real method")
        eng.pool._idx = 0
        seen_messages: list = []

        def _make_capturing_client():
            class _FakeCompletions:
                def create(_self, **kw):                     # noqa: N805
                    seen_messages.append(kw.get("messages"))
                    return _FakeResponse(good_payload)

            class _FakeChat:
                completions = _FakeCompletions()

            class _FakeClient:
                chat = _FakeChat()

            return _FakeClient()

        for pk in eng.pool.entries:
            eng._clients[pk.identity] = _make_capturing_client()

        from PIL import Image
        img_path = tmp / "probe.jpg"
        Image.new("RGB", (40, 40), (255, 255, 255)).save(img_path)
        eng.extract_image(str(img_path))
        check("a system message instructing JSON-only output is sent",
              seen_messages and seen_messages[-1][0].get("role") == "system",
              seen_messages)
        check("the user message also repeats the JSON-only instruction",
              "JSON" in seen_messages[-1][1]["content"][0]["text"].upper(),
              seen_messages)
    finally:
        S.keyring_available = real_keyring_available
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
