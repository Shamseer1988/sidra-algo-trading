"""Which code this process is running, in a form two containers can be compared by.

Container start times say when something was restarted, not what it runs. On
27 November the swing worker was 45 hours older than the API beside it and
nothing on any screen said so; it took hashing the source inside each container
by hand to find out.

So each process can report two things:

* ``commit`` -- the git commit its image was built from, baked in at build
  time through the ``SIDRA_BUILD_COMMIT`` build argument. "unknown" for an
  image built without it, which is every image built before this existed.
* ``source_fingerprint`` -- a SHA-256 over the ``app`` package's Python source,
  computed from the files actually present. It needs nothing at build time,
  so it works for old images too, and it is what proves two containers run
  the same code even when nobody passed the commit.

**Standard library only, on purpose.** ``scripts/verify_deployment.py`` loads
this file by path on the NAS host, outside any container, to fingerprint the
checked-out repository the same way. Importing anything from ``app`` here would
make that impossible. Run as ``python -m app.services.build_info`` inside a
container it prints one line of JSON and nothing else -- never an environment
value.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

BUILD_COMMIT_ENV = "SIDRA_BUILD_COMMIT"
UNKNOWN = "unknown"
_COMMIT_CHARACTERS = frozenset("0123456789abcdef")


def build_commit(environ: dict[str, str] | None = None) -> str:
    """The commit this image was built from, or "unknown".

    Anything that is not a plausible git hash is reported as unknown rather
    than echoed, so a mis-set build argument can never put an arbitrary string
    -- or a secret pasted into the wrong variable -- into a status response.
    """
    raw = (environ if environ is not None else os.environ).get(BUILD_COMMIT_ENV, "").strip().lower()
    if 7 <= len(raw) <= 40 and set(raw) <= _COMMIT_CHARACTERS:
        return raw
    return UNKNOWN


def source_fingerprint(package_root: Path | str | None = None) -> str:
    """SHA-256 over every ``.py`` file under ``package_root``, path and content.

    Paths are included, relative and with forward slashes, so a renamed or
    moved module changes the fingerprint; files are read in sorted order and
    as bytes, so the result does not depend on the platform or the locale.
    ``__pycache__`` is never read: compiled files differ between the host and
    a container running the same source.
    """
    root = Path(package_root) if package_root is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts and path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_info(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Commit and fingerprint, and nothing else -- this is what status screens show."""
    return {"commit": build_commit(environ), "source_fingerprint": source_fingerprint()}


if __name__ == "__main__":  # pragma: no cover - exercised by the deployment check
    print(json.dumps(build_info()))
