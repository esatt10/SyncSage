# Troubleshooting

## Health checks fail

- Confirm the container is running and port `8765` is exposed.
- Check `SYNCSAGE_CONFIG` points to `/config/syncsage.yaml`.
- Confirm `/state` is writable.

## Sources are not indexed

- Verify source paths exist inside the container, not just on the host.
- Confirm paths resolve under allowlisted workspace roots.
- Check include/exclude patterns are not filtering everything.
- Inspect sync status and event logs for parser or permission errors.

## Watcher events are missed

Docker mount watcher behavior differs across macOS, Windows/WSL2, and Linux. Keep scheduled fallback sync enabled and use explicit `sync_source` after agent commits.

## Obsidian vault is too noisy

Disable chunk notes, keep file notes concise, and store large graph exports under `/state` or `/exports` instead of the vault.

## State appears stale or inconsistent

Run validation/repair commands when available:

```bash
syncsage validate
syncsage repair
syncsage rebuild --source <source>
syncsage rebuild --all
```

## Multiple instances conflict

Do not point multiple independent SyncSage containers at the same writable `/state` volume. Use one namespace/PVC per instance.
