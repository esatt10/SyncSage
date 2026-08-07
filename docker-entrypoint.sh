#!/bin/sh
# pheasant container entrypoint.
#
# Its whole job is to make `docker run … ghcr.io/esatt10/pheasant` work with no
# config file. Before this, the image shipped pheasant.example.yaml as
# /config/pheasant.yaml, so a user who mounted nothing got the *example*
# config's sources — and a user who mounted a config directory got an empty
# one and a server that refused to start.
#
# Rules:
#   * A config that already exists is never touched. A bind-mounted config —
#     read-only or not — always wins, so this cannot surprise anyone who
#     supplied one.
#   * The generated config comes from `pheasant setup --accept-defaults`, the
#     same deterministic wizard a user runs in a terminal. There is one code
#     path for "what does a default config look like", not two.
#   * Any argument that is not a known subcommand is executed verbatim, so
#     `docker run … pheasant sync --all` and `docker run … sh` both work.
set -eu

CONFIG_PATH="${PHEASANT_CONFIG:-/config/pheasant.yaml}"
WORKSPACE="${PHEASANT_WORKSPACE:-/workspace}"

generate_config() {
    [ -f "$CONFIG_PATH" ] && return 0

    if ! mkdir -p "$(dirname "$CONFIG_PATH")" 2>/dev/null; then
        echo "pheasant: no config at $CONFIG_PATH and its directory is not writable." >&2
        echo "pheasant: mount a config there, or set PHEASANT_CONFIG." >&2
        return 1
    fi

    echo "pheasant: no config at $CONFIG_PATH — generating one."
    # `--accept-defaults` asks nothing and reads every default off the live
    # schema, so this stays correct as the schema changes.
    python -m pheasant setup --accept-defaults --output "$CONFIG_PATH" >/dev/null

    # Index the workspace when something is actually mounted there. An empty
    # /workspace means the user has not pointed us at anything yet; adding a
    # source over an empty directory would just produce a lone graph node and
    # the UI's empty state is a better answer than that.
    if [ -d "$WORKSPACE" ] && [ -n "$(ls -A "$WORKSPACE" 2>/dev/null || true)" ]; then
        echo "pheasant: adding $WORKSPACE as the first source."
        python -m pheasant up "$WORKSPACE" --config "$CONFIG_PATH" --no-serve || {
            echo "pheasant: could not index $WORKSPACE; starting anyway." >&2
        }
    else
        echo "pheasant: $WORKSPACE is empty — add a source from the UI or with 'pheasant up'."
    fi
}

case "${1:-serve}" in
    serve)
        generate_config || exit 1
        exec python -m pheasant serve --config "$CONFIG_PATH"
        ;;
    setup|up|sync|start|mcp|scan|doctor|validate|init|config|host|mount|repair|backup|restore|\
    client-config|compose-env)
        exec python -m pheasant "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
