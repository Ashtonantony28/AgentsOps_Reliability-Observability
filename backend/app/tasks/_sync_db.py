from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings


sync_engine = create_engine(settings.db_url_sync, pool_pre_ping=True)
SyncSession = sessionmaker(sync_engine, autoflush=False, autocommit=False)
