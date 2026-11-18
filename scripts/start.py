"""一键本地启动脚本。

由根目录 start.cmd 双击触发，依次完成：

1. 创建/复用虚拟环境（venv），并按 requirements.txt 变更自动安装依赖；
2. 缺失时从 .env.example 生成 .env；
3. 检查 Web 构建产物 web/dist，缺失且本机有 npm 时自动构建；
4. 检查 Ollama 服务与默认 Embedding 模型是否就绪（只警告，不阻塞）；
5. 选择可用端口（默认 8080，可用 APP_PORT 覆盖，被占用时自动向后寻找）；
6. 以 0.0.0.0 启动 uvicorn，等待 /api/v1/health 通过；
7. 打印本机与局域网访问地址，并尝试打开默认浏览器。

停止方式：在本窗口按 Ctrl+C。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# 控制台代码页由 start.cmd 切到 65001；重定向到文件时也统一按 UTF-8 输出，
# 避免中文在不同 Windows 代码页下乱码或抛出编码错误。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = BASE_DIR / "venv"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"
ENV_FILE = BASE_DIR / ".env"
WEB_DIR = BASE_DIR / "web"
WEB_DIST_DIR = WEB_DIR / "dist"
DEFAULT_PORT = 8080
MAX_PORT_OFFSET = 20
HEALTH_TIMEOUT_SECONDS = 120
OLLAMA_TIMEOUT_SECONDS = 2
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text-v2-moe"

if os.name == "nt":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"


def _info(message: str) -> None:
    print(f"[启动脚本] {message}", flush=True)


def _warn(message: str) -> None:
    print(f"[启动脚本] [警告] {message}", flush=True)


def _fail(message: str) -> None:
    print(f"[启动脚本] [失败] {message}", flush=True)


def _run(command: list[str], cwd: Path | None = None) -> int:
    """执行子命令并实时透传输出，返回退出码。"""
    _info(f"执行: {' '.join(str(part) for part in command)}")
    arguments = [str(part) for part in command]
    # Windows ships npm as npm.CMD; CreateProcess does not search .CMD for a
    # bare executable name. Resolve it without introducing a shell command.
    arguments[0] = shutil.which(arguments[0]) or arguments[0]
    try:
        completed = subprocess.run(arguments, cwd=str(cwd) if cwd else None)
        return completed.returncode
    except OSError as exc:
        _fail(f"无法运行 {command[0]}（{type(exc).__name__}）")
        return 1


def _venv_python() -> Path:
    return VENV_PYTHON


def _running_inside_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def _ensure_venv() -> None:
    """缺失虚拟环境时创建，并让新环境中的自己重新运行本脚本。"""
    if _venv_python().exists():
        return
    _info("未检测到虚拟环境，正在创建 venv ...")
    if _run([sys.executable, "-m", "venv", str(VENV_DIR)]) != 0:
        _fail("虚拟环境创建失败，请确认 Python 3.11 已正确安装。")
        raise SystemExit(1)
    _info("虚拟环境已创建，切换到虚拟环境继续启动 ...")
    os.execv(str(_venv_python()), [str(_venv_python()), str(Path(__file__).resolve())])


def _requirements_fingerprint() -> str:
    digest = hashlib.sha256()
    with open(REQUIREMENTS_FILE, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def _install_dependencies() -> None:
    """按 requirements.txt 指纹判断是否需要安装/重装依赖。"""
    if not REQUIREMENTS_FILE.exists():
        _warn("未找到 requirements.txt，跳过依赖安装。")
        return
    marker = VENV_DIR / ".requirements.sha256"
    fingerprint = _requirements_fingerprint()
    if marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == fingerprint:
                _info("依赖已是最新，跳过安装。")
                return
        except OSError:
            pass
    _info("正在安装 Python 依赖（首次约需数分钟，请耐心等待）...")
    if _run([_venv_python(), "-m", "pip", "install", "--upgrade", "pip", "-q"]) != 0:
        _fail("pip 升级失败。")
        raise SystemExit(1)
    if _run([_venv_python(), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE), "-q"]) != 0:
        _fail("依赖安装失败，请检查网络后重试。")
        raise SystemExit(1)
    try:
        marker.write_text(fingerprint, encoding="utf-8")
    except OSError:
        pass
    _info("依赖安装完成。")


def _ensure_env_file() -> None:
    if ENV_FILE.exists():
        return
    if not ENV_EXAMPLE_FILE.exists():
        _warn("未找到 .env 或 .env.example，将使用内置默认配置启动。")
        return
    shutil.copyfile(ENV_EXAMPLE_FILE, ENV_FILE)
    _info("已从 .env.example 生成 .env（密钥类配置留空时将自动生成）。")


def _load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)


def _web_fingerprint() -> str:
    digest = hashlib.sha256()
    files = list((WEB_DIR / "src").rglob("*"))
    files.extend(WEB_DIR / name for name in (
        "package.json", "package-lock.json", "index.html", "vite.config.ts",
        "tsconfig.json", "tsconfig.node.json",
    ))
    for path in sorted(path for path in files if path.is_file()):
        digest.update(path.relative_to(WEB_DIR).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _ensure_web_dist() -> bool:
    """确保前端构建产物存在；缺失时尝试用 npm 构建。返回是否可用。"""
    marker = WEB_DIST_DIR / ".source.sha256"
    fingerprint = _web_fingerprint()
    if (WEB_DIST_DIR / "index.html").exists() and marker.is_file() and marker.read_text().strip() == fingerprint:
        return True
    if shutil.which("npm") is None:
        _warn(
            "未找到 web/dist 构建产物，且本机没有 npm。"
            "后端与接口文档仍可访问（/docs），Web 界面需要先在 web/ 目录执行 "
            "npm install && npm run build。"
        )
        return False
    if not (WEB_DIR / "package.json").exists():
        _warn("web/ 目录不完整，跳过前端构建。")
        return False
    _info("未找到 web/dist，正在构建前端（首次约需数分钟）...")
    if _run(["npm", "ci"], cwd=WEB_DIR) != 0:
        _fail("前端依赖安装失败，Web 界面暂不可用。")
        return False
    if _run(["npm", "run", "build"], cwd=WEB_DIR) != 0:
        _fail("前端构建失败，Web 界面暂不可用。")
        return False
    _info("前端构建完成。")
    marker.write_text(fingerprint, encoding="utf-8")
    return (WEB_DIST_DIR / "index.html").exists()


def _http_get_json(url: str, timeout: float) -> tuple[int, object] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _check_ollama() -> None:
    """检查 Ollama 与默认 Embedding 模型；只警告，不阻塞启动。"""
    from app.bootstrap.container import get_provider_container

    container = get_provider_container()
    required = sorted({
        provider.identity().model for provider in (
            container.llm, container.rewrite_llm, container.router_llm,
            container.embedding,
        ) if provider.identity().provider == "ollama" and provider.identity().model
    })
    if not required:
        _info("已使用远程模型配置，无需本机 Ollama。")
        return
    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    embedding_model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    result = _http_get_json(f"{base_url}/api/tags", OLLAMA_TIMEOUT_SECONDS)
    if result is None:
        _warn(
            f"未检测到 Ollama 服务（{base_url}）。"
            "默认 Embedding 依赖本地 Ollama，请先安装并运行 Ollama：\n"
            f"    ollama pull {embedding_model}\n"
            "    ollama pull qwen3:4b\n"
            "（也可以在 .env 中把 EMBEDDING_PROVIDER 改为 openai_compatible 使用远程服务。）"
        )
        return
    _, payload = result
    if not isinstance(payload, dict):
        _warn("Ollama 模型列表响应格式无效。")
        return
    models = [
        item.get("name", "")
        for item in payload.get("models", [])
        if isinstance(item, dict)
    ]
    installed = {name if ":" in name.rsplit("/", 1)[-1] else name + ":latest" for name in models}
    missing = [name for name in required if (name if ":" in name.rsplit("/", 1)[-1] else name + ":latest") not in installed]
    for model in missing:
        _warn(f"缺少模型 {model}，请执行: ollama pull {model}")
    if not missing:
        _info("Ollama 所需模型已安装。")


def _ensure_agent_runtime() -> bool:
    from app.agents.settings import codecode_env, normalized_backend
    if normalized_backend(os.getenv("AGENT_BACKEND", "codecode")) != "codecode" or codecode_env("AUTO_START", "true").lower() not in {"1", "true", "yes"}:
        return True
    from app.agents.runtime import ensure_project_runtime, runtime_version
    from app.agents.settings import CODECODE_VERSION, default_settings, managed_endpoint

    try:
        if not managed_endpoint(default_settings().base_url):
            return True
        executable = ensure_project_runtime()
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and runtime_version(result.stdout) == CODECODE_VERSION:
            return True
        _warn(f"项目 codecode 版本校验未通过，要求 {CODECODE_VERSION}。")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        _warn(f"项目源码运行时尚未准备好（{type(exc).__name__}）；不会调用本机已有 codecode。请运行 python -m scripts.prepare_codecode --install-runtime。")
    return False


def _pick_port() -> int:
    requested = os.getenv("APP_PORT", "").strip()
    try:
        port = int(requested) if requested else DEFAULT_PORT
    except ValueError:
        _warn(f"APP_PORT={requested!r} 不是有效端口号，改用默认端口 {DEFAULT_PORT}。")
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        _fail("APP_PORT 须在 1–65535 之间。")
        raise SystemExit(1)
    for offset in range(MAX_PORT_OFFSET + 1):
        candidate = port + offset
        if candidate > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                if offset:
                    _info(f"端口 {port} 已被占用，改用端口 {candidate}。")
                return candidate
    _fail(f"端口 {port} 至 {port + MAX_PORT_OFFSET} 均被占用，请设置 APP_PORT 后重试。")
    raise SystemExit(1)


def _lan_ip() -> str:
    """取本机在局域网中的 IP（UDP connect 不实际发包，离线也可用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        except OSError:
            pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _wait_health(port: int, process=None) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/api/v1/health"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        result = _http_get_json(url, 2)
        if result is not None and result[0] == 200:
            return True
        time.sleep(1)
    return False


