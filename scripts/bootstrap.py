from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = "syncsage.yaml"
DEFAULT_EXAMPLE = "syncsage.example.yaml"
DEFAULT_COMPOSE_ENV = ".syncsage/compose.env"
DEFAULT_MCP_CONFIG = ".vscode/mcp.json"
DEFAULT_API_BASE = "http://localhost:8765"
ENV_ORDER = (
    "SYNCSAGE_IMAGE",
    "SYNCSAGE_CONFIG_PATH",
    "SYNCSAGE_WORKSPACE_PATH",
    "SYNCSAGE_VAULT_PATH",
)


def log(message: str) -> None:
    print(f"[syncsage] {message}", flush=True)


def command_text(args: list[str]) -> str:
    return subprocess.list2cmdline(args) if platform.system() == "Windows" else " ".join(args)


def run(args: list[str], *, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    log(f"$ {command_text(args)}")
    return subprocess.run(args, cwd=cwd, check=check)


def venv_python(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_syncsage(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "syncsage.exe"
    return venv / "bin" / "syncsage"


def ensure_config(config: Path, example: Path) -> None:
    if config.exists():
        log(f"Using existing config: {config.relative_to(ROOT)}")
        return
    if not example.exists():
        raise FileNotFoundError(f"Example config does not exist: {example}")
    config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, config)
    log(f"Created {config.relative_to(ROOT)} from {example.relative_to(ROOT)}")


def install_dependencies(venv: Path, *, no_uv: bool) -> Path:
    venv_py = venv_python(venv)
    uv = shutil.which("uv")
    if uv and not no_uv:
        try:
            if not venv_py.exists():
                run([uv, "venv", str(venv)])
            run([uv, "pip", "install", "--python", str(venv_py), "-e", ".[mcp]"])
            return venv_py
        except subprocess.CalledProcessError:
            log("uv install failed; falling back to stdlib venv and pip")

    if not venv_py.exists():
        run([sys.executable, "-m", "venv", str(venv)])
    else:
        log(f"Using existing virtual environment: {venv}")
    run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(venv_py), "-m", "pip", "install", "-e", ".[mcp]"])
    return venv_py


def render_local_files(venv_py: Path, config: Path, compose_env: Path, mcp_config: Path) -> None:
    compose_env.parent.mkdir(parents=True, exist_ok=True)
    mcp_config.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(venv_py),
            "-m",
            "syncsage",
            "compose-env",
            str(config),
            "--output",
            str(compose_env),
        ]
    )
    run(
        [
            str(venv_py),
            "-m",
            "syncsage",
            "client-config",
            "vscode",
            "--output",
            str(mcp_config),
        ]
    )


def validate_config(venv_py: Path, config: Path) -> None:
    run([str(venv_py), "-m", "syncsage", "validate", str(config), "--no-require-paths"])


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key.strip()] = value
    return values


