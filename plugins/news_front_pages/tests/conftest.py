"""Optional external Tesserae checkout for testing the standalone catalog plugin."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

# A catalog plugin uses its own code, with the host providing app and fixtures.
if os.environ.get("TESSERAE_TEST_ROOT"):
    sys.path.insert(0, os.environ["TESSERAE_TEST_ROOT"])
    package = types.ModuleType("plugins.news_front_pages")
    package.__path__ = [str(Path(os.environ["TESSERAE_TEST_PLUGINS"]) / "news_front_pages")]
    sys.modules["plugins.news_front_pages"] = package


@pytest.fixture
def app(tmp_path):
    from app.main import REPO_ROOT, create_app

    return create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=Path(os.environ.get("TESSERAE_TEST_PLUGINS", REPO_ROOT / "plugins")),
    )


@pytest.fixture
def client(app):
    return app.test_client()
