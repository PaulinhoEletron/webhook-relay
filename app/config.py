import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    operator_api_key: SecretStr = Field(min_length=32)
    signing_secret: SecretStr = Field(min_length=32)
    destination_url: str = "http://receiver:8001/webhooks"
    demo_mode: bool = True
    retry_profile: Literal["production", "demo"] = "production"
    request_deadline: float = Field(default=10, gt=0, le=10)
    lease_duration: int = Field(default=60, ge=60)
    demo_storage_limit_bytes: int = Field(default=268435456, ge=1)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    public_base_url: str = "http://localhost:8000"

    @model_validator(mode="after")
    def safe_configuration(self):
        for network in self.trusted_proxy_cidrs:
            ipaddress.ip_network(network, strict=True)
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("Use PostgreSQL with psycopg")
        for secret in (self.operator_api_key, self.signing_secret):
            if secret.get_secret_value().startswith("replace-"):
                raise ValueError("Replace placeholder secrets before startup")
        if self.retry_profile == "demo" and not self.demo_mode:
            raise ValueError("Accelerated retries require demo mode")
        url = urlsplit(self.destination_url)
        internal = self.demo_mode and self.destination_url == "http://receiver:8001/webhooks"
        if not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("Destination must have a host and no credentials or fragment")
        if url.scheme != "https" and not internal:
            raise ValueError("HTTPS required except the explicit Compose demo receiver")
        return self
