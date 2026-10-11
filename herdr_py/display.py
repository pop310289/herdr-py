"""Terminal cell widths (CJK counts as 2) and line building with ANSI colours.

Text from agents, judges and web pages reaches the terminal only through shown() or printable(): a control character
(ESC starts commands the terminal obeys: set the clipboard, the title, move the cursor) or a bidirectional override
(it reorders what follows on screen) becomes U+FFFD."""
import unicodedata

BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
REPLACEMENT = "\ufffd"


def shown(ch):
    """One character as the terminal may get it."""
    return REPLACEMENT if ch in BIDI or unicodedata.category(ch) == "Cc" else ch


def printable(text):
    """Text for print(): line breaks and tabs stay; every other control character (a carriage return would let the
    rest of a line write over its start) and every bidirectional override is replaced."""
    return "\n".join("".join(ch if ch == "\t" else shown(ch) for ch in line) for line in str(text).split("\n"))


def width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text):
    return sum(width(c) for c in text)


def clip(text, cols):
    """Cut text to at most `cols` cells. Returns (text, cells used)."""
    out, used = [], 0
    for ch in str(text).replace("\n", " ").replace("\t", " "):
        ch = shown(ch)
        w = width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out), used


def tail(text, cols):
    """The last part of text that fits in `cols` cells."""
    out, used = [], 0
    for ch in reversed(" ".join(str(text).split())):
        ch = shown(ch)
        w = width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(reversed(out))


def row(segments, cols):
    """segments: [(text, SGR codes)] -> one line exactly `cols` cells wide."""
    out, used = [], 0
    for text, style in segments:
        piece, w = clip(text, cols - used)
        if piece:
            out.append(f"\x1b[{style}m{piece}\x1b[0m" if style else piece)
            used += w
    return "".join(out) + " " * (cols - used)
