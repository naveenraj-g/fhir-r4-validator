from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    validator_port: int = Field(default=4567, validation_alias="VALIDATOR_PORT")
    host: str = Field(default="0.0.0.0", validation_alias="VALIDATOR_HOST")

    # Project directories
    data_dir: Path = Field(default=_ROOT / "data", validation_alias="DATA_DIR")
    igs_dir: Path = Field(default=_ROOT / "igs", validation_alias="IGS_DIR")
    package_cache_dir: Path = Field(
        default=Path.home() / ".cache" / "fhir-r4-validator" / "packages",
        validation_alias="PACKAGE_CACHE_DIR",
    )

    # Terminology settings (same variables as the Java wrapper)
    tx_server_url: str = Field(default="http://tx.fhir.org", validation_alias="TX_SERVER_URL")
    # The Java wrapper disables terminology whenever DISABLE_TX is *set* (any value).
    disable_tx: str | None = Field(default=None, validation_alias="DISABLE_TX")
    tx_timeout_seconds: float = Field(default=10.0, validation_alias="TX_TIMEOUT_SECONDS")
    display_issues_are_warnings: bool = Field(default=False, validation_alias="DISPLAY_ISSUES_ARE_WARNINGS")

    # NPM package registry
    packages_registry_url: str = Field(default="https://packages.fhir.org", validation_alias="PACKAGES_REGISTRY_URL")
    packages_catalogue_url: str = Field(
        default="https://packages2.fhir.org/packages/catalog", validation_alias="PACKAGES_CATALOGUE_URL"
    )
    preload_igs: str = Field(default="", validation_alias="PRELOAD_IGS")
    preload_igs_strict: bool = Field(default=False, validation_alias="PRELOAD_IGS_STRICT")
    packages_offline: bool = Field(default=False, validation_alias="PACKAGES_OFFLINE")

    # Version metadata
    wrapper_version: str = Field(default="2.3.6", validation_alias="WRAPPER_VERSION")
    # The Java service reports the HL7 validator library version; this is the version we are compatible with.
    hl7_validator_version: str = Field(default="6.5.18", validation_alias="HL7_VALIDATOR_VERSION")

    # Logging
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @property
    def tx_disabled(self) -> bool:
        return self.disable_tx is not None


settings = Settings()
