#!/usr/bin/env python3
"""用虛擬終端（pty）錄下一個程式的終端輸出，存成 asciicast v2（asciinema 的格式）。

只錄這個程式送到終端的字元與時間，不擷取螢幕，所以不會錄到其他視窗。終端大小固定（--cols × --rows）。

usage: rec.py --cols 66 --rows 44 --out demo.cast [--title T] -- COMMAND ...
"""
import argparse
import codecs
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, default=66)
    ap.add_argument("--rows", type=int, default=44)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", a.rows, a.cols, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(a.cols), LINES=str(a.rows))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")  # 一個中文字可能被切在兩次 read 之間
    t0 = time.time()
    with open(a.out, "w", encoding="utf-8") as out:
        out.write(json.dumps({"version": 2, "width": a.cols, "height": a.rows, "timestamp": int(t0), "title": a.title,
                              "env": {"TERM": "xterm-256color"}}, ensure_ascii=False) + "\n")
        while True:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:  # 程式結束後 pty 關閉（macOS 回 EIO）
                    break
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    out.write(json.dumps([round(time.time() - t0, 4), "o", text], ensure_ascii=False) + "\n")
            elif proc.poll() is not None:
                break
    proc.wait()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
