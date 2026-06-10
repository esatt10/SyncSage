from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _sync_services(engine, cfg):
    """Build the watcher + scheduler pair sharing one sync serialization lock.

    SyncEngine is not safe for concurrent syncs within a process, so both
    background services funnel their sync calls through the same lock.
    """
    import threading

    from syncsage.sync.scheduler import SchedulerService
    from syncsage.sync.watcher import WatcherService

    sync_lock = threading.Lock()
    return (
        WatcherService(engine, cfg, sync_lock=sync_lock),
        SchedulerService(engine, cfg, sync_lock=sync_lock),
    )


def _serve_app(cfg, config_path: str) -> None:
    import uvicorn

    from syncsage.api.app import create_app

    app_obj = create_app(cfg, config_path=config_path)
    watcher, scheduler = _sync_services(app_obj.state.engine, cfg)
    watcher.start()
    scheduler.start()
    try:
        uvicorn.run(app_obj, host=cfg.server.host, port=cfg.server.port)
    finally:
        scheduler.stop()
        watcher.stop()
        app_obj.state.engine.close()


def _engine(config_path: Path):
    from syncsage.config.loader import load_config
    from syncsage.persistence.paths import StatePaths
    from syncsage.persistence.state_store import StateStore
    from syncsage.sync.engine import SyncEngine

    cfg = load_config(config_path)
    paths = StatePaths.from_config(cfg)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    return SyncEngine(cfg, paths, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="syncsage",
        description="SyncSage knowledge graph indexing server",
    )
    sub = parser.add_subparsers(dest="command")
    start_p = sub.add_parser("start")
    start_p.add_argument("--config", "-c", default="syncsage.yaml")
    start_p.add_argument("--profile", default="quickstart")
    start_p.add_argument("--set", dest="overrides", action="append", default=[])
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("config", nargs="?", default="syncsage.example.yaml")
    validate_p.add_argument("--no-require-paths", action="store_true")
    init_p = sub.add_parser("init")
    init_p.add_argument("--profile", default="quickstart")
    init_p.add_argument("--output", "-o", default="syncsage.yaml")
    init_p.add_argument("--force", action="store_true")
    config_p = sub.add_parser("config")
    config_sub = config_p.add_subparsers(dest="config_command")
    config_show_p = config_sub.add_parser("show")
    config_show_p.add_argument("--effective", action="store_true")
    config_show_p.add_argument("--config", "-c", default="syncsage.yaml")
    config_show_p.add_argument("--profile", default="quickstart")
    config_show_p.add_argument("--set", dest="overrides", action="append", default=[])
    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--config", "-c", default="syncsage.yaml")
    doctor_p.add_argument("--profile", default="quickstart")
    doctor_p.add_argument("--no-require-paths", action="store_true")
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
    vscode_p.add_argument("--image")
    vscode_p.add_argument("--output", "-o")
    compose_env_p = sub.add_parser("compose-env")
    compose_env_p.add_argument("config", nargs="?", default="syncsage.yaml")
    compose_env_p.add_argument("--output", "-o")
    repair_p = sub.add_parser("repair")
    repair_p.add_argument("--config", "-c", default="syncsage.example.yaml")
    args = parser.parse_args(argv)
    if args.command in {None, "--help"}:
        parser.print_help()
        return 0
    if args.command == "start":
        from syncsage.config.loader import load_layered_config, parse_override_pairs

        cfg = load_layered_config(
            Path(args.config),
            args.profile,
            parse_override_pairs(args.overrides),
        )
        _serve_app(cfg, args.config)
        return 0
    if args.command == "validate":
        from syncsage.config.loader import load_config, validate_source_paths

        cfg = load_config(Path(args.config))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"Config valid: {args.config} ({len(cfg.sources)} sources)")
        return 0
    if args.command == "init":
        from syncsage.config.loader import render_init_config

        output = Path(args.output)
        if output.exists() and not args.force:
            print(f"ERROR: {output} already exists. Use --force to overwrite.")
            return 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_init_config(args.profile), encoding="utf-8")
        print(f"Wrote {output}")
        return 0
    if args.command == "config":
        import yaml

        from syncsage.config.loader import (
            effective_config_dict,
            parse_override_pairs,
        )

        if args.config_command != "show":
            config_p.print_help()
            return 1
        payload = effective_config_dict(
            Path(args.config),
            args.profile,
            parse_override_pairs(args.overrides),
        )
        print(yaml.safe_dump(payload, sort_keys=False), end="")
        return 0
    if args.command == "doctor":
        from syncsage.config.loader import (
            load_layered_config,
            parse_override_pairs,
            validate_source_paths,
        )

        cfg = load_layered_config(Path(args.config), args.profile, parse_override_pairs([]))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        for path in [
            cfg.syncsage.state_path,
            cfg.syncsage.vault_path,
            cfg.syncsage.exports_path,
        ]:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"cannot create {path}: {exc}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(
            f"Doctor ok: profile={args.profile} "
            f"sources={len(cfg.sources)} transports={cfg.server.mcp.transports}"
        )
        return 0
    if args.command == "sync":
        from syncsage.sync.locks import EngineLeaseError

        engine = _engine(Path(args.config))
        try:
            results = (
                engine.sync_all(args.mode)
                if args.all or not args.source
                else [engine.sync_source(args.source, args.mode)]
            )
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        for r in results:
            print(
                f"{r.source_id}: indexed={r.indexed_artifacts} "
                f"skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}"
            )
        return 0
    if args.command == "repair":
        from syncsage.sync.locks import EngineLeaseError

        engine = _engine(Path(args.config))
        try:
            engine.sync_all("repair")
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        print("Repair complete")
        return 0
    if args.command == "serve":
        from syncsage.config.loader import load_config

        cfg = load_config(Path(args.config))
        _serve_app(cfg, args.config)
        return 0
    if args.command == "mcp":
        from syncsage.config.loader import load_config
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
    if args.command == "compose-env":
        from syncsage.deployment.compose_env import load_compose_environment, render_env_file

        rendered = render_env_file(load_compose_environment(args.config))
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
