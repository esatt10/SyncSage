from __future__ import annotations

import argparse
from pathlib import Path

from syncsage.config.loader import load_config, validate_source_paths
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.sync.engine import SyncEngine


def _engine(config_path: Path) -> SyncEngine:
    cfg = load_config(config_path)
    paths = StatePaths.from_config(cfg)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    return SyncEngine(cfg, paths, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syncsage", description="SyncSage knowledge graph indexing server")
    sub = parser.add_subparsers(dest="command")
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("config", nargs="?", default="syncsage.example.yaml")
    validate_p.add_argument("--no-require-paths", action="store_true")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--config", "-c", default="syncsage.example.yaml")
    sync_p.add_argument("--source", "-s")
    sync_p.add_argument("--all", action="store_true")
    sync_p.add_argument("--mode", default="incremental")
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--config", "-c", default="/config/syncsage.yaml")
    mcp_p = sub.add_parser("mcp")
    mcp_p.add_argument("--config", "-c", default="/config/syncsage.yaml")
    mcp_p.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), default="stdio")
    client_p = sub.add_parser("client-config")
    client_sub = client_p.add_subparsers(dest="client")
    vscode_p = client_sub.add_parser("vscode")
    vscode_p.add_argument("--mode", choices=("docker-exec", "docker-run"), default="docker-exec")
    vscode_p.add_argument("--server-name", default="syncsage")
    vscode_p.add_argument("--container-name", default="syncsage")
    vscode_p.add_argument("--image", default="ghcr.io/esatt10/syncsage:latest")
    vscode_p.add_argument("--output", "-o")
    repair_p = sub.add_parser("repair")
    repair_p.add_argument("--config", "-c", default="syncsage.example.yaml")
    args = parser.parse_args(argv)
    if args.command in {None, "--help"}:
        parser.print_help(); return 0
    if args.command == "validate":
        cfg = load_config(Path(args.config))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        if errors:
            for e in errors: print(f"ERROR: {e}")
            return 1
        print(f"Config valid: {args.config} ({len(cfg.sources)} sources)")
        return 0
    if args.command == "sync":
        engine = _engine(Path(args.config))
        results = (
            engine.sync_all(args.mode)
            if args.all or not args.source
            else [engine.sync_source(args.source, args.mode)]
        )
        for r in results:
            print(
                f"{r.source_id}: indexed={r.indexed_artifacts} "
                f"skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}"
            )
        return 0
    if args.command == "repair":
        _engine(Path(args.config)).sync_all("full"); print("Repair complete"); return 0
    if args.command == "serve":
        from syncsage.api.app import create_app
        import uvicorn
        cfg = load_config(Path(args.config))
        uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)
        return 0
    if args.command == "mcp":
        from syncsage.mcp_server.server import run_mcp_server
        cfg = load_config(Path(args.config))
        run_mcp_server(cfg, args.transport)
        return 0
    if args.command == "client-config":
        from syncsage.mcp_client.vscode import (
            docker_exec_stdio_config,
            docker_run_stdio_config,
            render_vscode_mcp_json,
        )
        if args.client != "vscode":
            client_p.print_help()
            return 1
        payload = (
            docker_exec_stdio_config(args.server_name, args.container_name)
            if args.mode == "docker-exec"
            else docker_run_stdio_config(args.server_name, args.image)
        )
        rendered = render_vscode_mcp_json(payload)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    return 1


def app() -> None:
    raise SystemExit(main())
