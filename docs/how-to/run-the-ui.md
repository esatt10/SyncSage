# Run the web UI (and keep it up to date)

You have the `syncsage` CLI working and you want to *see* your knowledge base —
the three-pane workspace with sources, chat, and the graph canvas. This page is
the complete path from a shell prompt to a browser tab, plus the rules for
making sure the UI you are looking at is the current one.

!!! info "The UI is a separate workload"
    The UI is a static React bundle. It has no server of its own: it calls the
    SyncSage HTTP API and renders what comes back. So "run the UI" always means
    two things — **something serving the bundle**, and **the SyncSage API it can
    reach**. Every option below is just a different answer to those two.

## Pick a path

| Your situation | Use | UI lives at |
|---|---|---|
| I already run `syncsage` from the CLI and just want the UI | [Option 1 — SyncSage serves it](#option-1-let-syncsage-serve-the-ui-no-docker) | `http://localhost:8765` |
| I want the whole thing in containers, one command | [Option 2 — `syncsage host`](#option-2-syncsage-host-one-line-containers) | `http://localhost:8080` |
| I have this repo cloned and want the standard stack | [Option 3 — `docker compose`](#option-3-docker-compose-from-this-repo) | `http://localhost:8080` |
| I am editing the UI source | [Option 4 — Vite dev server](#option-4-vite-dev-server-for-ui-development) | `http://localhost:5173` |

All four serve the same bundle from `ui/`. None of them change how indexing
works.

---

## Option 1: let SyncSage serve the UI (no Docker)

Best if you already have the CLI running and do not want a second container.
SyncSage mounts a built bundle at the root of its own API port, so the UI and
the API are the same origin and there is nothing to proxy.

**Prerequisites:** Node.js 20+ (only to build the bundle once) and a working
`syncsage` CLI.

```bash
# 1. Build the bundle (from a clone of this repo)
cd ui
npm ci            # first time only; `npm install` is fine too
npm run build     # writes ui/dist
cd ..

# 2. Point SyncSage at some content and start it
syncsage up ~/notes --no-serve     # generates syncsage.yaml + indexes
syncsage start --config syncsage.yaml
```

On startup SyncSage tells you whether it found a bundle:

```text
API + MCP:   http://0.0.0.0:8765
Web UI:      http://localhost:8765
```

Open that URL. The API routes (`/health`, `/search`, …) keep working — the
bundle is mounted last, so it only catches paths the API does not claim. If the
banner says `Web UI: not served`, the bundle was not found; the two conditions
below are why.

!!! warning "Build with no `VITE_SYNCSAGE_API_BASE` set"
    A plain `npm run build` produces a bundle that calls the API on its **own
    origin**, which is what this option needs. Do not reuse the sidecar image's
    bundle here: that one is built with `VITE_SYNCSAGE_API_BASE=/api` and its
    requests would 404 against SyncSage directly.

`syncsage up <target>` serves the same way (index, then serve on `:8765`), so
once `ui/dist` exists you get the UI from the one-line command too.

Two conditions must hold or nothing is mounted (SyncSage starts normally and
serves API only):

- `server.ui.enabled` is `true` in your config — it is by default.
- A built bundle exists. SyncSage looks at `$SYNCSAGE_UI_DIST` first, then at
  `ui/dist` next to the source checkout. If you installed the package (not a
  clone), there is no `ui/` directory — build the bundle wherever you like and
  export the path:

```bash
SYNCSAGE_UI_DIST=/path/to/ui/dist syncsage start --config syncsage.yaml
```

Rebuild (`npm run build`) and restart `syncsage start` whenever you pull new UI
changes — the bundle is read from disk at startup.

---

## Option 2: `syncsage host` (one line, containers)

`syncsage host` resolves your targets, writes a config, writes a compose file,
and brings up **both** containers: SyncSage and the UI sidecar.

```bash
syncsage host ~/notes
```

```text
Wrote /home/you/syncsage.yaml
Wrote /home/you/syncsage.container.yaml
Wrote /home/you/docker-compose.syncsage.yml
SyncSage API + MCP: http://localhost:8765
SyncSage UI:        http://localhost:8080
```

Open **<http://localhost:8080>**.

Useful flags:

```bash
syncsage host ~/notes --ui-port 9090        # move the UI off 8080
syncsage host ~/notes --no-ui               # API + MCP only
syncsage host ~/notes --print-only          # write the compose file, run it yourself
syncsage host ~/notes --ui-image ghcr.io/esatt10/syncsage-ui:0.5.1   # pin a published bundle
```

Which UI bundle you get:

- **From a clone of this repo**, the generated compose file gets a `build:`
  context pointing at your `ui/` directory and `syncsage host` runs
  `docker compose up -d --build` — you always see *your* checkout's UI.
- **From an installed package**, there is no `ui/` to build, so the compose file
  pulls `ghcr.io/esatt10/syncsage-ui:<your syncsage version>` — the UI image is
  published from the same commit as the SyncSage image, so the two always match.
- `--ui-image` overrides both and pulls exactly what you name.

The generated `docker-compose.syncsage.yml` is a normal file: edit it, commit
it, or re-run it yourself with `docker compose -f docker-compose.syncsage.yml up -d --build`.

---

## Option 3: `docker compose` from this repo

The repo's `docker-compose.yml` is the reference stack: SyncSage plus the
`syncsage-ui` sidecar (nginx serving the bundle and proxying `/api/*` to the
SyncSage container).

```bash
git clone https://github.com/esatt10/SyncSage && cd SyncSage
docker compose up -d --build
```

Open **<http://localhost:8080>** for the UI and **<http://localhost:8765>** for
the API.

**Always pass `--build`.** The UI service carries both a `build:` context and an
`image:` tag. Compose builds only when no image with that tag exists locally, so
without `--build` you keep getting the bundle you built the first time — the
single most common reason "the UI didn't update".

### Point it at your own content

The default mounts index the repository itself. To index your own files, either
edit the compose variables directly:

```bash
SYNCSAGE_WORKSPACE_PATH=~/projects/my-app docker compose up -d --build
```

or generate an env file from your own config:

```bash
syncsage init --profile quickstart --output syncsage.yaml
# edit syncsage.yaml: sources + deployment.compose.workspace_path
syncsage compose-env syncsage.yaml --output .syncsage/compose.env
docker compose --env-file .syncsage/compose.env up -d --build
```

!!! warning "`compose-env` defaults the workspace to `./workspace`"
    Unless your config sets `deployment.compose.workspace_path`, the generated
    env file mounts `./workspace` — a directory that usually does not exist, so
    Docker creates it empty, nothing is indexed, and the UI comes up with one
    lonely knowledge-base node. Set `workspace_path` (or override
    `SYNCSAGE_WORKSPACE_PATH`) to the directory you actually want indexed.

### The compose variables

| Variable | Default | What it does |
|---|---|---|
| `SYNCSAGE_UI_PORT` | `8080` | Host port for the UI. |
| `SYNCSAGE_BIND` | `127.0.0.1` | Interface both ports publish on. |
| `SYNCSAGE_UI_IMAGE` | `ghcr.io/esatt10/syncsage-ui:<version>` | UI image tag to run/build as. |
| `SYNCSAGE_IMAGE` | `ghcr.io/esatt10/syncsage:<version>` | SyncSage image tag. |
| `SYNCSAGE_CONFIG_PATH` | `./syncsage.example.yaml` | Config mounted at `/config/syncsage.yaml`. |
| `SYNCSAGE_WORKSPACE_PATH` | `.` | Mounted read-only at `/workspace`. |
| `SYNCSAGE_DATA_PATH` | `./data` | Extra read-only mount at `/data`. |
| `SYNCSAGE_VAULT_PATH` | `./vault` | Obsidian projection output. |

Both ports publish to **loopback only** by default. The API is unauthenticated,
so only set `SYNCSAGE_BIND=0.0.0.0` behind an authenticating ingress — Docker's
iptables rules bypass most host firewalls. See [Security](../security.md).

---

## Option 4: Vite dev server (for UI development)

Hot reload against a SyncSage that is already running (from any option above,
or a bare `syncsage start`):

```bash
cd ui
npm ci
npm run dev            # http://localhost:5173
```

The dev server proxies `/api/*` to `http://localhost:8765`. Point it elsewhere:

```bash
SYNCSAGE_API_BASE=http://localhost:9000 npm run dev
```

Before opening a PR:

```bash
npm run typecheck
npm run build
```

---

## Getting the *current* UI

Almost every "my UI is stale" report is one of these three. The rules:

| Situation | What actually happens | Do this |
|---|---|---|
| Compose reuses a local image | An image with that tag already exists, so `docker compose up -d` never rebuilds it | `docker compose up -d --build` (or `--build syncsage-ui` for just the UI) |
| A `:latest` tag is cached | Docker does not re-pull a tag it already has | `docker compose pull` — or prefer the version-pinned tags, which is what SyncSage generates |
| SyncSage-served bundle | `ui/dist` is read at process start | `npm run build`, then restart `syncsage start` |

The nuclear option, when you want to be certain nothing is cached:

```bash
docker compose down
docker image rm ghcr.io/esatt10/syncsage-ui:$(python -c "from syncsage.version import __version__; print(__version__)")
docker compose up -d --build
```

Upgrading SyncSage itself upgrades the UI with it: the two images are built from
the same commit and published under the same version tag, so
`docker compose pull && docker compose up -d` moves both.

---

## Verify it worked

Work down this list; the first failure tells you which layer is broken.

```bash
# 1. The API is alive
curl -fsS http://localhost:8765/health

# 2. Something is actually indexed (`indexed_artifacts` > 0)
curl -fsS http://localhost:8765/overview

# 3. The bundle is being served
curl -fsSI http://localhost:8080 | head -1        # sidecar
curl -fsSI http://localhost:8765 | head -1        # SyncSage-served

# 4. The UI's own path to the API resolves (sidecar only)
curl -fsS http://localhost:8080/api/health
```

Then open the UI. A working first load shows your sources in the left pane. If
the graph looks sparse, that is by design: `concept` and `chunk` nodes are
hidden by default because they dominate a real index — turn them on from the
legend, which doubles as the type filter.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| UI loads but every panel errors / "failed to fetch" | The bundle cannot reach the API | Sidecar: is the `syncsage` container healthy (`docker compose ps`)? The nginx proxy resolves the service name `syncsage`, so both containers must be on the same compose project. SyncSage-served: check `curl localhost:8765/health`. |
| `manifest unknown` / `denied` pulling `syncsage-ui` | That version's UI image predates UI image publishing, or the tag does not exist | Build it instead — `docker compose up -d --build` from a clone — or pass `--ui-image` with a tag that exists. |
| UI shows old code after a change | Compose reused the local image | `docker compose up -d --build`; see [Getting the current UI](#getting-the-current-ui). |
| UI is up, canvas is empty | Nothing indexed yet, or the wrong directory was mounted | `curl localhost:8765/overview`. If `indexed_artifacts` is 0, check what is mounted at `/workspace` and the `sources:` in your config; then `syncsage sync --all --mode full` or `POST /sync`. |
| Only one node, labelled with your KB name | The workspace mount is empty (often the `./workspace` default) | Set `SYNCSAGE_WORKSPACE_PATH` / `deployment.compose.workspace_path` to real content and re-run. |
| Blank page, 404 on a deep link like `/sources` | Serving `dist/` with a static server that has no SPA fallback | Use the provided nginx config or SyncSage's own mount — both fall back to `index.html`. |
| `port is already allocated` | 8080 or 8765 in use | `SYNCSAGE_UI_PORT=9090 docker compose up -d` or `syncsage host --ui-port 9090 --port 9100`. |
| Nothing on 8080 from another machine | Ports publish to loopback by default | Tunnel (`ssh -L 8080:localhost:8080 host`), or set `SYNCSAGE_BIND` **only** behind an authenticating proxy. |
| `syncsage start` serves the API but not the UI | No built bundle found, or `server.ui.enabled: false` | `npm run build` in `ui/`, or set `SYNCSAGE_UI_DIST` to a bundle; confirm `server.ui.enabled`. |
| Chat answers are terse / extractive only | No model connected — this is the designed fallback, not a failure | Connect a key in the UI, or see [Ask your knowledge base](chat-and-ui.md). |

More: [Troubleshooting](../troubleshooting.md) ·
[Deployment](../deployment.md) ·
[Ask your knowledge base](chat-and-ui.md) ·
[UI source and design notes](https://github.com/esatt10/SyncSage/tree/main/ui)
