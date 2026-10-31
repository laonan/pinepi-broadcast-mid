"""Installer preflight and readiness checks; never print credentials or media URLs."""
import argparse
import asyncio
import importlib
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request


def validate(config_path):
    os.environ["PINEPI_BROADCAST_CONFIG"] = os.path.abspath(config_path)
    server = importlib.import_module("broadcast_server_mid")
    # Reuse real startup validation without locking/cleaning live media state.
    with tempfile.TemporaryDirectory(prefix="pinepi-preflight-") as directory:
        if server._config.get("media"):
            server._config["media"] = {**server._config["media"], "data_dir": directory}

        async def startup():
            async with server.lifespan(server.app):
                pass

        asyncio.run(startup())
    print("Configuration and application startup validation OK")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health():
    # Bypass HTTP_PROXY: this probe must reach the local upstream directly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for _ in range(30):
        request = urllib.request.Request("http://127.0.0.1:8765/api/send", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=1):
                pass
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 401:
                print("Readiness OK: unauthenticated API returns HTTP 401")
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError("Local API did not return HTTP 401; inspect journalctl -u pinepi-broadcast-mid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "health"))
    parser.add_argument("--config")
    args = parser.parse_args()
    if args.action == "validate":
        if not args.config:
            parser.error("validate requires --config")
        validate(args.config)
    else:
        health()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Avoid tracebacks/local variables containing configuration credentials.
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
