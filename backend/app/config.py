from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="FOOTY_",
    )

    app_name: str = "FOOTY API"
    debug: bool = False
    database_url: str = "sqlite:///./footy.db"
    jwt_secret: str = "change-me-in-env-with-at-least-32-chars"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_minutes: int = 60 * 24 * 7
    cors_origins: str = "http://localhost:3000"
    access_cookie_name: str = "access_token"
    refresh_cookie_name: str = "refresh_token"
    anonymous_cookie_name: str = "anonymous_id"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cookie_domain: str | None = None
    cookie_path: str = "/"
    upload_dir: str = "./uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    allowed_upload_mime: str = "image/jpeg,image/png,image/webp,image/gif"
    exports_dir: str = "../exports"
    bootstrap_report_dir: str = "../runbooks/reports"
    ranker_artifacts_dir: str = "./artifacts/ranker"
    ranker_model_filename: str = "model.txt"
    ranker_meta_filename: str = "meta.json"
    ranker_dataset_filename: str = "training_dataset.jsonl"
    ranker_min_rows: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
