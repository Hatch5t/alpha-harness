"""Keep the package's offline mathematical/template self-checks inside the CI gate."""

import subprocess
import sys
import unittest


class ModuleChecks(unittest.TestCase):
    def test_offline_self_checks(self) -> None:
        for module in [
            "labs.template",
            "labs.super_alpha",
            "labs.truncation",
            "tools.field_buckets",
            "tools.fields_sampler",
        ]:
            with self.subTest(module=module):
                result = subprocess.run(  # noqa: S603 — fixed local module allowlist, no user input
                    [sys.executable, "-m", "alpha_harness." + module],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:]
                )
