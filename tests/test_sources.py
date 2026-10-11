"""Every Python file of the repository compiles with warnings as errors, on the Python running the tests (CI: 3.6 and
the newest): the examples, benches, scripts and tools too, which no other test imports. An invalid escape such as
"\\ " in a string is a warning today (a DeprecationWarning on 3.6, a SyntaxWarning on 3.12) and an error in a later
Python. And no text file of the repository holds a bidirectional control character as such (it can make code read
differently from how it runs, "Trojan Source"); a test that needs one writes it as an escape, \\u202e."""
import os
import unittest
import warnings

BIDI = frozenset(chr(c) for c in (0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069))
TEXT = (".py", ".sh", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".html", ".js", ".css", ".service")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sources(ends=(".py",)):
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for name in sorted(files):
            if name.endswith(ends):
                yield os.path.join(folder, name)


class SourcesTest(unittest.TestCase):
    def test_every_python_file_compiles_without_warnings(self):
        bad, seen = [], 0
        for path in sources():
            seen += 1
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                try:
                    compile(text, path, "exec", dont_inherit=True)
                except (SyntaxError, SyntaxWarning, DeprecationWarning) as exc:
                    bad.append("%s: %s" % (os.path.relpath(path, ROOT), exc))
        self.assertGreater(seen, 100)  # the walk found the repository, not an empty folder
        self.assertEqual(bad, [])

    def test_no_bidirectional_control_characters(self):
        found, seen = [], 0
        for path in sources(TEXT):
            seen += 1
            with open(path, encoding="utf-8", errors="replace") as handle:
                for n, line in enumerate(handle, 1):
                    if BIDI.intersection(line):
                        found.append("%s:%d %s" % (os.path.relpath(path, ROOT), n, sorted("U+%04X" % ord(c) for c in BIDI.intersection(line))))
        self.assertGreater(seen, 150)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
