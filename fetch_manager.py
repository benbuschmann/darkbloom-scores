#!/usr/bin/env python3
"""Install the exact public manager dependency; never use a floating branch."""

import hashlib
from pathlib import Path
from urllib.request import urlopen

COMMIT = "dd064209f85fd2b701b3ab6b8a6bfff5ead7db27"
SHA256 = "7140b60ce1b2e84a138dfc940fffcaa53f045bb4848976f926ca0424d080ca9c"
URL = f"https://raw.githubusercontent.com/benbuschmann/darkbloom-manager/{COMMIT}/warm_model_manager.py"
TARGET = Path(__file__).with_name("warm_model_manager.py")


def install() -> None:
    if TARGET.exists():
        source = TARGET.read_bytes()
    else:
        with urlopen(URL, timeout=30) as response:
            source = response.read()
    if hashlib.sha256(source).hexdigest() != SHA256:
        raise RuntimeError("Manager checksum mismatch; refusing to install an unverified dependency")
    if not TARGET.exists():
        TARGET.write_bytes(source)
    print(f"Verified public manager dependency at {COMMIT}")


if __name__ == "__main__":
    install()
