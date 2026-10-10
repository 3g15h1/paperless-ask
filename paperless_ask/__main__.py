"""`paperless-ask` / `python -m paperless_ask`: run the web app with uvicorn.

HOST (default 0.0.0.0) and PORT (default 8000) choose where it listens; everything else is
configured with the environment variables described in .env.example.
"""
import argparse
import os

import uvicorn

from . import __version__


def main() -> None:
    p = argparse.ArgumentParser(prog="paperless-ask", description="Natural-language Q&A over Paperless-ngx.")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args()
    uvicorn.run("paperless_ask.app:app", host=a.host, port=a.port, log_level=a.log_level,
                server_header=False, proxy_headers=True, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
