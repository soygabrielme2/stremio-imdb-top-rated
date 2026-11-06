#!/usr/bin/env python3
"""Serve ./dist locally so Stremio on this PC can install the addon.

    python serve.py            # then add http://127.0.0.1:7000/manifest.json in Stremio
"""
import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".json": "application/json"}

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7000)
    ap.add_argument("--dir", default="dist")
    args = ap.parse_args()
    handler = functools.partial(Handler, directory=args.dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {args.dir} at http://127.0.0.1:{args.port}/manifest.json", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
