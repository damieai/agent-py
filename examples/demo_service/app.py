"""A small lab-only workload for incident and patch verification exercises."""

import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Agent lab workload")
started = time.monotonic()


@app.get("/health")
def health():
    unhealthy = os.getenv("DEMO_FAULT") == "unhealthy"
    if unhealthy:
        raise HTTPException(503, "Injected lab fault")
    return {"status": "healthy"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    capacity = 0 if os.getenv("DEMO_FAULT") == "capacity" else 2
    return (
        f"demo_worker_capacity {capacity}\n"
        f"demo_queue_depth {100 if capacity == 0 else 0}\n"
        f"demo_uptime_seconds {time.monotonic() - started}\n"
    )
