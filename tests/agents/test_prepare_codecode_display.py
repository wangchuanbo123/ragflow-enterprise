"""Dependency build output uses the product brand while private values stay intact."""

from types import SimpleNamespace
import subprocess

import pytest

from scripts import prepare_codecode as prepare


def test_build_output_brands_both_streams(capsys, monkeypatch):
    monkeypatch.setattr(prepare.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="OpenCode build: vendor/opencode\n", stderr="OPENCODE progress\n"))
    prepare.run(["tool"], env={})
    shown = capsys.readouterr().out
    assert shown == "codecode build: vendor/codecode\ncodecode progress\n"
    assert "opencode" not in shown.lower()


def test_private_capture_preserves_source_contract_without_display(capsys, monkeypatch):
    private = "https://github.com/anomalyco/opencode.git\n"
    monkeypatch.setattr(prepare.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=private, stderr=""))
    result = prepare.run(["tool"], env={}, capture=True)
    assert result.stdout == private
    assert capsys.readouterr().out == ""


def test_failed_build_displays_branded_output_without_dependency_argv(capsys, monkeypatch):
    monkeypatch.setattr(prepare.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=2, stdout="OpenCode build failed\n", stderr="opencode internal error\n"))
    with pytest.raises(RuntimeError, match="codecode preparation command failed") as error:
        prepare.run(["vendor/opencode/bin"], env={}, capture=True)
    assert "opencode" not in str(error.value).lower()
    shown = capsys.readouterr().out
    assert "codecode build failed" in shown
    assert "opencode" not in shown.lower()


def test_timed_out_build_does_not_leak_dependency_command_or_output(capsys, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["vendor/opencode/bin"], 1, output="OpenCode partial output")
    monkeypatch.setattr(prepare.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="codecode preparation command timed out") as error:
        prepare.run(["vendor/opencode/bin"], env={})
    assert "opencode" not in str(error.value).lower()
    assert capsys.readouterr().out == ""
