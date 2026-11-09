import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.secure_printers import restrict_trusted  # noqa: E402

CONF = """[server]
host: 0.0.0.0

[authorization]
cors_domains:
    https://my.mainsail.xyz
trusted_clients:
    10.0.0.0/8
    127.0.0.0/8
    192.168.0.0/16
    FE80::/10

[octoprint_compat]
"""


class RestrictTrustedTests(unittest.TestCase):
    def test_only_the_printer_itself_stays_trusted(self):
        new = restrict_trusted(CONF)
        self.assertIn("trusted_clients:\n    127.0.0.0/8\n    ::1/128\n\n[octoprint_compat]", new)
        self.assertNotIn("192.168.0.0/16", new)
        self.assertIn("    https://my.mainsail.xyz", new)  # other settings untouched
        self.assertEqual(restrict_trusted(new), new)       # running twice changes nothing

    def test_adds_the_setting_when_missing(self):
        new = restrict_trusted("[authorization]\nforce_logins: False\n")
        self.assertEqual(new, "[authorization]\ntrusted_clients:\n    127.0.0.0/8\n    ::1/128\nforce_logins: False\n")

    def test_no_authorization_section(self):
        self.assertIsNone(restrict_trusted("[server]\nhost: 0.0.0.0\n"))


if __name__ == "__main__":
    unittest.main()
