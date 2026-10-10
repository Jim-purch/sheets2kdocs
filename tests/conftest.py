import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def tmp_db(tmp_path):
    from app import db

    db.close_thread_conn()
    db.init_db(tmp_path / "test.db")
    yield db
    db.close_thread_conn()
