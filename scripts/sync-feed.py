#!/usr/bin/env python3
"""Synchronize the public HND APT feed from all hduybr release repositories.

The central repository never builds, repacks, resigns, or patches packages. It
downloads release .deb assets into a temporary staging directory, selects the
newest Package+Architecture pair using dpkg version semantics, and publishes
only the selected original bytes under debs/.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEBS = ROOT / "debs"
PACKAGES = ROOT / "Packages"
PACKAGES_GZ = ROOT / "Packages.gz"
PACKAGES_BZ2 = ROOT / "Packages.bz2"
RELEASE = ROOT / "Release"
INDEX = ROOT / "index.html"

CONTROL_FIELDS = (
    "Package",
    "Name",
    "Version",
    "Architecture",
    "Description",
    "Depends",
    "Pre-Depends",
    "Conflicts",
    "Provides",
    "Replaces",
    "Section",
    "Priority",
    "Maintainer",
    "Author",
    "Depiction",
    "SileoDepiction",
    "Icon",
)


@dataclass(frozen=True)
class Candidate:
    path: Path
    filename: str
    package: str
    version: str
    architecture: str
    fields: dict[str, str]
    sha256: str
    sha512: str
    size: int
    repository: str
    release_tag: str
    release_url: str

    @property
    def key(self) -> tuple[str, str]:
        return self.package, self.architecture

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.package, self.version, self.architecture


def run(command: list[str], *, cwd: Path | None = None, stdout=None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE if stdout is None else subprocess.PIPE,
        text=stdout is None,
    )
    return result.stdout if stdout is None else ""


def gh_json(command: list[str]) -> object:
    return json.loads(run(["gh", *command]))


def parse_control(raw: str) -> dict[str, str]:
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
        if not separator:
            continue
        current = name
        fields[current] = value.lstrip()
    return fields


def sha(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compare_debian_versions(left: str, operator: str, right: str) -> bool:
    result = subprocess.run(
        ["dpkg", "--compare-versions", left, operator, right],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode == 0


def repositories() -> list[dict[str, object]]:
    data = gh_json(
        [
            "repo",
            "list",
            "hduybr",
            "--limit",
            "1000",
            "--json",
            "name,nameWithOwner,isPrivate,isArchived,url",
        ]
    )
    if not isinstance(data, list):
        raise RuntimeError("gh repo list did not return a JSON list")
    return data


def releases(repository: str) -> list[dict[str, object]]:
    raw = run(
        [
            "gh",
            "api",
            f"repos/{repository}/releases?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    pages = json.loads(raw)
    if not isinstance(pages, list):
        raise RuntimeError(f"release response for {repository} is not a list")
    if pages and isinstance(pages[0], list):
        result: list[dict[str, object]] = []
        for page in pages:
            result.extend(page)
        return result
    return pages


def inspect_deb(
    path: Path,
    *,
    repository: str,
    release_tag: str,
    release_url: str,
) -> Candidate:
    fields = parse_control(run(["dpkg-deb", "-f", str(path)]))
    required = ("Package", "Version", "Architecture")
    missing = [field for field in required if not fields.get(field)]
    if missing:
        raise ValueError(f"missing control fields: {', '.join(missing)}")
    return Candidate(
        path=path,
        filename=path.name,
        package=fields["Package"],
        version=fields["Version"],
        architecture=fields["Architecture"],
        fields=fields,
        sha256=sha(path, "sha256"),
        sha512=sha(path, "sha512"),
        size=path.stat().st_size,
        repository=repository,
        release_tag=release_tag,
        release_url=release_url,
    )


def download_candidates(
    stage: Path,
) -> tuple[list[Candidate], int, int, list[str]]:
    all_repositories = repositories()
    scanned_releases = 0
    discovered = 0
    candidates: list[Candidate] = []
    rejected: list[str] = []

    for repository_entry in all_repositories:
        if repository_entry.get("isArchived"):
            continue
        repository = str(repository_entry["nameWithOwner"])
        for release in releases(repository):
            if release.get("draft"):
                continue
            scanned_releases += 1
            assets = release.get("assets") or []
            deb_assets = [
                asset
                for asset in assets
                if str(asset.get("name", "")).lower().endswith(".deb")
            ]
            if not deb_assets:
                continue

            release_tag = str(release.get("tag_name", ""))
            if not release_tag:
                rejected.append(f"{repository}: release {release.get('id')} has no tag")
                continue
            release_dir = stage / f"{repository.replace('/', '__')}__{release.get('id')}"
            release_dir.mkdir(parents=True, exist_ok=True)
            run(
                [
                    "gh",
                    "release",
                    "download",
                    release_tag,
                    "--repo",
                    repository,
                    "--pattern",
                    "*.deb",
                    "--dir",
                    str(release_dir),
                ]
            )
            release_url = f"https://github.com/{repository}/releases/tag/{release_tag}"
            for asset in deb_assets:
                filename = str(asset["name"])
                path = release_dir / filename
                discovered += 1
                if not path.is_file() or path.stat().st_size == 0:
                    rejected.append(f"{repository}@{release_tag}/{filename}: missing or empty")
                    continue
                try:
                    candidates.append(
                        inspect_deb(
                            path,
                            repository=repository,
                            release_tag=release_tag,
                            release_url=release_url,
                        )
                    )
                except (OSError, subprocess.CalledProcessError, ValueError) as error:
                    rejected.append(f"{repository}@{release_tag}/{filename}: {error}")

    return candidates, len(all_repositories), scanned_releases, discovered, rejected


def select_latest(candidates: list[Candidate]) -> tuple[list[Candidate], list[str]]:
    selected: dict[tuple[str, str], Candidate] = {}
    by_filename: dict[str, Candidate] = {}
    notes: list[str] = []

    for candidate in candidates:
        previous_filename = by_filename.get(candidate.filename)
        if previous_filename and (
            previous_filename.sha256 != candidate.sha256
            or previous_filename.identity != candidate.identity
        ):
            raise RuntimeError(
                f"filename conflict for {candidate.filename}: "
                f"{previous_filename.repository}@{previous_filename.release_tag} vs "
                f"{candidate.repository}@{candidate.release_tag}"
            )
        by_filename[candidate.filename] = candidate

        previous = selected.get(candidate.key)
        if previous is None:
            selected[candidate.key] = candidate
            continue
        if compare_debian_versions(candidate.version, "eq", previous.version):
            if candidate.sha256 != previous.sha256:
                raise RuntimeError(
                    "same Package/Version/Architecture has different SHA256: "
                    f"{candidate.package}/{candidate.version}/{candidate.architecture}: "
                    f"{previous.sha256} ({previous.repository}@{previous.release_tag}) vs "
                    f"{candidate.sha256} ({candidate.repository}@{candidate.release_tag})"
                )
            continue
        if compare_debian_versions(candidate.version, "gt", previous.version):
            selected[candidate.key] = candidate
            notes.append(
                f"{candidate.package}/{candidate.architecture}: {previous.version} -> {candidate.version}"
            )

    return sorted(selected.values(), key=lambda item: (item.package, item.architecture)), notes


def package_stanza(candidate: Candidate) -> str:
    lines: list[str] = []
    for field in CONTROL_FIELDS:
        value = candidate.fields.get(field)
        if value is not None and value != "":
            lines.append(f"{field}: {value}")
    lines.extend(
        [
            f"Filename: ./debs/{candidate.filename}",
            f"Size: {candidate.size}",
            f"SHA256: {candidate.sha256}",
        ]
    )
    return "\n".join(lines)


def write_compressed() -> None:
    with PACKAGES_GZ.open("wb") as output:
        run(["gzip", "-9nc", PACKAGES.name], cwd=ROOT, stdout=output)
    with PACKAGES_BZ2.open("wb") as output:
        run(["bzip2", "-9kc", PACKAGES.name], cwd=ROOT, stdout=output)


def write_release() -> None:
    feed_files = (PACKAGES, PACKAGES_GZ, PACKAGES_BZ2)
    md5_lines = [f" {sha(path, 'md5')} {path.stat().st_size} {path.name}" for path in feed_files]
    sha256_lines = [
        f" {sha(path, 'sha256')} {path.stat().st_size} {path.name}" for path in feed_files
    ]
    date = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)
    content = "\n".join(
        [
            "Origin: HND Repo",
            "Label: HND Repo",
            "Suite: stable",
            "Codename: hnd",
            "Version: 1.0",
            "Architectures: iphoneos-arm iphoneos-arm64 iphoneos-arm64e all",
            "Components: main",
            "Description: HND Sileo / Zebra Repository",
            f"Date: {date}",
            "",
            "MD5Sum:",
            *md5_lines,
            "",
            "SHA256:",
            *sha256_lines,
            "",
        ]
    )
    RELEASE.write_text(content, encoding="utf-8")


def write_index(selected: list[Candidate]) -> None:
    rows = []
    for candidate in selected:
        rows.append(
            "<tr>"
            f"<td>{html.escape(candidate.fields.get('Name', candidate.package))}</td>"
            f"<td><code>{html.escape(candidate.package)}</code></td>"
            f"<td>{html.escape(candidate.version)}</td>"
            f"<td>{html.escape(candidate.architecture)}</td>"
            f"<td><a href=\"./debs/{html.escape(candidate.filename, quote=True)}\">Download</a></td>"
            "</tr>"
        )
    table = "\n".join(rows) or "<tr><td colspan=\"5\">No packages published yet.</td></tr>"
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HND Repo</title>
  <style>body{{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#202124}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:.55rem;text-align:left}}code{{font-size:.9em}}</style>
</head>
<body>
  <h1>HND Repo</h1>
  <p>Sileo / Zebra Package Repository</p>
  <p>Source URL: <code>https://hduybr.github.io/repo/</code></p>
  <p>Compiled Debian packages and APT metadata only. Private project source is not published here.</p>
  <table>
    <thead><tr><th>Name</th><th>Package ID</th><th>Version</th><th>Architecture</th><th></th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</body>
</html>
"""
    INDEX.write_text(content, encoding="utf-8")


