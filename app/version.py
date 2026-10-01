"""Report installed package metadata, including source-only development runs."""

from importlib.metadata import PackageNotFoundError, version


def dashboard_version() -> str:
    try:
        return version("gufo-dashboard")
    except PackageNotFoundError:
        return "unknown (source checkout)"
