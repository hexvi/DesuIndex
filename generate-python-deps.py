#!/usr/bin/env python3
"""Generate python3-requirements.json for the Flatpak build.

flatpak-builder has no network access while building, so every Python package
must be listed up front as a pinned, checksummed source. This resolves
requirements-flatpak.txt with the pip inside the manifest's SDK (so the Python
version always matches the runtime), once per target architecture, and writes
a module that installs the resulting wheels offline.

Only wheels are used: onnxruntime ships no sdist, and building numpy from
source inside the sandbox is slow and fragile.

Re-run this whenever requirements-flatpak.txt or the runtime version changes.
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "io.github.hexvi.DesuIndex.json"
REQUIREMENTS = HERE / "requirements-flatpak.txt"
OUTPUT = HERE / "python3-requirements.json"

ARCHES = ["x86_64", "aarch64"]
# The GNOME runtime's glibc is far newer than any manylinux baseline in use.
MANYLINUX_TAGS = ["manylinux2014"] + [f"manylinux_2_{n}" for n in range(17, 40)]


def resolve(sdk: str, arch: str) -> list[dict]:
    platform_args = [f"--platform={tag}_{arch}" for tag in MANYLINUX_TAGS]
    cmd = [
        "flatpak", "run", "--share=network", "--command=python3", sdk,
        "-m", "pip", "install",
        "--dry-run", "--quiet", "--ignore-installed", "--report=-",
        "--only-binary=:all:", *platform_args,
        "--requirement=/dev/stdin",
    ]
    proc = subprocess.run(
        cmd, input=REQUIREMENTS.read_text(), capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.exit(f"pip failed resolving for {arch}:\n{proc.stderr}")
    return json.loads(proc.stdout)["install"]


def main():
    manifest = json.loads(MANIFEST.read_text())
    sdk = f"{manifest['sdk']}//{manifest['runtime-version']}"
    # pip runs inside the SDK so the Python version matches the runtime.
    subprocess.run(
        ["flatpak", "install", "--user", "--noninteractive", "--or-update", "flathub", sdk],
        check=True,
    )

    # url -> {"sha256": ..., "arches": set(...)}
    wheels: dict[str, dict] = {}
    for arch in ARCHES:
        print(f">>> Resolving for {arch} with {sdk} …")
        for item in resolve(sdk, arch):
            meta = item["metadata"]
            info = item["download_info"]
            url = info["url"]
            sha256 = info["archive_info"]["hashes"]["sha256"]
            print(f"    {meta['name']}=={meta['version']}")
            entry = wheels.setdefault(url, {"sha256": sha256, "arches": set()})
            entry["arches"].add(arch)

    sources = []
    for url in sorted(wheels, key=lambda u: u.rsplit("/", 1)[-1].lower()):
        source = {"type": "file", "url": url, "sha256": wheels[url]["sha256"]}
        if wheels[url]["arches"] != set(ARCHES):
            source["only-arches"] = sorted(wheels[url]["arches"])
        sources.append(source)

    module = {
        "name": "python3-requirements",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --no-index --no-deps --ignore-installed"
            " --prefix=${FLATPAK_DEST} *.whl"
        ],
        # Drop pip-generated console scripts (hf, f2py, isympy, …).
        "cleanup": ["/bin"],
        "sources": sources,
    }
    OUTPUT.write_text(json.dumps(module, indent=4) + "\n")
    print(f">>> Wrote {OUTPUT.name} ({len(sources)} wheels)")


if __name__ == "__main__":
    main()
