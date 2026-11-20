import os

import pytest

from app import db as db_mod
from app import seed


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """每个用例独立 sqlite 库；夹具仍取仓库内 fixtures（到期+未到期对照）。"""
    db_path = tmp_path / "lockerbay_test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    seed.init_db()
    yield db_path
