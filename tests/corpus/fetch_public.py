"""Fetch pinned, public C2PA test images, or verify an existing offline cache.

Normal corpus generation never fetches network resources. Run this script
explicitly once to populate tests/corpus/generated/external; --verify checks the
cache offline. --out changes the cache directory (useful for offline tests).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMMIT = "0e92435c6d621c02c9bde2a8d82346295ed1f509"
BASE = f"https://raw.githubusercontent.com/contentauth/c2pa-rs/{COMMIT}/sdk/tests/fixtures/"
SOURCES = {
    "C.jpg": {"url": BASE + "C.jpg", "size": 132518,
              "sha256": "a2d14755db55de67a47c04090340d8266e892367be4104a45626d7a6fa6e9ffd"},
    "CA.jpg": {"url": BASE + "CA.jpg", "size": 166864,
               "sha256": "e71bff58fc57640803e6e65f7534e2fb0c2f99018c85276cc14b30f04427cc76"},
}
EXTERNAL_DIR = Path(__file__).parent / "generated" / "external"


def verify_bytes(name: str, data: bytes) -> bytes:
    """Require the independent published size and SHA-256 before using bytes."""
    source = SOURCES[name]
    if len(data) != source["size"]:
        raise ValueError(f"size mismatch for {name}")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {name}")
    return data


def read_cached(directory: Path, name: str) -> bytes | None:
    """Absent is optional; malformed cached content is always an error."""
    path = directory / name
    if path.is_symlink():
        raise ValueError(f"public fixture must not be a symlink: {name}")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size != SOURCES[name]["size"]:
        raise ValueError(f"size mismatch for {name}")
    with path.open("rb") as stream:
        return verify_bytes(name, stream.read(SOURCES[name]["size"] + 1))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Only the two explicitly authorized raw URLs may be requested.
        return None


def fetch(directory: Path, *, verify_only: bool) -> None:
    cached = {name: read_cached(directory, name) for name in SOURCES}
    if verify_only:
        for name, data in cached.items():
            if data is None:
                raise ValueError(f"missing public fixture: {name}")
    else:
        opener = urllib.request.build_opener(_NoRedirect)
        for name, data in cached.items():
            if data is None:
                source = SOURCES[name]
                request = urllib.request.Request(source["url"], headers={"User-Agent": "doc-sentinel-corpus"})
                with opener.open(request, timeout=60) as response:
                    if response.geturl() != source["url"]:
                        raise ValueError(f"unexpected download URL for {name}")
                    cached[name] = verify_bytes(name, response.read(source["size"] + 1))
        # Verify the entire set before writing anything, and do not replace
        # existing files. Race-safe exclusive creation avoids overwrites.
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in cached.items():
            path = directory / name
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(data)
    for name in SOURCES:
        print(f"verified {name}: {SOURCES[name]['sha256']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=EXTERNAL_DIR)
    parser.add_argument("--verify", action="store_true", help="verify existing files without network")
    args = parser.parse_args()
    try:
        fetch(args.out, verify_only=args.verify)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
