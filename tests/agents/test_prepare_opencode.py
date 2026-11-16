"""Source preparation is isolated and its cached artifact is tied to its pin."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_opencode as prepare


def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(prepare, "ROOT", root)
    manifest = {
        "repository": "https://github.com/anomalyco/opencode.git",
        "checkout": "vendor/opencode",
        "tag": "v2.2.5",
        "commit": "9bbb8ce18c80f099e0e448775f59d965ead40a21",
        "bun_version": "1.4.4",
        "runtime_directory": "integrations/opencode/runtime",
    }
    return root, manifest


def test_build_environment_discards_personal_configuration_and_application_secrets(tmp_path, monkeypatch):
    root, _ = project(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "system-toolchain-path")
    monkeypatch.setenv("HOME", str(tmp_path / "personal"))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "personal-config")
    monkeypatch.setenv("ANSWER_LLM_API_KEY", "application-secret")
    monkeypatch.setenv("NPM_TOKEN", "registry-secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("BUN_OPTIONS", "--preload personal.js")
    env = prepare.build_environment()
    assert env["PATH"] == "system-toolchain-path"
    for key in ("OPENCODE_CONFIG_CONTENT", "ANSWER_LLM_API_KEY", "NPM_TOKEN", "GIT_CONFIG_COUNT", "BUN_OPTIONS"):
        assert key not in env
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(env[key]).resolve().is_relative_to(root)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == prepare.os.devnull
    npm_user = Path(env["NPM_CONFIG_USERCONFIG"])
    npm_global = Path(env["NPM_CONFIG_GLOBALCONFIG"])
    assert npm_user != npm_global
    assert npm_user.is_relative_to(root) and npm_global.is_relative_to(root)
    assert npm_user.read_text() == npm_global.read_text() == ""


def test_project_boundary_rejects_resolved_link_escape(tmp_path, monkeypatch):
    root, _ = project(tmp_path, monkeypatch)
    candidate = root / "linked" / "binary"
    outside = tmp_path / "personal" / "binary"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == candidate else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="escapes"):
        prepare.inside_project(candidate)


@pytest.mark.parametrize("target", ["build-home/config", "build-tools/node_modules/bun/bin/bun.exe", "runtime/opencode", "runtime/build.json"])
def test_specific_configuration_and_executable_links_are_checked_before_use(tmp_path, monkeypatch, target):
    root, manifest = project(tmp_path, monkeypatch)
    if target.startswith("runtime/"):
        suffix = target.split("/", 1)[1]
        if suffix == "opencode" and prepare.os.name == "nt":
            suffix += ".exe"
        candidate = root / manifest["runtime_directory"] / suffix
    else:
        candidate = root / "data/agents" / target
    outside = tmp_path / "personal" / candidate.name
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == candidate else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    calls = []
    monkeypatch.setattr(prepare, "run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(ValueError, match="escapes"):
        if target.startswith("build-home"):
            prepare.build_environment()
        elif target.startswith("build-tools"):
            prepare.ensure_bun(manifest, {})
        else:
            prepare.build_runtime(manifest, root / manifest["checkout"], {})
    assert calls == []


def test_source_checkout_requires_matching_remote_and_clean_tree_before_checkout(tmp_path, monkeypatch):
    root, manifest = project(tmp_path, monkeypatch)
    (root / manifest["checkout"]).mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        output = manifest["repository"] if command[-1] == "origin" else " M changed.ts"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(prepare, "run", run)
    with pytest.raises(ValueError, match="local changes"):
        prepare.verify_source(manifest, {})
    assert len(calls) == 2
    assert not any("fetch" in command or "checkout" in command for command in calls)


def test_cached_runtime_is_never_executed_when_content_does_not_match_build_proof(tmp_path, monkeypatch):
    root, manifest = project(tmp_path, monkeypatch)
    output = root / manifest["runtime_directory"]
    output.mkdir(parents=True)
    executable = output / ("opencode.exe" if prepare.os.name == "nt" else "opencode")
    executable.write_bytes(b"changed artifact")
    (output / "build.json").write_text(json.dumps({
        "method": "source", "source_commit": manifest["commit"], "source_tag": manifest["tag"],
        "version": "2.2.5", "executable_sha256": "wrong-hash",
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(prepare, "run", lambda command, **kwargs: calls.append(command))

    def stop_before_build(*args):
        raise RuntimeError("build requested")

    monkeypatch.setattr(prepare, "ensure_bun", stop_before_build)
    with pytest.raises(RuntimeError, match="build requested"):
        prepare.build_runtime(manifest, root / manifest["checkout"], {})
    assert calls == []


def test_source_build_uses_local_toolchain_and_writes_matching_provenance(tmp_path, monkeypatch):
    root, manifest = project(tmp_path, monkeypatch)
    source = root / manifest["checkout"]
    source.mkdir(parents=True)
    (source / "package.json").write_text(json.dumps({"packageManager": "bun@1.4.4"}), encoding="utf-8")
    bun = root / "data/agents/build-tools/node_modules/bun/bin/bun.exe"
    bun.parent.mkdir(parents=True)
    bun.write_bytes(b"local-bun")
    monkeypatch.setattr(prepare, "ensure_bun", lambda manifest, env: bun)
    verified = []
    monkeypatch.setattr(prepare, "verify_source", lambda manifest, env: verified.append(manifest["commit"]))
    name = "opencode.exe" if prepare.os.name == "nt" else "opencode"
    artifact = source / "packages/cli/dist/cli-test/bin" / name
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "packages/cli/script/build.ts" in command:
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"source-built-artifact")
        return SimpleNamespace(stdout="opencode v2.2.5\n")

    monkeypatch.setattr(prepare, "run", run)
    executable = prepare.build_runtime(manifest, source, {"PATH": "system-tools"})
    assert calls[0][0][:3] == [bun, "--no-env-file", "install"]
    assert {"--frozen-lockfile", "--ignore-scripts"}.issubset(calls[0][0])
    assert calls[1][0] == [bun, "--no-env-file", "run", "packages/cli/script/build.ts", "--single", "--skip-install", "--skip-web-ui"]
    assert calls[1][1]["env"]["PATH"].split(prepare.os.pathsep)[0] == str(bun.parent)
    assert verified == [manifest["commit"]]
    assert executable.read_bytes() == artifact.read_bytes()
    metadata = json.loads((executable.parent / "build.json").read_text(encoding="utf-8"))
    assert metadata["method"] == "source"
    assert metadata["source_commit"] == manifest["commit"]
    assert metadata["source_tag"] == manifest["tag"]
    assert metadata["bun_version"] == manifest["bun_version"]
    assert metadata["executable_sha256"] == prepare.sha256(executable)
    assert not (executable.parent / "build.json.new").exists()


@pytest.mark.parametrize("output", ["opencode v2.2.5\n", "2.2.5", "v2.2.5"])
def test_cli_version_supports_actual_v2_banner(output):
    assert prepare.cli_version(output) == "2.2.5"


def test_fresh_isolated_npm_configuration_can_start_npm(tmp_path, monkeypatch):
    import shutil
    import subprocess
    project(tmp_path, monkeypatch)
    npm = shutil.which("npm.cmd" if prepare.os.name == "nt" else "npm")
    if not npm:
        pytest.skip("Node.js/npm not installed")
    result = subprocess.run([npm, "--version"], env=prepare.build_environment(), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
