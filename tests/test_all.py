"""Run every test suite.  python -m tests.test_all

Each suite runs in its own subprocess because the GUI suite replaces the
real tkinter module with a fake one, and we do not want that leaking into
the pipeline tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    ("pipeline / queue / CSV", "tests.test_pipeline"),
    ("GUI (headless)", "tests.test_gui"),
    ("golden sample certificates", "tests.test_golden"),
    ("OTP access gate logic", "tests.test_otp_auth"),
    ("OTP sign-in screen (headless)", "tests.test_login_gate"),
    ("API key pool / error classification", "tests.test_extractor"),
]


def main() -> int:
    failed = []
    for title, module in SUITES:
        print("=" * 66)
        print("RUN  %s   (%s)" % (title, module))
        print("=" * 66)
        rc = subprocess.call([sys.executable, "-m", module], cwd=str(ROOT))
        if rc != 0:
            failed.append(title)
        print("")

    print("=" * 66)
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("ALL SUITES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
