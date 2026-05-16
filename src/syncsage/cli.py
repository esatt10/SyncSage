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
        results = engine.sync_all(args.mode) if args.all or not args.source else [engine.sync_source(args.source, args.mode)]
        for r in results: print(f"{r.source_id}: indexed={r.indexed_artifacts} skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}")
        return 0
    if args.command == "repair":
        _engine(Path(args.config)).sync_all("full"); print("Repair complete"); return 0
    if args.command == "serve":
        from syncsage.api.app import create_app
        import uvicorn
        cfg = load_config(Path(args.config))
        uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)
        return 0
    return 1


def app() -> None:
    raise SystemExit(main())
