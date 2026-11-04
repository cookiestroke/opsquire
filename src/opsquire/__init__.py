"""Opsquire: agents prepare the scripts, you run them, and every run is recorded for the next agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("opsquire")
except PackageNotFoundError:
    __version__ = "0+unknown"
