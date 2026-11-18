"""Minimal HTTP server for the Indiana Field Explorer (stdlib only)."""

from __future__ import annotations

import gzip
import json
import mimetypes
import posixpath
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .store import Store


class Handler(BaseHTTPRequestHandler):
    server_version = "IndianaFieldExplorer/1.0"
    store: Store
    web_dir: str

    def log_message(self, fmt, *args):  # quieter default logging
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    # ------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              cache: str = "no-store", compress: bool | None = None) -> None:
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if compress is None:
            compress = not content_type.startswith("image/")
        gzipped = body
        if accepts_gzip and compress and len(body) > 1024:
            gzipped = gzip.compress(body, 6)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if gzipped is not body:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(gzipped)))
        self.end_headers()
        self.wfile.write(gzipped)

    def _json(self, payload, status: int = 200, cache: str = "no-store") -> None:
        self._send(status, json.dumps(payload, separators=(",", ":"),
                                      allow_nan=False).encode(),
                   "application/json; charset=utf-8", cache)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path == "/healthz":
                return self._json({"ok": True})
            if path == "/api/meta":
                return self._json(self.store.county_meta())
            if path == "/api/fields.geojson":
                return self._json(self.store.fields_geojson(),
                                  cache="public, max-age=3600")
            if path == "/api/ndvi":
                year = int(query.get("year", ["2021"])[0])
                period = int(query.get("period", ["1"])[0])
                if year not in (2020, 2021, 2022, 2023, 2024, 2025, 2026):
                    return self._error(400, "year out of range")
                if not 1 <= period <= 15:
                    return self._error(400, "period out of range")
                return self._json(self.store.period_values(year, period))
            if path == "/api/ndvi-raster":
                year = int(query.get("year", ["2021"])[0])
                period = int(query.get("period", ["1"])[0])
                if year not in (2020, 2021, 2022, 2023, 2024, 2025, 2026):
                    return self._error(400, "year out of range")
                if not 1 <= period <= 15:
                    return self._error(400, "period out of range")
                return self._json(self.store.raster_info(year, period))
            if path == "/api/ndvi-raster.png":
                year = int(query.get("year", ["2021"])[0])
                period = int(query.get("period", ["1"])[0])
                png = self.store.raster_png(year, period)
                if not png.exists():
                    return self._error(404, "raster not computed")
                return self._send(200, png.read_bytes(), "image/png",
                                  cache="public, max-age=3600")
            if path == "/api/field-raster-stats":
                field_id = query.get("field_id", [""])[0]
                year = int(query.get("year", ["2021"])[0])
                period = int(query.get("period", ["1"])[0])
                stats = self.store.field_raster_stats(field_id, year, period)
                if stats is None:
                    return self._error(404, "unknown field_id")
                return self._json(stats)
            if path == "/api/rasters":
                return self._json({"rasters": self.store.cached_rasters()})
            if path == "/api/soils.geojson":
                return self._json(self.store.soils_geojson(),
                                  cache="public, max-age=3600")
            if path.startswith("/api/field/"):
                field_id = path.rsplit("/", 1)[-1]
                detail = self.store.field_detail(field_id)
                if detail is None:
                    return self._error(404, "unknown field_id")
                return self._json(detail)
            if path == "/api/job":
                return self._json(self.store.job() or {"state": "idle"})
            return self._static(path)
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/compute-2020/"):
            try:
                period = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._error(400, "bad period")
            if not 1 <= period <= 15:
                return self._error(400, "period out of range")
            return self._json(self.store.start_2020(period))
        if parsed.path.startswith("/api/compute-raster/"):
            parts = parsed.path.strip("/").split("/")
            try:
                year, period = int(parts[-2]), int(parts[-1])
            except (ValueError, IndexError):
                return self._error(400, "bad year or period")
            if year not in (2020, 2021, 2022, 2023, 2024, 2025, 2026):
                return self._error(400, "year out of range")
            if not 1 <= period <= 15:
                return self._error(400, "period out of range")
            return self._json(self.store.start_raster(year, period))
        self._error(404, "not found")

    # ------------------------------------------------------------------
    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        safe = posixpath.normpath(path).lstrip("/")
        if safe.startswith(".."):
            return self._error(404, "not found")
        target = self.store.cfg.web_dir / safe
        if not target.is_file():
            return self._error(404, "not found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        cache = "no-cache" if target.name == "index.html" else "public, max-age=300"
        self._send(200, target.read_bytes(), content_type, cache)


def serve(host: str = "0.0.0.0", port: int = 8420) -> None:
    Handler.store = Store()
    Handler.web_dir = str(Handler.store.cfg.web_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Indiana Field Explorer on http://{host}:{port} "
          f"(web={Handler.web_dir})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
