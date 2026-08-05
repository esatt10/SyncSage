from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print_scan(report: dict) -> None:
    """Human-readable pre-flight for one source.

    The point of the depth table is that a depth cap should be picked from
    evidence — "depth 2 gets me 900 files, depth 3 gets me 40,000" — rather
    than guessed and then discovered by an OOM.
    """
    name = report.get("source_id")
    if not report.get("scannable"):
        print(f"{name}: not scannable — {report.get('reason')}")
        return
    print(f"{name} ({report.get('path')})")
    print(
        f"  would index {report['file_count']} files, {report['total_mb']} MB "
        f"(scanned {report['entries_scanned']} entries, "
        f"pruned {report['directories_pruned']} directories)"
    )
    subtree = report.get("bytes_by_subtree") or {}
    if subtree:
        top = ", ".join(
            f"{key} ({value // (1024 * 1024)} MB)" for key, value in list(subtree.items())[:5]
        )
        print(f"  largest subtrees: {top}")
    options = report.get("depth_options") or []
    if len(options) > 1:
        table = "  ".join(f"depth {o['max_depth']}={o['files']}" for o in options[:8])
        print(f"  files by depth cap: {table}")
    if report.get("oversized_count"):
        print(f"  {report['oversized_count']} file(s) skipped as oversized")
    if report.get("would_exceed"):
        print(f"  WOULD BE REFUSED: exceeds {', '.join(report['would_exceed'])}")
        print("  narrow with --depth / include / exclude, raise sync.limits, or use --full-scan")
    else:
        print("  within configured limits — sync would proceed")


def _sync_services(engine, cfg, config_path=None):
    """Build the watcher + scheduler pair sharing one sync serialization lock.

    SyncEngine is not safe for concurrent syncs within a process, so both
    background services funnel their sync calls through the same lock.
    """
    import threading

    from pheasant.sync.scheduler import SchedulerService
    from pheasant.sync.watcher import WatcherService

    sync_lock = threading.Lock()
    if config_path is not None:
        # Serving: background syncs run in a child process, so a scheduled
        # re-index never competes with queries for the GIL.
        from pheasant.sync.worker import WorkerBackedEngine

        engine = WorkerBackedEngine(engine, config_path)
    return (
        WatcherService(engine, cfg, sync_lock=sync_lock),
        SchedulerService(engine, cfg, sync_lock=sync_lock),
    )


def _report_ui(app_obj, cfg) -> None:
    """Say whether the web UI is being served, and how to get it if not.

    Going from "the CLI works" to "I can see the graph" is the step people get
    stuck on: the API answers on the port either way, so a missing bundle is
    otherwise silent.
    """
    host = "localhost" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    if getattr(app_obj.state, "ui_dist", None):
        print(f"Web UI:      http://{host}:{cfg.server.port}")
    elif cfg.server.ui.enabled:
        print(
            "Web UI:      not served — no built bundle found.\n"
            "             Build it with `npm --prefix ui ci && npm --prefix ui run build`,\n"
            "             set PHEASANT_UI_DIST to an existing one, or run the container\n"
            "             sidecar (`pheasant host`). See docs/how-to/run-the-ui.md."
        )


def _serve_app(cfg, config_path: str, *, report_ui: bool = True) -> None:
    import uvicorn

    from pheasant.api.app import create_app

    app_obj = create_app(cfg, config_path=config_path)
    if report_ui:
        _report_ui(app_obj, cfg)
    watcher, scheduler = _sync_services(app_obj.state.engine, cfg, config_path=config_path)
    watcher.start()
    scheduler.start()
    try:
        uvicorn.run(app_obj, host=cfg.server.host, port=cfg.server.port)
    finally:
        scheduler.stop()
        watcher.stop()
        app_obj.state.engine.close()


