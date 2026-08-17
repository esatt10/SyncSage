#!/bin/sh
# Reset only the persistent mounts owned by the UI-native Compose pathway,
# then hand control to the image's normal config-generation/startup flow.
set -eu

find /config -mindepth 1 -delete
find /state -mindepth 1 -delete
find /exports -mindepth 1 -delete

exec /app/docker-entrypoint.sh serve
