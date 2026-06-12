import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import token_lens_core as core  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    conn = core.connect(tmp_path / "token_lens.db")
    yield conn
    conn.close()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "token_lens.db"
