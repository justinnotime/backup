import json
import shlex
import subprocess
import sys

import pytest

from runtime_install.config import ConfigError, load_config, resolve
from runtime_install.install import InstallError, cron_config, job_line, main


def test_environment_defaults_relocation_and_no_shell_evaluation(tmp_path, monkeypatch):
    home = tmp_path / "another user"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PACKAGE_LOCATION", raising=False)
    value = {
        "env": "PACKAGE_LOCATION",
        "default": "~/packages/tool",
        "suffix": "/scripts/run",
    }
    assert resolve(value) == str(home / "packages/tool/scripts/run")
    monkeypatch.setenv("PACKAGE_LOCATION", str(home / "selected package"))
    assert resolve(value) == str(home / "selected package/scripts/run")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert resolve("${XDG_CONFIG_HOME}/private.json") == str(
        home / ".config/private.json"
    )
    marker = tmp_path / "must-not-exist"
    literal = "$(touch " + str(marker) + ") ${SHELL_VALUE:+literal}"
    assert resolve(literal) == literal
    assert not marker.exists()


def test_config_dir_uses_source_of_symlink_and_moves_with_home(tmp_path):
    first = tmp_path / "first home"
    source = first / "private/settings.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"path": "${CONFIG_DIR}/profile.md"}))
    link = first / "configured.json"
    link.symlink_to("private/settings.json")
    assert load_config(link)["path"] == str(source.parent / "profile.md")
    moved = tmp_path / "different home"
    first.rename(moved)
    assert load_config(moved / "configured.json")["path"] == str(
        moved / "private/profile.md"
    )


def test_missing_selected_environment_rejected_without_value_disclosure(monkeypatch):
    monkeypatch.delenv("MISSING_LOCATION", raising=False)
    for value in ("${MISSING_LOCATION}/file", {"env": "MISSING_LOCATION"}):
        with pytest.raises(ConfigError, match="environment is missing"):
            resolve(value)
    monkeypatch.setenv("MISSING_LOCATION", "")
    with pytest.raises(ConfigError, match="environment is missing"):
        resolve("${MISSING_LOCATION}/file")
    with pytest.raises(ConfigError):
        resolve({"env": "INVALID=VALUE", "default": "private-placeholder"})


def test_structured_cron_job_runs_literal_arguments_with_spaced_paths(tmp_path):
    script = tmp_path / "selected command.py"
    output = tmp_path / "command output.json"
    log = tmp_path / "selected log"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
    )
    args = ["space value", "$(not-a-command)", "; no execution"]
    job = {
        "id": "collector",
        "schedule": "12 * * * *",
        "argv": [sys.executable, str(script), str(output), *args],
        "log": str(log),
        "environment": {"OPTION": "value with spaces", "OMITTED": ""},
    }
    line = job_line(job)
    assert shlex.split(line)[5] == "OPTION=value with spaces"
    assert (
        subprocess.run(["/bin/sh", "-c", line.split(" ", 5)[5]], check=False).returncode
        == 0
    )
    assert json.loads(output.read_text()) == args


@pytest.mark.parametrize(
    "update",
    [{"log": "/example/%bad"}, {"environment": {"A=B": "x"}}, {"schedule": "invalid"}],
)
def test_bad_structured_cron_rejected(update):
    with pytest.raises(InstallError):
        job_line({"schedule": "12 * * * *", "argv": ["/example/run"], **update})


def test_print_job_is_read_only_and_does_not_call_checks(tmp_path, capsys):
    marker = tmp_path / "must-not-exist"
    job = {"id": "collector", "schedule": "12 * * * *", "argv": ["/example/run"]}
    config = {
        "schema": "runtime-install/v1",
        "kind": "cron",
        "jobs": [job],
        "checks": [{"argv": ["touch", str(marker)]}],
        "requirements": [{"path": "/does-not-exist"}],
    }
    filename = tmp_path / "jobs.json"
    filename.write_text(json.dumps(config))
    assert main("cron", ["--config", str(filename), "--print-job", "collector"]) == 0
    assert capsys.readouterr().out == "12 * * * * /example/run\n"
    assert not marker.exists()
    assert main("cron", ["--config", str(filename), "--print-job", "missing"]) == 1
    with pytest.raises(InstallError):
        cron_config({**config, "lines": []})
