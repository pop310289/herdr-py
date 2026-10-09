"""opencode.unasked: which permission kinds a real OpenCode runs without asking, so the policy never sees them
(found against OpenCode 1.18.32: with its defaults a policy-denied curl ran; scripts/check_opencode.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_py.opencode import ASK_CONFIG, ASKED, OpenCodeError, unasked  # noqa: E402
import json  # noqa: E402


class Stub:
    def __init__(self, config=None, error=None):
        self.config, self.error = config, error

    def call(self, method, path, body=None, timeout=None):
        assert (method, path) == ("GET", "/config")
        if self.error:
            raise self.error
        return self.config


class UnaskedTest(unittest.TestCase):
    def test_defaults_ask_about_nothing(self):
        self.assertEqual(unasked(Stub({})), [f"{k}=unset" for k in ASKED])
        self.assertEqual(unasked(Stub(None)), [f"{k}=unset" for k in ASKED])

    def test_the_recommended_config_asks_about_everything(self):
        self.assertEqual(unasked(Stub(json.loads(ASK_CONFIG))), [])

    def test_one_allow_in_a_table_or_a_plain_value_counts(self):
        config = json.loads(ASK_CONFIG)
        config["permission"]["bash"] = {"*": "ask", "ls *": "allow"}
        config["permission"]["edit"] = "allow"
        self.assertEqual(unasked(Stub(config)), ['edit="allow"', 'bash={"*": "ask", "ls *": "allow"}'])
        config["permission"]["bash"] = {"*": "ask"}
        config["permission"]["edit"] = "ask"
        self.assertEqual(unasked(Stub(config)), [])

    def test_a_config_that_cannot_be_read_is_unknown_not_fine(self):
        self.assertIsNone(unasked(Stub(error=OpenCodeError("GET /config -> HTTP 404"))))


if __name__ == "__main__":
    unittest.main()
