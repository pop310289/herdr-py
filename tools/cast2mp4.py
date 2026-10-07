#!/usr/bin/env python3
"""把 asciicast v2 錄影轉成 MP4。

用最小的終端模擬重建每一格畫面（游標定位、清除、SGR 顏色、中文字佔 2 格、自動換行與捲動），
用 Pillow 畫字、ffmpeg（libx264）編碼。每個字元先查字型的 cmap：Menlo 有就用 Menlo，否則依序用
蘋方繁中、Arial Unicode，避免缺字變成方塊。

usage: uv run --with pillow --with fonttools python3 cast2mp4.py IN.cast OUT.mp4 [--speed 4] [--fps 10]
       [--note "說明列"] [--png-at 秒數 ...]（另存指定實際時間的畫面，給人檢查用）
       [--overlay manifest.jsonl [--reference ref.png]]  在終端畫面下方放「當時最新的圖」（manifest 每行 {"t": 絕對時間, "path"}），
       右下角放參考圖的小圖
"""
import argparse
import json
import os
import re
import subprocess
import unicodedata

from fontTools.ttLib import TTCollection, TTFont
from PIL import Image, ImageDraw, ImageFont

MENLO = "/System/Library/Fonts/Menlo.ttc"
PINGFANG = "/System/Library/AssetsV2/com_apple_MobileAsset_Font7/3419f2a427639ad8c8e139149a287865a90fa17e.asset/AssetData/PingFang.ttc"
ARIAL_UNI = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
BG = (20, 22, 27)
FG = (215, 218, 224)
PALETTE = [(29, 31, 35), (224, 108, 117), (152, 195, 121), (229, 192, 123), (59, 116, 196), (198, 120, 221), (86, 182, 194), (220, 223, 228),
           (127, 132, 142), (255, 123, 134), (181, 232, 144), (240, 209, 151), (130, 196, 255), (215, 155, 234), (127, 209, 219), (255, 255, 255)]
CSI = re.compile(r"\x1b\[([?0-9;]*)([@-~])")


