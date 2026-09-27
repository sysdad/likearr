"""Run the web app under uvicorn. Imported only by `likearr start`."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from likearr.logging_setup import setup_logging
from likearr.models import EXIT_OK
from likearr.web.app import WebSettings, create_app

__all__ = ["serve"]


def serve(config_path: Path, *, host: str, port: int, password: str, verbose: bool = False) -> int:
    """Serve until SIGTERM, then drain (see `JobRunner.shutdown`) and return.

    ``proxy_headers`` is off and no address is trusted to forward: uvicorn would otherwise
    rewrite the client address from ``X-Forwarded-For``, and the login pause is keyed on it.
    Logging goes through likearr's own redacting handler rather than uvicorn's.
    """
    setup_logging(verbose)
    app = create_app(
        WebSettings(
            config_path=config_path,
            password=password,
            auto_fetch_names=True,
            auto_count_files=True,
            auto_preview_prune=True,
            scheduler=True,
        )
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        server_header=False,
        log_config=None,
    )
    return EXIT_OK
