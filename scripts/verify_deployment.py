"""Build the isolated stack and run real Postgres/HTTP/stdio acceptance.

python scripts/verify_deployment.py --keep-running
Requires Docker and requirements-test.txt. Credentials stay in .env.review.
"""

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import asyncpg
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    ".env.review",
    "-p",
    "porter-review",
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.verify.yml",
]


async def prepare_test_database(url):
    connection = await asyncpg.connect(url)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'porter_test'"
        )
        if not exists:
            await connection.execute("CREATE DATABASE porter_test")
    finally:
        await connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave only the isolated verification stack running",
    )
    args = parser.parse_args(argv)
    path = ROOT / ".env.review"
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            for name in ("POSTGRES_PASSWORD", "PORTER_JWT_SECRET", "PORTER_SETUP_TOKEN"):
                output.write(name + "=" + secrets.token_urlsafe(48) + "\n")
    cfg = dotenv_values(path)
    for name in ("POSTGRES_PASSWORD", "PORTER_JWT_SECRET", "PORTER_SETUP_TOKEN"):
        if not cfg.get(name):
            raise SystemExit("Missing " + name + " in .env.review")
    env = dict(os.environ)
    database = "postgresql://porter:" + cfg["POSTGRES_PASSWORD"] + "@127.0.0.1:55432/porter"
    base = "http://127.0.0.1:18000"
    try:
        subprocess.run(
            COMPOSE + ["up", "--build", "-d", "--wait", "--wait-timeout", "90"],
            cwd=ROOT,
            check=True,
        )
        with httpx.Client(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get(base + "/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("API did not become ready; inspect the porter-review logs")
                time.sleep(0.25)
        asyncio.run(prepare_test_database(database))
        env.update(
            PORTER_TEST_DATABASE_URL=database + "_test",
            PORTER_TEST_BASE_URL=base,
            PORTER_TEST_SETUP_TOKEN=cfg["PORTER_SETUP_TOKEN"],
            PORTER_TEST_JWT_SECRET=cfg["PORTER_JWT_SECRET"],
            PORTER_TEST_STDIO_DATABASE_URL=database,
            PORTER_TEST_EMBEDDER_URL="http://127.0.0.1:18001",
            PORTER_TEST_RECREATE="1",
            PYTHONUTF8="1",
        )
        with tempfile.TemporaryDirectory(prefix="porter-deployment-") as directory:
            temp = Path(directory) / "pytest"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_postgres_integration.py",
                    "tests/test_deployed_api.py",
                    "--basetemp",
                    str(temp),
                    "--tb=short",
                ],
                cwd=ROOT,
                env=env,
            )
        if result.returncode == 0:
            print(
                "Verified: real Postgres, OAuth, HTTP/stdio MCP, snapshots and container recreation."
            )
            if args.keep_running:
                print("Panel: " + base + "/panel (setup token is in .env.review)")
            print(
                "Embedding mode: deterministic hash; model quality is a separate acceptance check."
            )
        else:
            subprocess.run(COMPOSE + ["logs", "--tail", "100"], cwd=ROOT, check=False)
        return result.returncode
    except Exception:
        subprocess.run(COMPOSE + ["logs", "--tail", "100"], cwd=ROOT, check=False)
        raise
    finally:
        if not args.keep_running:
            # Keep all named volumes; never affect another Compose project.
            subprocess.run(COMPOSE + ["down"], cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
