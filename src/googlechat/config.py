"""Where the saved session lives on disk (overridable with env vars)."""

import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("GCHAT_CONFIG_DIR", Path.home() / ".config" / "googlechat-mcp"))


def cookie_file() -> Path:
    return Path(os.environ.get("GCHAT_COOKIES", config_dir() / "cookies.json"))


def default_profile_dir() -> Path:
    return config_dir() / "browser-profile"


def profile_dir() -> Path:
    return Path(os.environ.get("GCHAT_PROFILE", default_profile_dir()))
