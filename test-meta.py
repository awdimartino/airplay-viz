#!/usr/bin/env python3
"""Send fake AirPlay track info to the display via shairport's metadata pipe.

  test-meta.py COVER.jpg "Title" "Artist" "Album"   # start a fake track
  test-meta.py end                                   # end the fake session
"""
import base64
import os
import sys


def item(t, c, d=b""):
    if isinstance(d, str):
        d = d.encode()
    return (f"<item><type>{t.encode().hex()}</type><code>{c.encode().hex()}</code>"
            f"<length>{len(d)}</length>\n<data encoding=\"base64\">\n"
            f"{base64.b64encode(d).decode()}</data></item>\n").encode()


fd = os.open("/tmp/shairport-sync-metadata", os.O_WRONLY)
if sys.argv[1] == "end":
    os.write(fd, item("ssnc", "pend"))
    sys.exit()
art = open(sys.argv[1], "rb").read()
os.write(fd, item("ssnc", "pbeg") + item("core", "minm", sys.argv[2])
         + item("core", "asar", sys.argv[3]) + item("core", "asal", sys.argv[4])
         + item("ssnc", "PICT", art) + item("ssnc", "prgr", "1000/3100000/9300000"))
