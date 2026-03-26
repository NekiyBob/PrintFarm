from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


Base = declarative_base()
SessionLocal = sessionmaker(autoflush=False, autocommit=False, expire_on_commit=False)

_ENGINE: Optional[Engine] = None


def _build_sqlite_url(path: Path) -> str:
    resolved = path.resolve()
    return f"sqlite:///{resolved.as_posix()}"


def init_maintenance_db(path: Path) -> Engine:
    global _ENGINE

    if _ENGINE is not None:
        return _ENGINE

    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        _build_sqlite_url(path),
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Import models before create_all so the metadata is fully registered.
    import maintenance_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    SessionLocal.configure(bind=engine)
    _ENGINE = engine
    return engine


def get_maintenance_session() -> Session:
    if _ENGINE is None:
        raise RuntimeError("Maintenance database is not initialized")
    return SessionLocal()
