from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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

    class Config:
        env_file = ".env"


settings = Settings()
