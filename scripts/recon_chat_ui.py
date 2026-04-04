#!/usr/bin/env python3
"""Standalone UI reconnaissance script for discovering chat UI selectors."""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.browser.recon import run_recon


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run provider-specific UI reconnaissance")
    parser.add_argument("--model", default="internal-web-chat", help="Model id to inspect")
    args = parser.parse_args()

    print(f"Starting UI reconnaissance for model={args.model}...")
    print("This will open a browser to discover selectors.")
    print("Press Ctrl+C to exit.\n")

    asyncio.run(run_recon(model=args.model))
