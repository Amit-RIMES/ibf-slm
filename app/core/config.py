from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@rimes.int"
    APP_BASE_URL: str = "http://localhost:8000"
    RESET_TOKEN_EXPIRE_MINUTES: int = 60

    LOG_FILE: str = "ibf_app.log"
    LOG_LEVEL: str = "INFO"
    SMTP_FAILURE_ALERT_AFTER: int = 3
    TRIGGER_COOLDOWN_HOURS: int = 6
    ALERT_ESCALATION_HOURS: int = 24
    WEEKLY_DIGEST_DAY: int = 0   # 0=Monday
    WEEKLY_DIGEST_HOUR: int = 8

    # Ollama chat assistant
    OLLAMA_HOST: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "gemma4:e4b"

    # CHIRPS observed rainfall ingestion
    CHIRPS_ENABLED: bool = True
    CHIRPS_LOOKBACK_DAYS: int = 7     # how many days back to fetch on each run
    CHIRPS_LAT_MIN: float = 0.0
    CHIRPS_LAT_MAX: float = 35.0
    CHIRPS_LON_MIN: float = 60.0
    CHIRPS_LON_MAX: float = 155.0

    # Data gap alerting
    DATA_GAP_CHIRPS_DAYS: int = 3       # alert if no new CHIRPS for this many days
    DATA_GAP_FORECAST_DAYS: int = 3     # alert if no new forecast for this many days
    DATA_GAP_ALERT_COOLDOWN_HOURS: int = 24  # min hours between repeat gap alert emails



settings = Settings()
