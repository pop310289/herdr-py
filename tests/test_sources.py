"""Every Python file of the repository compiles with warnings as errors, on the Python running the tests (CI: 3.6 and
the newest): the examples, benches, scripts and tools too, which no other test imports. An invalid escape such as
"\\ " in a string is a warning today (a DeprecationWarning on 3.6, a SyntaxWarning on 3.12) and an error in a later
Python."""
import os
import unittest
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sources():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
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


if __name__ == "__main__":
    unittest.main()
