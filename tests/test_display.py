"""Terminal text (herdr_py/display.py): what agents, judges and web pages wrote reaches the terminal without control
characters, which the terminal would obey (set the clipboard, the title, clear the screen), and without bidirectional
overrides, which reorder what follows on screen."""
import os
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from herdr_py import display  # noqa: E402

EVIL = "ok\x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2J\u202etxt.exe\x9b31m\x00\x7f fake\rreal\u2066x\u2069"


def unsafe(text):
    return [hex(ord(c)) for c in text if c in display.BIDI or (unicodedata.category(c) == "Cc" and c not in "\n\t")]


class TerminalTextTest(unittest.TestCase):
    def test_control_characters_never_reach_the_terminal(self):
        for name, out in (("clip", display.clip(EVIL, 500)[0]), ("tail", display.tail(EVIL, 500)),
                          ("printable", display.printable(EVIL)), ("row", display.row([(EVIL, "")], 500))):
            self.assertEqual(unsafe(out), [], name)
            self.assertIn("ZWNobyBwd25lZA==", out, name)  # the visible text stays, only the control characters go
        self.assertEqual(display.printable("a\nb\x1bc\td\re"), "a\nb�c\td�e")
        self.assertEqual(display.clip("\x1b[2J", 3), ("�[2", 3))  # a replacement takes one cell, like any letter

    def test_a_styled_row_keeps_its_own_colours_only(self):
        out = display.row([("hi\x1b[31m", "1;32")], 10)
        self.assertEqual(out, "\x1b[1;32mhi�[31m\x1b[0m" + " " * 3)

    def test_ordinary_text_is_unchanged(self):
        for text in ("plain words", "中文字與全形，標點", "emoji 👍🏽", "zero‍width joiner", "tab\there", "Ελληνικά"):
            self.assertEqual(display.printable(text), text)
        self.assertEqual(display.clip("中文字", 4), ("中文", 4))


if __name__ == "__main__":
    unittest.main()
