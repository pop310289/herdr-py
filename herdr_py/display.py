"""Terminal cell widths (CJK counts as 2) and line building with ANSI colours."""
import unicodedata


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
