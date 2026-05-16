from __future__ import annotations

import os

import uvicorn

from syncsage.api.app import create_app
from syncsage.config.loader import load_config
from syncsage.telemetry.logging import configure_logging


def main() -> None:
    config_path = os.environ.get("SYNCSAGE_CONFIG", "/config/syncsage.yaml")
    config = load_config(config_path)
    configure_logging(config.syncsage.log_level)
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