def _engine(config_path: Path):
    from pheasant.config.loader import load_config
    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import StateStore
    from pheasant.sync.engine import SyncEngine

    cfg = load_config(config_path)
    paths = StatePaths.from_config(cfg)
    paths.ensure()
    state = StateStore(paths.sqlite)
    state.migrate()
    return SyncEngine(cfg, paths, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pheasant",
        description="pheasant knowledge graph indexing server",
    )
    sub = parser.add_subparsers(dest="command")
    up_p = sub.add_parser(
        "up",
        help="zero-config quickstart: detect, generate config, index, serve",
    )
    up_p.add_argument(
        "path",
        nargs="*",
        default=["."],
        help=(
            "one or more targets: a folder, file, glob (~/clients/*), git URL, "
            "web URL, s3:// bucket, or connector (notion:workspace)"
        ),
    )
    up_p.add_argument("--config", "-c", default="pheasant.yaml")
    up_p.add_argument("--name", help="knowledge-base name [default: slug of the directory]")
    up_p.add_argument("--port", type=int, default=8765)
    up_p.add_argument("--profile", default="quickstart")
    up_p.add_argument("--mode", default="incremental")
    up_p.add_argument(
        "--split",
        action="store_true",
        help="index each immediate subdirectory of a target as its own source",
    )
    up_p.add_argument(
        "--no-serve",
        action="store_true",
        help="generate + index only; do not start the server",
    )
    host_p = sub.add_parser(
        "host",
        help="one-line container hosting: generate config + compose file, then run it",
    )
    host_p.add_argument("path", nargs="*", default=["."])
    host_p.add_argument("--config", "-c", default="pheasant.yaml")
    host_p.add_argument("--name")
    host_p.add_argument("--port", type=int, default=8765)
    host_p.add_argument("--ui-port", type=int, default=8080)
    host_p.add_argument("--profile", default="quickstart")
    host_p.add_argument("--split", action="store_true")
    host_p.add_argument("--output", "-o", default="docker-compose.pheasant.yml")
    host_p.add_argument("--image")
    host_p.add_argument("--ui-image")
    host_p.add_argument("--no-ui", action="store_true", help="omit the web UI sidecar")
    host_p.add_argument(
        "--print-only",
        action="store_true",
        help="write the compose file but do not run docker compose",
    )
    start_p = sub.add_parser("start")
    start_p.add_argument("--config", "-c", default="pheasant.yaml")
    start_p.add_argument("--profile", default="quickstart")
    start_p.add_argument("--set", dest="overrides", action="append", default=[])
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("config", nargs="?", default="pheasant.example.yaml")
    validate_p.add_argument("--no-require-paths", action="store_true")
    init_p = sub.add_parser("init")
    init_p.add_argument("--profile", default="quickstart")
    init_p.add_argument("--output", "-o", default="pheasant.yaml")
    init_p.add_argument("--force", action="store_true")
    config_p = sub.add_parser("config")
    config_sub = config_p.add_subparsers(dest="config_command")
    config_show_p = config_sub.add_parser("show")
    config_show_p.add_argument("--effective", action="store_true")
    config_show_p.add_argument("--config", "-c", default="pheasant.yaml")
    config_show_p.add_argument("--profile", default="quickstart")
    config_show_p.add_argument("--set", dest="overrides", action="append", default=[])
    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--config", "-c", default="pheasant.yaml")
    doctor_p.add_argument("--profile", default="quickstart")
    doctor_p.add_argument("--no-require-paths", action="store_true")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    sync_p.add_argument("--source", "-s")
    sync_p.add_argument("--all", action="store_true")
    sync_p.add_argument("--mode", default="incremental")
    sync_p.add_argument(
        "--depth",
        type=int,
        default=None,
        help="cap directory depth for this run (0 = only files directly in the source root)",
    )
    sync_p.add_argument(
        "--full-scan",
        action="store_true",
        help="index everything: no depth cap and no size budget (sync.limits is ignored)",
    )
    sync_p.add_argument(
        "--json",
        action="store_true",
        help="emit the result as one JSON object (how the server's sync worker reports back)",
    )
    scan_p = sub.add_parser(
        "scan",
        help="estimate what a source would index — file count, size, depth options — "
        "without indexing anything",
    )
    scan_p.add_argument("--config", "-c", default="pheasant.yaml")
    scan_p.add_argument("--source", "-s", help="source to scan (default: every enabled source)")
    scan_p.add_argument("--depth", type=int, default=None, help="cap directory depth for the scan")
    scan_p.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--config", "-c", default="/config/pheasant.yaml")
    mcp_p = sub.add_parser("mcp")
    mcp_p.add_argument("--config", "-c", default="/config/pheasant.yaml")
    mcp_p.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), default="stdio")
    client_p = sub.add_parser("client-config")
    client_sub = client_p.add_subparsers(dest="client")
    vscode_p = client_sub.add_parser("vscode")
    vscode_p.add_argument("--mode", choices=("docker-exec", "docker-run"), default="docker-exec")
    vscode_p.add_argument("--server-name", default="pheasant")
    vscode_p.add_argument("--container-name", default="pheasant")
    vscode_p.add_argument("--image")
    vscode_p.add_argument("--output", "-o")
    for agent_name in ("claude-code", "cursor"):
        agent_p = client_sub.add_parser(
            agent_name, help=f"emit an mcpServers config for {agent_name}"
        )
        agent_p.add_argument(
            "--mode", choices=("local", "docker-exec", "docker-run"), default="local"
        )
        agent_p.add_argument("--server-name", default="pheasant")
        agent_p.add_argument("--config", "-c", default="pheasant.yaml")
        agent_p.add_argument("--container-name", default="pheasant")
        agent_p.add_argument("--image")
        agent_p.add_argument("--output", "-o")
    compose_env_p = sub.add_parser("compose-env")
    compose_env_p.add_argument("config", nargs="?", default="pheasant.yaml")
    compose_env_p.add_argument("--output", "-o")
    repair_p = sub.add_parser("repair")
    repair_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    backup_p = sub.add_parser("backup")
    backup_p.add_argument("output")
    backup_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    restore_p = sub.add_parser("restore")
    restore_p.add_argument("input")
    restore_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    restore_p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {None, "--help"}:
        parser.print_help()
        return 0
    if args.command == "up":
        from pheasant.quickstart import ensure_up_config, state_root
        from pheasant.sync.locks import EngineLeaseError
        from pheasant.targets import TargetError, fetch_target, resolve_targets

        config_path = Path(args.config)
        specs = args.path if isinstance(args.path, list) else [args.path]
        if not specs:
            specs = ["."]
        local_state = state_root(config_path)
        try:
            targets = resolve_targets(
                specs,
                clone_root=local_state / "sources",
                workspace=local_state / "external",
                split=args.split,
                name=args.name,
            )
            for target in targets:
                status = fetch_target(target)
                if status:
                    print(status)
        except TargetError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        created = ensure_up_config(
            targets, config_path, name=args.name, port=args.port, profile=args.profile
        )
        if created:
            print(f"Wrote {config_path}")
            for target in targets:
                print(f"  + {target.name} ({target.type}) <- {target.path}")
        else:
            print(f"Reusing existing {config_path} (never overwritten)")
        engine = _engine(config_path)
        try:
            results = engine.sync_all(args.mode)
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
        if args.no_serve:
            print(f"Ready. Start the server with: pheasant start -c {config_path}")
            print(
                "Attach to a coding agent: "
                f"pheasant client-config claude-code -c {config_path} -o .mcp.json"
            )
            return 0
        from pheasant.config.loader import load_config

        cfg = load_config(config_path)
        print(f"API + MCP:   http://{cfg.server.host}:{cfg.server.port}")
        _serve_app(cfg, str(config_path))
        return 0
    if args.command == "host":
        from pheasant.deployment.host import host_stack
        from pheasant.targets import TargetError

        try:
            return host_stack(args)
        except TargetError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if args.command == "start":
        from pheasant.config.loader import load_layered_config, parse_override_pairs

        cfg = load_layered_config(
            Path(args.config),
            args.profile,
            parse_override_pairs(args.overrides),
        )
        _serve_app(cfg, args.config)
        return 0
    if args.command == "validate":
        from pheasant.config.loader import load_config, validate_source_paths

        cfg = load_config(Path(args.config))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"Config valid: {args.config} ({len(cfg.sources)} sources)")
        return 0
    if args.command == "init":
        from pheasant.config.loader import render_init_config

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

        from pheasant.config.loader import (
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
        from pheasant.config.loader import (
            load_layered_config,
            parse_override_pairs,
            validate_source_paths,
        )

        cfg = load_layered_config(Path(args.config), args.profile, parse_override_pairs([]))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        for path in [
            cfg.pheasant.state_path,
            cfg.pheasant.vault_path,
            cfg.pheasant.exports_path,
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
        from pheasant.sync.locks import EngineLeaseError

        engine = _engine(Path(args.config))
        try:
            # This process owns the CPU cost of indexing, so it also owns
            # building the graph search index when it is missing (after an
            # upgrade, or a wiped cache). Doing it here keeps that work out of
            # the server, which is the whole point of running sync out of
            # process.
            engine.ensure_node_index()
            results = (
                engine.sync_all(args.mode, max_depth=args.depth, full_scan=args.full_scan)
                if args.all or not args.source
                else [
                    engine.sync_source(
                        args.source,
                        args.mode,
                        max_depth=args.depth,
                        full_scan=args.full_scan,
                    )
                ]
            )
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        if getattr(args, "json", False):
            # One line, last: the server runs this command as a subprocess and
            # reads the report off stdout.
            import json as _json

            payload = {
                "status": "ok",
                "results": [
                    {
                        "source_id": r.source_id,
                        "indexed_artifacts": r.indexed_artifacts,
                        "skipped_artifacts": r.skipped_artifacts,
                        "graph_nodes": r.graph_nodes,
                        "graph_edges": r.graph_edges,
                        "status": r.status,
                        "details": r.details,
                    }
                    for r in results
                ],
            }
            print(_json.dumps(payload))
            return 0 if all(r.status != "limit_exceeded" for r in results) else 1
        exit_code = 0
        for r in results:
            if r.status == "limit_exceeded":
                # A refused sync is a failure the caller must see — both in the
                # message and in the exit code, so a scripted sync does not
                # sail past it reporting "0 indexed" as if that were success.
                print(f"ERROR: {r.details.get('error', 'sync limit exceeded')}", file=sys.stderr)
                exit_code = 1
                continue
            print(
                f"{r.source_id}: indexed={r.indexed_artifacts} "
                f"skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}"
            )
        return exit_code
    if args.command == "scan":
        engine = _engine(Path(args.config))
        try:
            names = (
                [args.source]
                if args.source
                else [s.name for s in engine.config.sources if s.enabled]
            )
            reports = [engine.scan_source(name, max_depth=args.depth) for name in names]
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        if args.json:
            print(json.dumps(reports, indent=2))
            return 0
        for report in reports:
            _print_scan(report)
        return 0
    if args.command == "repair":
        from pheasant.sync.locks import EngineLeaseError

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
    if args.command == "backup":
        from pheasant.config.loader import load_config
        from pheasant.persistence.backup import create_backup
        from pheasant.persistence.paths import StatePaths

        cfg = load_config(Path(args.config))
        paths = StatePaths.from_config(cfg)
        out = create_backup(paths.state, Path(args.output), sqlite_path=paths.sqlite)
        size = out.stat().st_size
        print(f"Backup written: {out} ({size} bytes)")
        return 0
    if args.command == "restore":
        from pheasant.config.loader import load_config
        from pheasant.persistence.backup import restore_backup
        from pheasant.persistence.paths import StatePaths

        cfg = load_config(Path(args.config))
        paths = StatePaths.from_config(cfg)
        try:
            target = restore_backup(Path(args.input), paths.state, force=args.force)
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Restored state into: {target}")
        return 0
    if args.command == "serve":
        from pheasant.config.loader import load_config

        cfg = load_config(Path(args.config))
        # `serve` is the container entrypoint, where the UI is a separate
        # sidecar workload — an npm hint in the image's logs would be noise.
        _serve_app(cfg, args.config, report_ui=False)
        return 0
    if args.command == "mcp":
        from pheasant.config.loader import load_config
        from pheasant.mcp_server.server import run_mcp_server

        cfg = load_config(Path(args.config))
        run_mcp_server(cfg, args.transport)
        return 0
    if args.command == "client-config":
        from pheasant.mcp_client.vscode import (
            docker_exec_stdio_config,
            docker_run_stdio_config,
            render_vscode_mcp_json,
        )

        if args.client in ("claude-code", "cursor"):
            from pheasant.mcp_client.agents import (
                AGENT_CONFIG_FILES,
                agent_mcp_config,
                render_agent_mcp_json,
            )

            payload = agent_mcp_config(
                args.mode,
                server_name=args.server_name,
                config_path=args.config,
                container_name=args.container_name,
                image=args.image,
            )
            rendered = render_agent_mcp_json(payload)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
                print(
                    f"# save as {AGENT_CONFIG_FILES[args.client]} in your project root",
                    file=sys.stderr,
                )
            return 0
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
        from pheasant.deployment.compose_env import load_compose_environment, render_env_file

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
