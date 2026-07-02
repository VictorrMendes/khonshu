from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://khonshu:khonshu@localhost:5432/khonshu"
    redis_url: str = "redis://localhost:6379"

    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "qwen2.5:3b"
    ollama_embedding_model: str = "nomic-embed-text"

    ntfy_url: str = "http://localhost:2586"
    ntfy_topic: str = "khonshu"

    influxdb_url: str = "http://localhost:8086"
    influxdb_token: str = ""
    influxdb_org: str = "vmserver"
    influxdb_bucket: str = "khonshu"

    secret_key: str = "changeme"
    access_token_expire_minutes: int = 60

    api_port: int = 8100
    web_port: int = 3100


settings = Settings()