def width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.blank = (" ", None, None, False, False)
        self.grid = [[self.blank] * cols for _ in range(rows)]
        self.x = self.y = 0
        self.fg = self.bg = None
        self.bold = self.dim = self.reverse = False
        self.pending = ""

    def cell(self, ch):
        fg, bg = (self.bg or 0, self.fg if self.fg is not None else -1) if self.reverse else (self.fg, self.bg)
        return (ch, fg, bg, self.bold, self.dim)

    def newline(self):
        if self.y == self.rows - 1:
            self.grid = self.grid[1:] + [[self.blank] * self.cols]
        else:
            self.y += 1

    def feed(self, text):
        text, self.pending = self.pending + text, ""
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                m = CSI.match(text, i)
                if m is None:
                    if len(text) - i < 32 and (i + 1 == len(text) or text[i + 1] == "["):
                        self.pending = text[i:]  # 控制序列被切在兩段輸出之間
                        return
                    i += 2
                    continue
                self.csi(m.group(1), m.group(2))
                i = m.end()
                continue
            if ch == "\r":
                self.x = 0
            elif ch == "\n":
                self.newline()
            elif ch == "\b":
                self.x = max(0, self.x - 1)
            elif ch >= " ":
                w = width(ch)
                if w:
                    if self.x + w > self.cols:
                        self.x = 0
                        self.newline()
                    self.grid[self.y][self.x] = self.cell(ch)
                    if w == 2:
                        self.grid[self.y][self.x + 1] = self.cell("")  # 中文字的第二格
                    self.x += w
            i += 1

    def csi(self, params, final):
        if params.startswith("?"):
            return  # 顯示／隱藏游標之類的模式切換，不影響畫面
        nums = [int(n) if n else 0 for n in params.split(";")] if params else []
        if final in "Hf":
            r, c = (nums + [1, 1])[:2]
            self.y, self.x = min(max(r, 1), self.rows) - 1, min(max(c, 1), self.cols) - 1
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode == 2:
                self.grid = [[self.blank] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                self.grid[self.y][self.x:] = [self.blank] * (self.cols - self.x)
                for r in range(self.y + 1, self.rows):
                    self.grid[r] = [self.blank] * self.cols
        elif final == "K":
            self.grid[self.y][self.x:] = [self.blank] * (self.cols - self.x)
        elif final == "m":
            for n in nums or [0]:
                if n == 0:
                    self.fg = self.bg = None
                    self.bold = self.dim = self.reverse = False
                elif n == 1:
                    self.bold = True
                elif n == 2:
                    self.dim = True
                elif n == 22:
                    self.bold = self.dim = False
                elif n == 7:
                    self.reverse = True
                elif n == 27:
                    self.reverse = False
                elif 30 <= n <= 37:
                    self.fg = n - 30
                elif n == 39:
                    self.fg = None
                elif 40 <= n <= 47:
                    self.bg = n - 40
                elif n == 49:
                    self.bg = None
                elif 90 <= n <= 97:
                    self.fg = n - 90 + 8
                elif 100 <= n <= 107:
                    self.bg = n - 100 + 8


class Painter:
    def __init__(self, cols, rows, size, note, panel=0):
        self.cols, self.rows = cols, rows
        self.menlo = {b: ImageFont.truetype(MENLO, size, index=1 if b else 0) for b in (False, True)}
        self.cjk = {b: ImageFont.truetype(PINGFANG, size + 1, index=10 if b else 2) for b in (False, True)}
        self.sym = ImageFont.truetype(ARIAL_UNI, size - 2)
        self.menlo_cmap = set(TTCollection(MENLO).fonts[0]["cmap"].getBestCmap())
        self.cjk_cmap = set(TTCollection(PINGFANG).fonts[2]["cmap"].getBestCmap())
        self.cw = round(self.menlo[False].getlength("M"))
        self.ch = round(size * 1.24)
        self.base = round(size * 0.95)
        self.note = note
        self.note_font = ImageFont.truetype(PINGFANG, round(size * 0.82), index=2)
        self.note_h = round(size * 0.82 * 1.5) * len(note) + 24 if note else 0
        self.panel = panel
        self.side = False
        self.w = self.cw * cols + 24
        self.h = self.ch * rows + 24 + self.note_h + panel
        self.w += self.w % 2
        self.h += self.h % 2
        self.missing = set()

    def color(self, idx, default, dim):
        c = default if idx is None or idx < 0 else PALETTE[idx]
        return tuple(round(v * 0.55 + b * 0.45) for v, b in zip(c, BG)) if dim else c

    def font_for(self, ch, bold):
        cp = ord(ch)
        if cp in self.menlo_cmap:
            return self.menlo[bold], 0
        if cp in self.cjk_cmap:
            return self.cjk[bold], 1
        self.missing.add(ch)
        return self.sym, 1

    def paint(self, screen, footer, picture=None, reference=None, caption=""):
        img = Image.new("RGB", (self.w, self.h), BG)
        if self.panel:
            top = self.ch * self.rows + 24
            d0 = ImageDraw.Draw(img)
            d0.rectangle([0, top, self.w, top + self.panel], fill=(10, 12, 16))
            if self.side and reference is not None:  # tall pictures: ours on the left, the original on the right
                half = (self.w - 36) // 2
                for k, im in enumerate((picture, reference)):
                    if im is not None:
                        pic = im.copy()
                        pic.thumbnail((half, self.panel - 56))
                        x = 12 + k * (half + 12) + (half - pic.width) // 2
                        if k:
                            d0.rectangle([x - 3, top + 41, x + pic.width + 2, top + 44 + pic.height + 2], outline=(240, 200, 80), width=3)
                        img.paste(pic, (x, top + 44))
                reference = picture = None
            if picture is not None:
                pic = picture.copy()
                pic.thumbnail((self.w - 24, self.panel - 56))
                img.paste(pic, ((self.w - pic.width) // 2, top + 44))
            if reference is not None:
                ref = reference.copy()
                ref.thumbnail((self.w // 4, self.panel // 2))
                x, y = self.w - ref.width - 16, top + self.panel - ref.height - 12
                d0.rectangle([x - 3, y - 3, x + ref.width + 2, y + ref.height + 2], outline=(240, 200, 80), width=3)
                img.paste(ref, (x, y))
            d0.text((14, top + 8), caption or "waiting for the first render", font=self.note_font, fill=(200, 206, 214))
        d = ImageDraw.Draw(img)
        for r, line in enumerate(screen.grid):
            y = 12 + r * self.ch
            for c, (ch, fg, bg, bold, dim) in enumerate(line):  # 先畫整行的底色，再畫字；否則中文字的第二格底色會蓋掉字的右半邊
                if bg is not None:
                    x = 12 + c * self.cw
                    d.rectangle([x, y, x + self.cw - 1, y + self.ch - 1], fill=PALETTE[bg])
            for c, (ch, fg, bg, bold, dim) in enumerate(line):
                x = 12 + c * self.cw
                wide = ch != "" and width(ch) == 2
                if ch in ("", " "):
                    continue
                font, centered = self.font_for(ch, bold)
                cells = 2 if wide else 1
                dx = (self.cw * cells - font.getlength(ch)) / 2 if centered else 0
                d.text((x + dx, y + self.base), ch, font=font, fill=self.color(fg, FG, dim), anchor="ls")
        if self.note:
            y = self.ch * self.rows + 24 + self.panel
            d.rectangle([0, y - 6, self.w, self.h], fill=(32, 35, 42))
            for i, text in enumerate([footer] + self.note[1:]):
                d.text((14, y + 6 + i * round(self.note_font.size * 1.5)), text, font=self.note_font, fill=(170, 176, 186))
        return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cast")
    ap.add_argument("out")
    ap.add_argument("--speed", type=float, default=4.0)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--size", type=int, default=26)
    ap.add_argument("--note", action="append", default=[], help="說明列；第一行可用 {speed}、{real} 代入")
    ap.add_argument("--png-at", type=float, nargs="*", default=[])
    ap.add_argument("--overlay", help="JSON lines {t, path[, round]}: the picture shown below the terminal from time t on")
    ap.add_argument("--reference", help="small reference picture in the corner of the overlay panel")
    ap.add_argument("--panel", type=int, default=660, help="height of the overlay panel in pixels")
    ap.add_argument("--side-by-side", action="store_true", help="ours on the left, the original on the right (for tall pictures)")
    a = ap.parse_args()
    lines = open(a.cast, encoding="utf-8").read().splitlines()
    head = json.loads(lines[0])
    events = [json.loads(l) for l in lines[1:] if l.strip()]
    real = events[-1][0] if events else 0
    screen = Screen(head["width"], head["height"])
    overlays = []
    if a.overlay and os.path.exists(a.overlay):
        overlays = [json.loads(l) for l in open(a.overlay, encoding="utf-8") if l.strip()]
    start = head.get("timestamp", 0)
    reference = Image.open(a.reference).convert("RGB") if a.reference else None
    pictures = {}
    painter = Painter(head["width"], head["height"], a.size, a.note, panel=a.panel if a.overlay else 0)
    painter.side = a.side_by_side
    footer = a.note[0].format(speed=f"{a.speed:g}", real=f"{int(real) // 60:02d}:{int(real) % 60:02d}") if a.note else ""
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{painter.w}x{painter.h}",
                           "-r", str(a.fps), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
                           "-movflags", "+faststart", a.out], stdin=subprocess.PIPE)
    frames = int(real / a.speed * a.fps) + 1 + 3 * a.fps  # 最後一格多停 3 秒
    shots = sorted(a.png_at)
    i, last_key, img, last_bytes = 0, None, None, None
    for k in range(frames):
        t = k * a.speed / a.fps
        while i < len(events) and events[i][0] <= t:
            screen.feed(events[i][2])
            i += 1
        shown = [o for o in overlays if o["t"] - start <= t]
        current = shown[-1] if shown else None
        key = str(screen.grid) + (current["path"] if current else "")
        if key != last_key:  # 畫面沒變就沿用上一格
            picture = None
            if current:
                if current["path"] not in pictures:
                    pictures[current["path"]] = Image.open(current["path"]).convert("RGB")
                picture = pictures[current["path"]]
            caption = ((f"left: ours after round {current.get('round')}   right (yellow frame): the original" if a.side_by_side else
                        f"our slide after round {current.get('round')}   (yellow frame: the original)") if current else "")
            img = painter.paint(screen, footer, picture, reference if a.overlay else None, caption)
            last_key, last_bytes = key, img.tobytes()
        while shots and shots[0] <= t:  # 不論畫面有沒有變，到了指定時間就存圖
            img.save(f"{a.out[:-4]}_{shots.pop(0):05.1f}s.png")
        ff.stdin.write(last_bytes)
    ff.stdin.close()
    ff.wait()
    print(json.dumps({"frames": frames, "seconds": round(frames / a.fps, 1), "real_s": round(real, 1), "size": [painter.w, painter.h],
                      "glyphs_from_fallback": "".join(sorted(painter.missing))}, ensure_ascii=False))
    return ff.returncode


if __name__ == "__main__":
    raise SystemExit(main())
