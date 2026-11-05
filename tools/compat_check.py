#!/usr/bin/env python3
"""Check that OmonFlow's runtime and packaging dependencies publish wheels
for CPython 3.13 on every target platform (SPEC §2.1 / PLAN M02 spike 3).

Reads PyPI metadata only. Usage: python3 tools/compat_check.py
"""

import json
import re
import urllib.request

TARGETS = {
    "macOS arm64": lambda t: "macosx" in t and ("arm64" in t or "universal2" in t),
    "macOS x86_64": lambda t: "macosx" in t and ("x86_64" in t or "universal2" in t or "intel" in t),
    "Windows x64": lambda t: "win_amd64" in t,
    "Linux x86_64": lambda t: ("manylinux" in t or "linux" in t) and "x86_64" in t,
}
PACKAGES = {
    # name: (version or None for latest, platforms needed)
    "PySide6-Essentials": ("6.11.3", list(TARGETS)),
    "shiboken6": ("6.11.3", list(TARGETS)),
    "platformdirs": (None, list(TARGETS)),
    "pyobjc-core": (None, ["macOS arm64", "macOS x86_64"]),
    "pyobjc-framework-Cocoa": (None, ["macOS arm64", "macOS x86_64"]),
    "pyobjc-framework-Quartz": (None, ["macOS arm64", "macOS x86_64"]),
    "pyinstaller": (None, list(TARGETS)),
    "maturin": (None, list(TARGETS)),
}


def py_ok(tags: str) -> bool:
    return bool(re.search(r"(cp313|abi3|py3-none|py2\.py3-none)", tags))


def main():
    rows, ok_all = [], True
    for name, (version, needed) in PACKAGES.items():
        url = f"https://pypi.org/pypi/{name}/{version}/json" if version else f"https://pypi.org/pypi/{name}/json"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        ver = data["info"]["version"]
        files = [f["filename"] for f in data["urls"] if f["packagetype"] == "bdist_wheel"]
        result = {}
        for target in needed:
            hits = [f for f in files if py_ok(f) and (TARGETS[target](f) or "none-any" in f)]
            result[target] = bool(hits)
            ok_all &= bool(hits)
        rows.append((name, ver, result))
    print("| Package | Version | " + " | ".join(TARGETS) + " |")
    print("|---|---|" + "---|" * len(TARGETS))
    for name, ver, result in rows:
        cells = ["yes" if result.get(t) else ("NO" if t in result else "n/a") for t in TARGETS]
        print(f"| {name} | {ver} | " + " | ".join(cells) + " |")
    print("\nAll required wheels available for CPython 3.13:", "YES" if ok_all else "NO")


if __name__ == "__main__":
    main()