def main() -> int:
    DEBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hnd-feed-stage-", dir="/private/tmp") as temp:
        candidates, repository_count, release_count, discovered, rejected = download_candidates(
            Path(temp)
        )
        selected, superseded = select_latest(candidates)
        selected_names = {candidate.filename for candidate in selected}
        for existing in DEBS.glob("*.deb"):
            if existing.name not in selected_names:
                existing.unlink()
        for candidate in selected:
            destination = DEBS / candidate.filename
            shutil.copyfile(candidate.path, destination)

    PACKAGES.write_text(
        "\n\n".join(package_stanza(candidate) for candidate in selected) + "\n",
        encoding="utf-8",
    )
    write_compressed()
    write_release()
    write_index(selected)

    print(f"Repositories scanned: {repository_count}")
    print(f"Releases scanned: {release_count}")
    print(f"DEB assets discovered: {discovered}")
    print(f"Unique Package+Architecture keys: {len(selected)}")
    print(f"Packages selected: {len(selected)}")
    print(f"Packages rejected/conflicted: {len(rejected)}")
    for item in rejected:
        print(f"REJECTED: {item}")
    print(f"Packages superseded by newer Debian versions: {len(superseded)}")
    print("Packages published:")
    for candidate in selected:
        print(
            f"  {candidate.package} {candidate.version} {candidate.architecture} "
            f"{candidate.filename} sha256={candidate.sha256} size={candidate.size}"
        )
    if rejected:
        print("Feed generated with rejected assets; inspect the lines above before push.")
    else:
        print("Sync completed without rejected or conflicting packages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
