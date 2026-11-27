"""The deployment check finds a container running other code, and prints no secret.

On 27 November 2026 the swing worker had run code 45 hours older than the API
for two days, because the documented deploy rebuilt only api and
scanner-worker. These tests drive the comparison with a fake Docker, so they
need neither Docker nor the NAS.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import build_info

# The API folder is the root that exists everywhere: in a checkout it is
# apps/api, and inside the test container scripts/run-tests.sh builds, it is
# /app (only apps/api is copied in). The repository root, and with it
# docker-compose.yml, exists only in a checkout.
API_ROOT = Path(__file__).resolve().parents[1]
REPO = next((parent for parent in API_ROOT.parents if (parent / "docker-compose.yml").exists()), None)
NO_CHECKOUT = "needs the repository checkout (docker-compose.yml); the test container holds apps/api only"
SCRIPT = API_ROOT / "scripts" / "verify_deployment.py"
COMMIT = "a4548b0abddba8df5ef5bdd7ed8acebb3b7b8065"
OTHER_COMMIT = "f657615" + "0" * 33
SOURCE = "a" * 64
STALE_SOURCE = "b" * 64
SECRET = "SUPER-SECRET-TOTP-SEED-123"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_deployment_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load_script()


def _runner(answers: dict[str, dict | None]):
    def run(service: str, _command: list[str]) -> str | None:
        answer = answers.get(service)
        return None if answer is None else json.dumps(answer)

    return run


def _python(commit: str = COMMIT, source: str = SOURCE, **extra) -> dict:
    return {"commit": commit, "source_fingerprint": source, **extra}


def _all_good() -> dict[str, dict | None]:
    return {**{name: _python() for name in verify.PYTHON_SERVICES}, "web": {"commit": COMMIT}}


def _states(verdict) -> dict[str, str]:
    return {item.service: item.state for item in verdict.services}


# --- build_info itself ------------------------------------------------------------


def test_build_commit_accepts_only_a_hex_hash():
    assert build_info.build_commit({"SIDRA_BUILD_COMMIT": COMMIT}) == COMMIT
    assert build_info.build_commit({"SIDRA_BUILD_COMMIT": COMMIT.upper()}) == COMMIT
    assert build_info.build_commit({}) == "unknown"
    assert build_info.build_commit({"SIDRA_BUILD_COMMIT": SECRET}) == "unknown"
    assert build_info.build_commit({"SIDRA_BUILD_COMMIT": "abc"}) == "unknown"


def test_the_fingerprint_follows_content_and_paths(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n")
    first = build_info.source_fingerprint(tmp_path)
    assert build_info.source_fingerprint(tmp_path) == first

    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "b.cpython-312.py").write_text("ignored")
    assert build_info.source_fingerprint(tmp_path) == first

    (tmp_path / "pkg" / "b.py").write_text("y = 3\n")
    changed = build_info.source_fingerprint(tmp_path)
    assert changed != first

    (tmp_path / "pkg" / "b.py").rename(tmp_path / "pkg" / "c.py")
    assert build_info.source_fingerprint(tmp_path) != changed


def test_build_info_reports_two_fields_and_no_environment(monkeypatch):
    monkeypatch.setenv("SIDRA_BUILD_COMMIT", COMMIT)
    monkeypatch.setenv("UPSTOX_TOTP_SECRET", SECRET)
    info = build_info.build_info()
    assert set(info) == {"commit", "source_fingerprint"}
    assert SECRET not in json.dumps(info)


# --- the comparison ---------------------------------------------------------------


def test_a_consistent_deployment_passes():
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(_all_good()), build_info)
    assert verdict.state == verify.OK
    assert set(_states(verdict)) == {"api", "scanner-worker", "swing-worker", "research-worker", "web"}


def test_a_stale_swing_worker_fails():
    answers = _all_good()
    answers["swing-worker"] = _python(commit="unknown", source=STALE_SOURCE)
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    assert verdict.state == verify.FAIL
    assert _states(verdict)["swing-worker"] == verify.FAIL
    assert _states(verdict)["api"] == verify.OK


def test_a_missing_research_worker_fails():
    answers = _all_good()
    answers["research-worker"] = None
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    assert verdict.state == verify.FAIL and _states(verdict)["research-worker"] == verify.FAIL


def test_a_missing_swing_worker_fails():
    answers = _all_good()
    answers["swing-worker"] = None
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    assert verdict.state == verify.FAIL
    assert "not running" in " ".join(next(i for i in verdict.services if i.service == "swing-worker").notes)


def test_a_web_built_from_another_commit_fails():
    answers = _all_good()
    answers["web"] = {"commit": OTHER_COMMIT}
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    assert _states(verdict)["web"] == verify.FAIL


def test_matching_code_without_a_stamped_commit_is_only_a_warning():
    """Every image built before this change: same code, no commit inside."""
    answers = {name: _python(commit="unknown") for name in verify.PYTHON_SERVICES}
    answers["web"] = {"commit": "unknown"}
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    assert verdict.state == verify.WARN


def test_services_that_disagree_with_each_other_fail_even_if_the_checkout_is_unreadable():
    answers = _all_good()
    answers["scanner-worker"] = _python(source=STALE_SOURCE)
    verdict = verify.evaluate(COMMIT, "unreadable", _runner(answers), build_info)
    assert verdict.state == verify.FAIL
    assert all(state == verify.FAIL for name, state in _states(verdict).items() if name != "web")


def test_an_unreadable_answer_fails():
    def run(service, _command):
        return "Traceback (most recent call last): ..." if service == "api" else json.dumps(_python())

    verdict = verify.evaluate(COMMIT, SOURCE, run, build_info, services=verify.PYTHON_SERVICES)
    assert _states(verdict)["api"] == verify.FAIL


# --- nothing secret reaches the output ----------------------------------------------


def test_extra_fields_and_bad_values_never_reach_the_output():
    answers = {
        name: _python(commit=SECRET, source=SOURCE, password=SECRET, env={"UPSTOX_PIN": SECRET})
        for name in verify.PYTHON_SERVICES
    }
    answers["web"] = {"commit": SECRET, "token": SECRET}
    verdict = verify.evaluate(COMMIT, SOURCE, _runner(answers), build_info)
    text = verify.render(verdict)
    assert SECRET not in text
    assert SECRET not in json.dumps({"state": verdict.state, **verify.asdict(verdict)})


def test_the_docker_runner_never_returns_stderr(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=f"UPSTOX_PIN={SECRET}")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    assert verify.docker_runner("docker", API_ROOT)("api", verify.PYTHON_PROBE) is None


def test_the_fallback_probe_computes_the_same_fingerprint(tmp_path):
    """Old images cannot run build_info, so the inline probe must agree with it exactly."""
    package = tmp_path / "app"
    (package / "services" / "__pycache__").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "services" / "x.py").write_text("value = 'é'\n", encoding="utf-8")
    (package / "services" / "__pycache__" / "x.cpython-312.py").write_text("ignored")
    completed = subprocess.run(
        [sys.executable, *verify.PYTHON_FALLBACK_PROBE[1:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env={"SIDRA_BUILD_COMMIT": SECRET, "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
    )
    reported = json.loads(completed.stdout)
    assert reported == {"commit": "unknown", "source_fingerprint": build_info.source_fingerprint(package)}
    assert SECRET not in completed.stdout


def test_an_old_image_is_read_through_the_fallback():
    def run(service, command):
        if command == verify.PYTHON_PROBE:
            return None  # "No module named app.services.build_info"
        return json.dumps(_python(commit="unknown")) if service != "web" else json.dumps({"commit": "unknown"})

    verdict = verify.evaluate(COMMIT, SOURCE, run, build_info)
    assert verdict.state == verify.WARN


def test_the_probe_commands_read_nothing_but_the_commit():
    assert verify.PYTHON_PROBE == ["python", "-m", "app.services.build_info"]
    probe = " ".join(verify.WEB_PROBE)
    assert "SIDRA_BUILD_COMMIT" in probe and "process.env)" not in probe and "printenv" not in probe


@pytest.mark.skipif(REPO is None, reason=NO_CHECKOUT)
@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_main_against_a_fake_docker_prints_no_secret(monkeypatch, capsys, flag):
    monkeypatch.setenv("UPSTOX_TOTP_SECRET", SECRET)
    repo_source = build_info.source_fingerprint(REPO / "apps" / "api" / "app")
    answers = {name: _python(commit="unknown", source=repo_source) for name in verify.PYTHON_SERVICES}
    answers["web"] = {"commit": "unknown"}
    monkeypatch.setattr(verify, "docker_runner", lambda _docker, _cwd: _runner(answers))
    code = verify.main(["--repo", str(REPO), "--docker", "docker", *flag])
    out = capsys.readouterr()
    assert code == 0  # same code everywhere, commits unknown: a warning, not a failure
    assert SECRET not in out.out and SECRET not in out.err
    assert "swing-worker" in out.out
