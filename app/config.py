"""Paths and constants shared across the app."""
import os
from pathlib import Path

# Everything the app writes lives under DATA_DIR (the container's only bind mount).
DATA_DIR = Path(os.environ.get("FORGE_DATA_DIR", "/data"))
# Install directory: static files and the read-only defaults shipped in the image.
APP_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
