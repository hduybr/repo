# HND central Sileo/Zebra feed rules

- This is the central public Sileo/Zebra feed at https://hduybr.github.io/repo/.
- Publish only compiled `.deb` files and APT/web metadata.
- Never publish source code, source archives, private manifests, IPA files,
  credentials, tokens, logs, DerivedData or source snapshots.
- Synchronize all non-archived repositories owned by `hduybr`; do not hardcode
  only one project.
- Inspect package metadata with `dpkg-deb`; do not infer Package ID from a
  filename.
- The latest Package+Architecture wins using Debian version comparison.
- A same Package+Version+Architecture with a different SHA-256 is a hard
  conflict and must stop synchronization.
- Never build, re-sign, re-pack, patch or change a project DEB in this repo.
- No GitHub Actions, Codex Cloud, remote build worker or cloud CI is used.
- Run synchronization and verification locally after each project DEB release.
- Never weaken package validation to obtain a pass.
- Run `python3 scripts/verify-feed.py` before every push.
