"""Validate container settings and launch the backend."""

import os
import sys
from urllib.parse import urlsplit, urlunsplit


def fail(key):
    raise SystemExit(f"Missing or invalid runtime configuration: {key}")


def configure():
    env = os.environ
    for key in (
        "MONGO_URI",
        "JWT_SECRET",
        "DB_NAME",
        "S3_ENDPOINT",
        "S3_BUCKET",
        "S3_PUBLIC_BASE_URL",
    ):
        if not env.get(key, "").strip():
            fail(key)
    if len(env["JWT_SECRET"].encode()) < 32 or env["JWT_SECRET"] == "change-me-in-prod":
        fail("JWT_SECRET")
    # Both explicitly empty is the existing anonymous SeaweedFS mode. Missing
    # keys must not activate the application's built-in development credentials.
    if (
        any(k not in env for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY"))
        or bool(env["S3_ACCESS_KEY"]) != bool(env["S3_SECRET_KEY"])
        or "minioadmin" in (env["S3_ACCESS_KEY"], env["S3_SECRET_KEY"])
    ):
        fail("S3_ACCESS_KEY/S3_SECRET_KEY")
    # Adapt this deployment's single Mongo endpoint only. Preserve the raw,
    # possibly percent-encoded userinfo, database, and authentication options.
    try:
        u = urlsplit(env["MONGO_URI"])
        userinfo, sep, host = u.netloc.rpartition("@")
        if (
            u.scheme != "mongodb"
            or not host
            or "," in host
            or u.port not in (None, 27017)
        ):
            fail("MONGO_URI (expected single standard-port Mongo endpoint)")
        env["MONGO_URI"] = urlunsplit(u._replace(netloc=userinfo + sep + "mongo:27017"))
    except ValueError:
        fail("MONGO_URI")


if __name__ == "__main__":
    configure()
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "twilightcupbackend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8100",
            "--loop",
            "uvloop",
        ],
    )
