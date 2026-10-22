from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "development"
    APP_SECRET: str = "dev-secret-change-me-please-32-bytes"
    JWT_SECRET: str = "dev-jwt-secret-change-me-please-32"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL_MIN: int = 60
    JWT_REFRESH_TTL_DAYS: int = 14
    REGISTER_LIMIT_PER_HOUR: int = 5  # per client address; each registration makes an org
    LOGIN_LIMIT_PER_MIN: int = 10  # per client address and per email
    PASSWORD_HASH_THREADS: int = 2  # bcrypt's own executor
    PASSWORD_HASH_QUEUE: int = 16  # hashes running or waiting for it; past it, 429

    POSTGRES_USER: str = "agentops"
    POSTGRES_PASSWORD: str = "agentops"
    POSTGRES_DB: str = "agentops"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_URL: str = "redis://redis:6379/0"

    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"
    FRONTEND_URL: str = "http://localhost:3000"  # invite links point here

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SLACK_CLIENT_ID: str = ""
    SLACK_CLIENT_SECRET: str = ""

    SEED_ON_START: bool = True
    DEMO_EMAIL: str = "demo@agentops.dev"
    DEMO_PASSWORD: str = "Demo1234!"

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def db_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


# the defaults above and the values in .env.example: public, so never valid outside development
PUBLIC_SECRETS = {
    "", Settings.model_fields["APP_SECRET"].default, Settings.model_fields["JWT_SECRET"].default,
    "change-me-please-32-bytes-minimum-secret-key", "change-me-too-please-32-bytes-minimum-jwt-key",
}


def check_secrets(s: Settings) -> None:
    if s.APP_ENV != "development":
        public = [k for k in ("APP_SECRET", "JWT_SECRET") if getattr(s, k) in PUBLIC_SECRETS]
        if public:
            raise RuntimeError(f"{' and '.join(public)} must be set outside development (APP_ENV={s.APP_ENV})")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
