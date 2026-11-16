"""Fetch official source and compile a project-owned OpenCode native binary."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def cli_version(output: str) -> str:
    match = re.fullmatch(r"(?:opencode\s+v?|v)?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)", output.strip())
    if not match:
        raise ValueError("Unexpected OpenCode CLI version output")
    return match[1]


def inside_project(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("OpenCode preparation path escapes the project")
    return resolved


def build_environment() -> dict:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "SYSTEMDRIVE",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = inside_project(ROOT / "data/agents/build-home")
    home.mkdir(parents=True, exist_ok=True)
    for name in ("config", "data", "cache"):
        inside_project(home / name).mkdir(exist_ok=True)
    npm_user = inside_project(home / "config/npm-user.npmrc")
    npm_global = inside_project(home / "config/npm-global.npmrc")
    # npm rejects the same file serving as both global and user configuration,
    # including NUL on Windows. Two isolated empty files avoid that collision.
    npm_user.write_text("", encoding="utf-8")
    npm_global.write_text("", encoding="utf-8")
    env.update({"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "config"),
                "LOCALAPPDATA": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "NPM_CONFIG_USERCONFIG": str(npm_user), "NPM_CONFIG_GLOBALCONFIG": str(npm_global),
                "NPM_CONFIG_CACHE": str(home / "cache/npm"), "NPM_CONFIG_UPDATE_NOTIFIER": "false",
                "BUN_INSTALL_CACHE_DIR": str(home / "cache/bun"), "HUSKY": "0"})
    return env


def run(command, *, cwd=ROOT, env, capture=False, timeout=1800):
    return subprocess.run([str(value) for value in command], cwd=str(cwd), env=env, check=True,
                          capture_output=capture, text=capture, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def verify_source(manifest: dict, env: dict) -> Path:
    target = inside_project(ROOT / manifest["checkout"])
    if target.exists():
        remote = run(["git", "-C", target, "remote", "get-url", "origin"], env=env, capture=True).stdout.strip()
        if remote != manifest["repository"]:
            raise ValueError("Existing OpenCode checkout has a different source")
        if run(["git", "-C", target, "status", "--porcelain"], env=env, capture=True).stdout.strip():
            raise ValueError("OpenCode checkout contains local changes; refusing to overwrite")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-c", "credential.helper=", "clone", "--depth", "1", "--branch", manifest["tag"],
             manifest["repository"], target], env=env)
    actual = run(["git", "-C", target, "rev-parse", "HEAD"], env=env, capture=True).stdout.strip()
    if actual != manifest["commit"]:
        run(["git", "-C", target, "fetch", "--depth", "1", "origin", "tag", manifest["tag"]], env=env)
        run(["git", "-C", target, "checkout", "--detach", manifest["commit"]], env=env)
    actual = run(["git", "-C", target, "rev-parse", "HEAD"], env=env, capture=True).stdout.strip()
    if actual != manifest["commit"]:
        raise ValueError("OpenCode checkout did not match the pinned commit")
    print(f"Official OpenCode source verified: {manifest['tag']} {actual}", flush=True)
    return target


def ensure_bun(manifest: dict, env: dict) -> Path:
    tools = inside_project(ROOT / "data/agents/build-tools")
    bun = inside_project(tools / "node_modules/bun/bin/bun.exe")
    expected = manifest["bun_version"]
    try:
        valid = bun.is_file() and run([bun, "--version"], env=env, capture=True, timeout=15).stdout.strip() == expected
    except (OSError, subprocess.SubprocessError):
        valid = False
    if not valid:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise RuntimeError("Node.js/npm is required to prepare the project's Bun build tool")
        run([npm, "install", "--prefix", tools, "--save-exact", "--no-audit", "--no-fund", f"bun@{expected}"], env=env)
        # Only the official Bun package's project-local installer is invoked
        # if npm defers its postinstall under an explicit script policy.
        with bun.open("rb") as stream:
            signature = stream.read(4)
        native = signature[:2] == b"MZ" if os.name == "nt" else signature in {
            b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
        }
        if not native:
            node = shutil.which("node")
            if not node:
                raise RuntimeError("Node.js is required to finish the local Bun installation")
            run([node, inside_project(tools / "node_modules/bun/install.js")],
                cwd=inside_project(tools / "node_modules/bun"), env=env)
        if run([bun, "--version"], env=env, capture=True, timeout=15).stdout.strip() != expected:
            raise RuntimeError("The project-local Bun version does not match the source toolchain")
    return inside_project(bun)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_runtime(manifest: dict, source: Path, env: dict, force=False) -> Path:
    build_package = manifest.get("build_package", "packages/cli")
    build_flags = manifest.get("build_flags", ["--single", "--skip-install", "--skip-web-ui"])
    if build_package != "packages/cli" or build_flags != ["--single", "--skip-install", "--skip-web-ui"]:
        raise ValueError("Unsupported OpenCode V2 source-build layout or flags")
    output = inside_project(ROOT / manifest["runtime_directory"])
    executable = inside_project(output / ("opencode.exe" if os.name == "nt" else "opencode"))
    stamp = inside_project(output / "build.json")
    if not force and executable.is_file() and stamp.is_file():
        try:
            metadata = json.loads(stamp.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                metadata = {}
        except (OSError, ValueError):
            metadata = {}
        if (metadata.get("method") == "source" and metadata.get("source_commit") == manifest["commit"]
            and metadata.get("source_tag") == manifest["tag"] and metadata.get("version") == manifest["tag"].removeprefix("v")
            and metadata.get("bun_version") == manifest["bun_version"] and metadata.get("build_flags") == build_flags
            and metadata.get("executable_sha256") == sha256(executable)
            and cli_version(run([executable, "--version"], env=env, capture=True, timeout=15).stdout) == metadata["version"]):
            print(f"Verified project source-built runtime: {executable}", flush=True)
            return executable
    bun = ensure_bun(manifest, env)
    package_manager = json.loads((source / "package.json").read_text(encoding="utf-8"))["packageManager"]
    if package_manager != f"bun@{manifest['bun_version']}":
        raise ValueError("Pinned source and local Bun toolchain disagree")
    env = {**env, "PATH": str(bun.parent) + os.pathsep + env.get("PATH", ""),
           "OPENCODE_VERSION": manifest["tag"].removeprefix("v"), "OPENCODE_CHANNEL": "latest"}
    # The pinned upstream script removes packages/cli/dist. Check the
    # exact resolved target before executing that build script on Windows.
    dist = inside_project(source / build_package / "dist")
    run([bun, "--no-env-file", "install", "--frozen-lockfile", "--ignore-scripts",
         "--filter", "./" + build_package, "--network-concurrency", "32"], cwd=source, env=env)
    run([bun, "--no-env-file", "run", build_package + "/script/build.ts", *build_flags], cwd=source, env=env)
    name = "opencode.exe" if os.name == "nt" else "opencode"
    artifacts = list(dist.glob(f"cli-*/bin/{name}"))
    if len(artifacts) != 1:
        raise RuntimeError("The native source build did not produce exactly one platform binary")
    artifact = inside_project(artifacts[0])
    version = cli_version(run([artifact, "--version"], env=env, capture=True, timeout=30).stdout)
    if version != manifest["tag"].removeprefix("v"):
        raise RuntimeError("Compiled OpenCode version differs from the pinned source")
    verify_source(manifest, env)
    output.mkdir(parents=True, exist_ok=True)
    temporary = inside_project(output / (name + ".new"))
    shutil.copy2(artifact, temporary)
    temporary.replace(executable)
    metadata = {"method": "source", "repository": manifest["repository"], "source_tag": manifest["tag"],
                "source_commit": manifest["commit"], "version": version, "bun_version": manifest["bun_version"],
                "executable_sha256": sha256(executable), "built_at": datetime.now(timezone.utc).isoformat(),
                "headless": True, "build_package": build_package, "build_flags": build_flags}
    temporary_stamp = inside_project(output / "build.json.new")
    temporary_stamp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_stamp.replace(stamp)
    print(f"Project source-built OpenCode ready: {executable}", flush=True)
    return executable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-runtime", "--build-runtime", action="store_true", dest="build")
    parser.add_argument("--force-build", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "integrations/opencode/source.json").read_text(encoding="utf-8"))
    env = build_environment()
    source = verify_source(manifest, env)
    if args.build or args.force_build:
        build_runtime(manifest, source, env, force=args.force_build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
