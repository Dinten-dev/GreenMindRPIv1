from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    gateway_id: str = "pi-gw-01"
    hetzner_api_url: str = "http://macmini.local:8000/api/v1/ingest"
    hetzner_api_token: str = "changeme"
    esp32_auth_token: str = "secret_from_esp32"
    sqlite_db_url: str = "sqlite:////var/lib/greenmind-gateway/queue.db"
    allow_unauthenticated_esp32: bool = True
    
    class Config:
        env_file = ".env"

settings = Settings()
