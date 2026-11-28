"""Check that every application container runs the code that is checked out.

Read-only. It builds nothing, restarts nothing, and reads no secret. Run it on
the NAS host from the repository root, after a deploy and whenever in doubt:

    python3 apps/api/scripts/verify_deployment.py
    python3 apps/api/scripts/verify_deployment.py --json

or through ``scripts/verify-deployment.sh``, which does the same.

What it compares, per service:

* api, scanner-worker, swing-worker, research-worker: the source fingerprint of the ``app``
  package inside the running container against the same fingerprint of the
  checkout on disk, and the build commit baked into the image against
  ``git rev-parse HEAD``.
* web: the build commit only. Its code is a compiled Next.js bundle, so there
  is no source to fingerprint inside it.

Exit code 0 when everything matches, 1 when anything does not or a service is
not running, 2 when the check itself could not run. A build commit of
"unknown" -- an image built before the commit was stamped in -- is a warning,
not a failure, provided the source fingerprint still matches.

Why it exists: on 27 November 2026 the swing worker had been running code 45
hours older than the API, because the documented deploy rebuilt only
``api scanner-worker``. Start times did not show it; only hashing the source in
each container did.

**Standard library only, and Python 3.8.** The NAS host has a plain python3
(3.8.15 on 27 November 2026) and nothing else,
so this must not import the application. It loads ``app/services/build_info.py``
by path, which is itself standard-library only.

**It prints no secret.** It never reads ``.env`` and never runs ``env`` or
``printenv`` in a container. Each container is asked to run one fixed command
that prints a commit hash and a fingerprint; anything else it returns is
dropped before printing, and a commit that is not a hex string is shown as
"unknown".
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional  # noqa: UP035 - the NAS host runs Python 3.8

PYTHON_SERVICES = ("api", "scanner-worker", "swing-worker", "research-worker")
# Expected to be stopped on the NAS: research runs on the research VM (28 Nov
# 2026). Not running is OK; running here is a warning, and running other code
# than the checkout is still a failure.
OPTIONAL_SERVICES = ("research-worker",)
WEB_SERVICE = "web"
APPLICATION_SERVICES = (*PYTHON_SERVICES, WEB_SERVICE)

PYTHON_PROBE = ["python", "-m", "app.services.build_info"]
# For containers built before app/services/build_info.py existed. The same
# algorithm as build_info.source_fingerprint, inline, run from /app; a test
# holds the two to the same answer. Reads one environment variable and prints
# it only if it is a hex commit.
PYTHON_FALLBACK_PROBE = [
    "python",
    "-c",
    "import hashlib,json,os,pathlib\n"
    "r=pathlib.Path('app');h=hashlib.sha256()\n"
    "for p in sorted(x for x in r.rglob('*.py') if '__pycache__' not in x.parts and x.is_file()):\n"
    " h.update(p.relative_to(r).as_posix().encode());h.update(b'\\0');h.update(p.read_bytes());h.update(b'\\0')\n"
    "c=os.environ.get('SIDRA_BUILD_COMMIT','').strip().lower()\n"
    "ok=7<=len(c)<=40 and set(c)<=set('0123456789abcdef')\n"
    "print(json.dumps({'commit':c if ok else 'unknown','source_fingerprint':h.hexdigest()}))",
]
# Prints only the one variable, and only after the same hex check build_info applies.
WEB_PROBE = [
    "node",
    "-e",
    "const c=(process.env.SIDRA_BUILD_COMMIT||'').trim().toLowerCase();"
    "console.log(JSON.stringify({commit:/^[0-9a-f]{7,40}$/.test(c)?c:'unknown'}))",
]

OK, WARN, FAIL = "OK", "WARN", "FAIL"

# (service, command) -> stdout, or None when the service is not running or the
# command failed. Injected so the comparison can be tested without Docker.
#
# Spelled with typing's List and Optional because this line is evaluated at
# import, and the NAS host's python3 is 3.8, where ``list[str]`` and
# ``collections.abc.Callable[...]`` raise TypeError. Every other annotation in
# this file is a string under ``from __future__ import annotations``.
Runner = Callable[[str, List[str]], Optional[str]]  # noqa: UP006, UP007, UP045


def _load_build_info(repo_root: Path):  # noqa: ANN202 - a module object
    path = repo_root / "apps" / "api" / "app" / "services" / "build_info.py"
    spec = importlib.util.spec_from_file_location("sidra_build_info", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class ServiceCheck:
    service: str
    state: str
    commit: str | None = None
    source_fingerprint: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    expected_commit: str
    expected_source_fingerprint: str
    services: list[ServiceCheck]

    @property
    def state(self) -> str:
        states = {item.state for item in self.services}
        return FAIL if FAIL in states else WARN if WARN in states else OK


def _clean_commit(value: object, build_info) -> str:  # noqa: ANN001
    return build_info.build_commit({build_info.BUILD_COMMIT_ENV: str(value or "")})


def _clean_fingerprint(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text) else None


def _parse(stdout: str | None) -> dict | None:
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def evaluate(
    expected_commit: str,
    expected_source: str,
    runner: Runner,
    build_info,  # noqa: ANN001 - the module loaded by path
    services: tuple[str, ...] = APPLICATION_SERVICES,
) -> Verdict:
    """Ask each service what it runs and compare it with the checkout. Pure apart from ``runner``."""
    checks: list[ServiceCheck] = []
    for service in services:
        if service == WEB_SERVICE:
            reported = _parse(runner(service, WEB_PROBE))
        else:
            # An image built before build_info existed cannot run the module;
            # the fallback computes the same fingerprint inline.
            reported = _parse(runner(service, PYTHON_PROBE)) or _parse(runner(service, PYTHON_FALLBACK_PROBE))
        if reported is None:
            if service in OPTIONAL_SERVICES:
                checks.append(ServiceCheck(service, OK, notes=["not running on this host (runs on the research VM)"]))
                continue
            checks.append(ServiceCheck(service, FAIL, notes=["not running, or did not answer the version probe"]))
            continue
        check = ServiceCheck(service, OK, commit=_clean_commit(reported.get("commit"), build_info))
        if service != WEB_SERVICE:
            check.source_fingerprint = _clean_fingerprint(reported.get("source_fingerprint"))
            if check.source_fingerprint is None:
                check.state = FAIL
                check.notes.append("no readable source fingerprint")
            elif check.source_fingerprint != expected_source:
                check.state = FAIL
                check.notes.append("source differs from the checkout: this container runs other code")
        if check.commit == build_info.UNKNOWN:
            if check.state == OK:
                check.state = WARN
            check.notes.append("image carries no build commit (built before SIDRA_BUILD_COMMIT was passed)")
        elif expected_commit != build_info.UNKNOWN and check.commit != expected_commit:
            check.state = FAIL
            check.notes.append("built from a different commit than the checkout")
        if service in OPTIONAL_SERVICES:
            if check.state == OK:
                check.state = WARN
            check.notes.append("running on the NAS; research is meant to run on the research VM")
        checks.append(check)

    # When the checkout could not be fingerprinted, every container "differs from
    # the checkout" and the comparison above says nothing useful. The containers
    # can still be compared with each other, and a disagreement is a failure.
    # With a readable checkout fingerprint the per-service check above already
    # names exactly the stale ones, and the matching ones stay OK.
    fingerprints = {item.source_fingerprint for item in checks if item.source_fingerprint}
    if _clean_fingerprint(expected_source) is None and len(fingerprints) > 1:
        for item in checks:
            if item.source_fingerprint and item.state != FAIL:
                item.state = FAIL
                item.notes.append("python services run different code from each other")
    return Verdict(expected_commit, expected_source, checks)


def _short(value: str | None, length: int = 12) -> str:
    return (value or "-")[:length]


def render(verdict: Verdict) -> str:
    lines = [
        "Deployment consistency (read-only)",
        f"  checkout commit       : {_short(verdict.expected_commit)}",
        f"  checkout source hash  : {_short(verdict.expected_source_fingerprint, 16)}",
        "",
        f"  {'service':<16}{'state':<7}{'commit':<14}{'source hash':<18}notes",
    ]
    for item in verdict.services:
        lines.append(
            f"  {item.service:<16}{item.state:<7}{_short(item.commit):<14}"
            f"{_short(item.source_fingerprint, 16):<18}{'; '.join(item.notes)}"
        )
    lines.append("")
    lines.append(f"  RESULT: {verdict.state}")
    if verdict.state == FAIL:
        lines.append(
            "  Rebuild every application service from this checkout, after the deploy checklist:\n"
            "    SIDRA_BUILD_COMMIT=$(git rev-parse HEAD) /usr/local/bin/docker compose up -d --build "
            + " ".join(APPLICATION_SERVICES)
        )
    return "\n".join(lines)


def docker_runner(docker: str, cwd: Path) -> Runner:
    def run(service: str, command: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                [docker, "compose", "exec", "-T", service, *command],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        # stderr is never printed: a failing exec can echo the command line or
        # the environment it was given, and neither belongs in this output.
        return completed.stdout if completed.returncode == 0 else None

    return run


def _git_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=20, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only check that all application containers run this checkout.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3], help="Repository root.")
    parser.add_argument("--docker", default=None, help="Docker binary (default: /usr/local/bin/docker, else docker).")
    parser.add_argument("--json", action="store_true", help="Print the verdict as JSON.")
    args = parser.parse_args(argv)

    repo_root = args.repo.resolve()
    try:
        build_info = _load_build_info(repo_root)
        expected_source = build_info.source_fingerprint(repo_root / "apps" / "api" / "app")
    except Exception as exc:  # noqa: BLE001 - reported, never raised at an operator
        print(f"Cannot fingerprint the checkout: {type(exc).__name__}", file=sys.stderr)
        return 2
    expected_commit = _clean_commit(_git_head(repo_root), build_info)
    docker = args.docker or (
        "/usr/local/bin/docker" if Path("/usr/local/bin/docker").exists() else shutil.which("docker")
    )
    if not docker:
        print("Docker was not found.", file=sys.stderr)
        return 2

    verdict = evaluate(expected_commit, expected_source, docker_runner(docker, repo_root), build_info)
    if args.json:
        print(json.dumps({"state": verdict.state, **asdict(verdict)}, indent=2))
    else:
        print(render(verdict))
    return 0 if verdict.state != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
