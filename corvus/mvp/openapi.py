from __future__ import annotations

import argparse
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from corvus.mvp.api import create_app


def build_openapi_document() -> dict[str, Any]:
    """Build the adapter contract without persisting credentials or a database."""
    with tempfile.TemporaryDirectory(prefix="corvus-openapi-") as directory:
        app = create_app(
            database=Path(directory) / "contract.sqlite3",
            bootstrap_token=secrets.token_urlsafe(32),
            session_secret=secrets.token_bytes(48),
        )
        return app.openapi()


def write_openapi_document(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_openapi_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the Corvus MVP OpenAPI contract")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    write_openapi_document(arguments.output)


if __name__ == "__main__":
    main()
