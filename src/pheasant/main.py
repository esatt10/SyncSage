from __future__ import annotations

import os

import uvicorn

from pheasant.api.app import create_app
from pheasant.config.loader import load_config
from pheasant.telemetry.logging import configure_logging


def main() -> None:
    config_path = os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml")
    config = load_config(config_path)
    configure_logging(config.pheasant.log_level)
    uvicorn.run(
        create_app(config, config_path=config_path),
        host=config.server.host,
        port=config.server.port,
    )


if __name__ == "__main__":
    main()
