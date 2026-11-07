"""Import the repository-as-package layout when pytest runs from either root."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def pytest_addoption(parser):
    parser.addoption("--run-browser", action="store_true", help="Run isolated real-browser workbench tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-browser"):
        skip = pytest.mark.skip(reason="Enable real-browser tests with --run-browser")
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(skip)
