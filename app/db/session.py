from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def database(url: str):
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
    )
    return engine, sessionmaker(engine, expire_on_commit=False)
