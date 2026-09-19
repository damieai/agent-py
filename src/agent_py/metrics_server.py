"""Optional protected per-Worker scrape server; business gauges live on the API scrape."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from prometheus_client import generate_latest


def start_worker_metrics(service):
    settings = service.settings
    if not settings.worker_metrics_enabled:
        return None
    secret = settings.metrics_secret.get_secret_value()
    if len(secret) < 32:
        raise ValueError("Worker metrics require a dedicated secret of at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret).encode()):
                self.send_error(403)
                return
            body = generate_latest(service.telemetry.registry)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        (settings.worker_metrics_host, settings.worker_metrics_port), Handler
    )
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True, name="worker-metrics").start()
    return server
