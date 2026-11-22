"Release hashes are selected from the exact wheel asset, never a neighboring installer."

import unittest

from alpha_harness.updates import _digest_of


class ReleaseIntegrityTests(unittest.TestCase):
    def test_only_matching_wheel_digest_is_accepted(self) -> None:
        digest = "sha256:" + "a" * 64
        wheel = {
            "name": "alpha_harness-2026.11.22rc4-py3-none-any.whl",
            "browser_download_url": (
                "https://github.com/Hatch5t/alpha-"
                "harness/releases/download/v2026.11.22rc4/alpha_harness.whl"
            ),
            "digest": digest,
        }
        self.assertEqual(
            _digest_of([{"name": "installer.dmg", "digest": "sha256:" + "b" * 64}, wheel]), digest
        )
        self.assertIsNone(_digest_of([wheel | {"digest": "sha256:invalid"}]))
        self.assertIsNone(_digest_of([wheel | {"digest": None}]))
        self.assertIsNone(_digest_of(None))
