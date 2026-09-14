#!/usr/bin/env python3
"""Verify every published file and metadata contract in the HND APT feed."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEBS = ROOT / "debs"
PACKAGES = ROOT / "Packages"
PACKAGES_GZ = ROOT / "Packages.gz"
PACKAGES_BZ2 = ROOT / "Packages.bz2"
RELEASE = ROOT / "Release"


def run(command: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def parse_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if current is not None:
                fields[current] += "\n" + line
            continue
        name, separator, value = line.partition(":")
        if separator:
            current = name
            fields[current] = value.lstrip()
    return fields


def paragraphs(raw: str) -> list[str]:
    return [part for part in raw.strip().split("\n\n") if part.strip()]


def digest(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fail(message: str) -> None:
    raise RuntimeError(message)


def verify_no_forbidden_artifacts() -> None:
    forbidden_suffixes = (".ipa", ".zip", ".tar", ".tar.gz", ".tgz", ".sha256", ".log")
    forbidden_markers = ("source_manifest", "deriveddata", "credentials", "secret", "token")
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(ROOT)
        if relative.parts and relative.parts[0] == "scripts":
            continue
        lower = path.name.lower()
        if lower.endswith(forbidden_suffixes) or any(marker in lower for marker in forbidden_markers):
            fail(f"forbidden public artifact: {relative}")
    packages_body = PACKAGES.read_text(encoding="utf-8")
    if ".ipa" in packages_body or "source-v" in packages_body or "SOURCE_MANIFEST" in packages_body:
        fail("Packages contains a forbidden IPA/source reference")


def verify_packages() -> tuple[list[dict[str, str]], set[Path]]:
    if not PACKAGES.is_file():
        fail("Packages is missing")
    stanzas = [parse_fields(part) for part in paragraphs(PACKAGES.read_text(encoding="utf-8"))]
    seen: set[tuple[str, str, str]] = set()
    referenced: set[Path] = set()
    for stanza in stanzas:
        required = ("Package", "Version", "Architecture", "Description", "Filename", "Size", "SHA256")
        for field in required:
            if field not in stanza or not stanza[field]:
                fail(f"Packages stanza missing {field}")
        identity = (stanza["Package"], stanza["Version"], stanza["Architecture"])
        if identity in seen:
            fail(f"duplicate Package/Version/Architecture: {identity}")
        seen.add(identity)

        filename = stanza["Filename"]
        if not filename.startswith("./debs/"):
            fail(f"invalid Filename: {filename}")
        path = (ROOT / filename[2:]).resolve()
        if DEBS.resolve() not in path.parents or path.suffix != ".deb":
            fail(f"Filename escapes debs/: {filename}")
        if not path.is_file() or path.stat().st_size == 0:
            fail(f"missing/empty package: {filename}")
        referenced.add(path)
        expected_size = int(stanza["Size"])
        if path.stat().st_size != expected_size:
            fail(f"size mismatch for {filename}")
        if digest(path, "sha256") != stanza["SHA256"]:
            fail(f"SHA256 mismatch for {filename}")
        control = parse_fields(run(["dpkg-deb", "-f", str(path)]).decode())
        for field in ("Package", "Version", "Architecture", "Name", "Description", "Depends", "Pre-Depends", "Conflicts", "Provides", "Replaces", "Section", "Priority", "Maintainer", "Author", "Depiction", "SileoDepiction", "Icon"):
            if field in stanza and control.get(field) != stanza[field]:
                fail(f"control mismatch {field} for {filename}")
        run(["dpkg-deb", "-I", str(path)])
        run(["dpkg-deb", "-c", str(path)])
    actual = set(DEBS.glob("*.deb"))
    if actual != referenced:
        missing = sorted(str(path.relative_to(ROOT)) for path in actual - referenced)
        absent = sorted(str(path.relative_to(ROOT)) for path in referenced - actual)
        fail(f"Packages/file inventory mismatch; unreferenced={missing} absent={absent}")
    return stanzas, referenced


def verify_compression() -> None:
    if not PACKAGES_GZ.is_file() or not PACKAGES_BZ2.is_file():
        fail("compressed Packages file is missing")
    plain = PACKAGES.read_bytes()
    if run(["gzip", "-dc", str(PACKAGES_GZ)]) != plain:
        fail("Packages.gz does not decompress to Packages")
    if run(["bzip2", "-dc", str(PACKAGES_BZ2)]) != plain:
        fail("Packages.bz2 does not decompress to Packages")


def verify_release() -> None:
    fields = parse_fields(RELEASE.read_text(encoding="utf-8"))
    for field in ("Origin", "Label", "Suite", "Codename", "Version", "Architectures", "Components", "Date"):
        if field not in fields:
            fail(f"Release missing {field}")
    release_text = RELEASE.read_text(encoding="utf-8")
    if "SHA256:\n" not in release_text or "MD5Sum:\n" not in release_text:
        fail("Release checksum sections missing")
    for name in ("Packages", "Packages.gz", "Packages.bz2"):
        path = ROOT / name
        sha_line = f" {digest(path, 'sha256')} {path.stat().st_size} {name}"
        md5_line = f" {digest(path, 'md5')} {path.stat().st_size} {name}"
        if sha_line not in release_text or md5_line not in release_text:
            fail(f"Release checksum mismatch for {name}")


def main() -> int:
    verify_no_forbidden_artifacts()
    stanzas, referenced = verify_packages()
    verify_compression()
    verify_release()
    print("PASS: HND feed has no public source/IPA/forbidden artifacts")
    print(f"PASS: verified {len(stanzas)} Packages entries and {len(referenced)} .deb files")
    print("PASS: dpkg-deb metadata/layout, size/SHA256, compression and Release checksums")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