def quote_env_value(value: str) -> str:
    if value and not any(char.isspace() or char in "#'\"" for char in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_file(path: Path, values: dict[str, str]) -> None:
    ordered = [key for key in ENV_ORDER if key in values]
    ordered.extend(sorted(set(values) - set(ordered)))
    path.write_text(
        "".join(f"{key}={quote_env_value(values[key])}\n" for key in ordered),
        encoding="utf-8",
    )


def resolve_host_path(value: str, *, root: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def ensure_host_paths(env: dict[str, str]) -> tuple[Path, Path]:
    workspace = resolve_host_path(env.get("SYNCSAGE_WORKSPACE_PATH", "./workspace"), root=ROOT)
    vault = resolve_host_path(env.get("SYNCSAGE_VAULT_PATH", "./vault"), root=ROOT)
    for path in (
        workspace,
        workspace / "repository",
        workspace / "documents",
        workspace / "obsidian-vault",
        vault,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return workspace, vault


def ensure_docker() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is not on PATH. Install Docker Desktop or Docker Engine first.")
    run(["docker", "--version"])
    run(["docker", "compose", "version"])


def compose(args: list[str], compose_env: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "--env-file", str(compose_env), *args], check=check)


def build_image(image: str) -> None:
    run(["docker", "build", "-t", image, "."])


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 10,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def wait_for_api(api_base: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            request_json(f"{api_base}/health")
            request_json(f"{api_base}/ready")
            return
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.HTTPException,
            OSError,
        ) as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"SyncSage API did not become ready: {last_error}")


def post_start_actions(
    api_base: str,
    *,
    skip_sync: bool,
    skip_export: bool,
    action_timeout: int,
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    if not skip_sync:
        results["sync"] = request_json(
            f"{api_base}/sync",
            method="POST",
            payload={"mode": "incremental"},
            timeout=action_timeout,
        )
    if not skip_export:
        results["obsidian"] = request_json(
            f"{api_base}/obsidian/export",
            method="POST",
            payload={},
            timeout=action_timeout,
        )
    return results


def print_summary(
    *,
    api_base: str,
    config: Path,
    compose_env: Path,
    mcp_config: Path,
    workspace: Path,
    vault: Path,
    venv: Path,
    image: str,
    actions: dict[str, dict],
    api_checked: bool,
) -> None:
    index = vault / "SyncSage" / "Index.md"
    syncsage_cmd = venv_syncsage(venv)
    print()
    print("SyncSage is ready." if api_checked else "SyncSage local setup is ready.")
    suffix = "" if api_checked else " (not checked)"
    print(f"  API health:        {api_base}/health{suffix}")
    print(f"  API readiness:    {api_base}/ready{suffix}")
    print(f"  API docs:         {api_base}/docs{suffix}")
    print(f"  Config:           {config}")
    print(f"  Compose env:      {compose_env}")
    print(f"  Image:            {image}")
    print(f"  Workspace mount:  {workspace} -> /workspace")
    print(f"  Obsidian vault:   {vault} -> /vault")
    print(f"  Obsidian index:   {index}")
    print(f"  VS Code MCP:      {mcp_config}")
    print(f"  Local CLI:        {syncsage_cmd} --help")
    if actions.get("sync"):
        print("  Initial sync:     complete")
    if actions.get("obsidian"):
        count = actions["obsidian"].get("count", 0)
        print(f"  Obsidian export:  {count} files written")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap a local SyncSage checkout.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--example", default=DEFAULT_EXAMPLE)
    parser.add_argument("--venv", default=".venv")
    parser.add_argument("--compose-env", default=DEFAULT_COMPOSE_ENV)
    parser.add_argument("--mcp-config", default=DEFAULT_MCP_CONFIG)
    parser.add_argument(
        "--image",
        help="Override SYNCSAGE_IMAGE, for example ghcr.io/esatt10/syncsage:latest.",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--ready-timeout", type=int, default=120)
    parser.add_argument("--action-timeout", type=int, default=600)
    parser.add_argument("--no-uv", action="store_true", help="Use stdlib venv/pip even if uv exists.")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument("--skip-up", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build the local checkout as the selected image instead of pulling it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = (ROOT / args.config).resolve()
    example = (ROOT / args.example).resolve()
    venv = (ROOT / args.venv).resolve()
    compose_env = (ROOT / args.compose_env).resolve()
    mcp_config = (ROOT / args.mcp_config).resolve()

    ensure_config(config, example)
    if args.skip_install:
        venv_py = venv_python(venv)
        if not venv_py.exists():
            raise RuntimeError(f"Virtual environment is missing: {venv}")
    else:
        venv_py = install_dependencies(venv, no_uv=args.no_uv)

    render_local_files(venv_py, config, compose_env, mcp_config)
    validate_config(venv_py, config)

    env = parse_env_file(compose_env)
    configured_image = env.get("SYNCSAGE_IMAGE", "")
    if args.image:
        env["SYNCSAGE_IMAGE"] = args.image
        write_env_file(compose_env, env)
    workspace, vault = ensure_host_paths(env)
    if not (args.skip_pull and args.skip_up):
        ensure_docker()

    if args.build:
        build_image(env["SYNCSAGE_IMAGE"])
    elif not args.skip_pull:
        pull = compose(["pull"], compose_env, check=False)
        if pull.returncode != 0:
            if args.image and args.image.endswith(":latest") and (ROOT / "Dockerfile").exists():
                log(
                    f"Image {args.image} is not available; "
                    "building local checkout as syncsage:local"
                )
                env["SYNCSAGE_IMAGE"] = "syncsage:local"
                write_env_file(compose_env, env)
                build_image(env["SYNCSAGE_IMAGE"])
            elif args.image and configured_image and configured_image != args.image:
                log(
                    f"Image {args.image or env['SYNCSAGE_IMAGE']} is not available; "
                    f"falling back to configured image {configured_image}"
                )
                env["SYNCSAGE_IMAGE"] = configured_image
                write_env_file(compose_env, env)
                compose(["pull"], compose_env)
            else:
                raise SystemExit(pull.returncode)
    if not args.skip_up:
        compose(["up", "-d"], compose_env)

    should_contact_api = not args.skip_up or not (args.skip_sync and args.skip_export)
    if should_contact_api:
        wait_for_api(args.api_base, args.ready_timeout)
        actions = post_start_actions(
            args.api_base,
            skip_sync=args.skip_sync,
            skip_export=args.skip_export,
            action_timeout=args.action_timeout,
        )
    else:
        actions = {}
    print_summary(
        api_base=args.api_base,
        config=config,
        compose_env=compose_env,
        mcp_config=mcp_config,
        workspace=workspace,
        vault=vault,
        venv=venv,
        image=env.get("SYNCSAGE_IMAGE", ""),
        actions=actions,
        api_checked=should_contact_api,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
