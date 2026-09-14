# HND Repo

Sileo / Zebra source:

https://hduybr.github.io/repo/

This repository contains compiled Debian packages and APT/Sileo metadata only.
Private project source is never published here. IPA files remain in their
project release repositories and are not part of this feed.

The feed is synchronized locally from non-draft `.deb` assets released by all
repositories owned by `hduybr`. The latest package selected for each
Package+Architecture pair uses Debian version comparison. A same-version,
different-SHA conflict is a hard failure.

Run locally after a project publishes a new DEB:

```sh
python3 scripts/sync-feed.py
python3 scripts/verify-feed.py
```

Only push after verification passes. GitHub Actions and Codex Cloud are not
used by this repository.
