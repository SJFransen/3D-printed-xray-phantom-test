"""Launch the MSF Phantom QA web app.

    python run_app.py [port]

Reads configuration from .env (see .env.example). Binds to 127.0.0.1 by
default — for a server deployment set PHANTOMQA_HOST=0.0.0.0 and put a
TLS-terminating reverse proxy in front; see README "Deploying on a server".
"""

import os
import sys

import uvicorn

from phantom_qa.config import get_config

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(
        os.environ.get("PHANTOMQA_PORT", 8777))
    host = os.environ.get("PHANTOMQA_HOST", "127.0.0.1")
    cfg = get_config()
    print(f"MSF Phantom QA — {cfg.summary()}")
    if not cfg.auth_enabled:
        print("  !! authentication is DISABLED — do not expose this port to a "
              "network. Set PHANTOMQA_ENV=production in .env before deploying.")
    uvicorn.run("phantom_qa.webapp.main:app", host=host, port=port,
                log_level="info", proxy_headers=cfg.behind_proxy,
                forwarded_allow_ips=os.environ.get("PHANTOMQA_FORWARDED_ALLOW_IPS", "127.0.0.1")
                if cfg.behind_proxy else None)
