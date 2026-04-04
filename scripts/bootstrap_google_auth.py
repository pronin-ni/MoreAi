#!/usr/bin/env python3

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.browser.auth import google_auth_bootstrapper


async def main(model: str) -> None:
    path = await google_auth_bootstrapper.ensure_model_authenticated(model)
    print(f"Saved storage state for model={model}: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bootstrap Google auth for a provider model")
    parser.add_argument("--model", required=True, help="Model id that uses Google auth")
    args = parser.parse_args()
    asyncio.run(main(args.model))