def _print_banner(port: int) -> None:
    lan_ip = _lan_ip()
    lines = [
        "",
        "=" * 64,
        "  RAGFlow 启动成功",
        f"  本机访问:   http://127.0.0.1:{port}",
        f"  局域网访问: http://{lan_ip}:{port}",
        f"  接口文档:   http://127.0.0.1:{port}/docs",
        "  停止服务:   在本窗口按 Ctrl+C",
        "=" * 64,
        "",
    ]
    print("\n".join(lines), flush=True)


def _open_browser(port: int, web_available: bool = True) -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{port}" + ("" if web_available else "/docs"))
    except Exception:
        pass


def main() -> int:
    _ensure_venv()
    if not _running_inside_venv():
        # venv 由外部 bootstrap 创建但当前不在其中时，切换到 venv 重跑。
        if _venv_python().exists():
            _info("切换到虚拟环境继续启动 ...")
            os.execv(str(_venv_python()), [str(_venv_python()), str(Path(__file__).resolve())])

    _install_dependencies()
    _ensure_env_file()
    _load_environment()
    web_available = _ensure_web_dist()
    _ensure_agent_runtime()
    _check_ollama()

    port = _pick_port()
    env = dict(os.environ)
    env.setdefault("APP_PROFILE", "local")
    env["APP_PORT"] = str(port)

    command = [
        str(_venv_python()),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    _info(f"正在启动服务: {' '.join(command)}")
    process = subprocess.Popen(command, cwd=str(BASE_DIR), env=env)

    _info("等待服务就绪（首次启动含数据库迁移，可能需要几十秒）...")
    if not _wait_health(port, process):
        _fail("服务未在预期时间内就绪，请查看上方日志。")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        return 1

    _print_banner(port)
    readiness = _http_get_json(f"http://127.0.0.1:{port}/api/v1/ready", 10)
    if readiness and isinstance(readiness[1], dict):
        failed = [name for name, item in readiness[1].get("components", {}).items() if item.get("status") != "ok"]
        if failed:
            _warn("以下组件尚未就绪：" + "、".join(failed) + "。请查看网页运行状态。")
    else:
        _warn("无法获取组件诊断，请查看网页运行状态。")
    _info("首次登录账号由 INITIAL_ADMIN_USERNAME 设置（默认 admin）；密码见项目 .env 中 INITIAL_ADMIN_PASSWORD。")
    _info("进程已启动；请在“运行状态”查看组件诊断，模型实际推理须通过问答验收。")
    _open_browser(port, web_available)

    try:
        return process.wait()
    except KeyboardInterrupt:
        _info("收到停止指令，正在关闭服务 ...")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
