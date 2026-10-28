"""Create a local .env with stable random secrets without printing them."""

import os
import secrets
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    if env_path.exists():
        print(".env already exists; preserved. See .env.example for optional settings.")
        return 0
    content = (root / ".env.example").read_text(encoding="utf-8")
    password = secrets.token_urlsafe(32)
    content = content.replace("POSTGRES_PASSWORD=porter", f"POSTGRES_PASSWORD={password}")
    content = content.replace(
        "postgresql://porter:porter@localhost", f"postgresql://porter:{password}@localhost"
    )
    content += f"\nPORTER_JWT_SECRET={secrets.token_urlsafe(48)}\n"
    content += f"PORTER_SETUP_TOKEN={secrets.token_urlsafe(32)}\n"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    env_path.chmod(0o600)
    print("Created .env with random credentials. Start with: docker compose up --build -d")
    print(
        "When services are ready, double-click Open-Porter.cmd on Windows "
        "or run: python scripts/open_panel.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
