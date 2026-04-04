import os
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

_API_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="footy-api-tests-")).resolve()
_API_TEST_DB_PATH = (_API_TEST_DB_DIR / "api-tests.db").resolve()
os.environ["FOOTY_DATABASE_URL"] = f"sqlite:///{_API_TEST_DB_PATH.as_posix()}"


@pytest.fixture(scope="session", autouse=True)
def api_test_db_isolation() -> Generator[None, None, None]:
    from app.database import engine

    engine_url = str(engine.url).lower()
    if "footy.db" in engine_url and _API_TEST_DB_PATH.name.lower() not in engine_url:
        raise RuntimeError(
            f"Refusing to run tests against shared DB: {engine.url}. "
            "API tests must use an isolated temporary sqlite DB."
        )

    yield

    engine.dispose()
    if _API_TEST_DB_PATH.exists():
        try:
            _API_TEST_DB_PATH.unlink(missing_ok=True)
        except PermissionError:
            pass
    shutil.rmtree(_API_TEST_DB_DIR, ignore_errors=True)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
