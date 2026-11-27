from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

_VARIABLES = {
    "port": "PORT",
    "data_dir": "DATA_DIR",
    "sim_port": "SIM_PORT",
    "embed_url": "EMBED_URL",
    "embed_api_key": "EMBED_API_KEY",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
        validate_default=True,
    )

    port: int = Field(default=8080, validation_alias="PORT", ge=1, le=65535)
    data_dir: Path = Field(default=Path(".store"), validation_alias="DATA_DIR")
    sim_port: int = Field(default=8090, validation_alias="SIM_PORT", ge=1, le=65535)
    embed_url: str = Field(
        default_factory=lambda data: f"http://127.0.0.1:{data.get('sim_port', 8090)}",
        validation_alias="EMBED_URL",
    )
    embed_api_key: SecretStr = Field(
        default=SecretStr("test-embed-key"), validation_alias="EMBED_API_KEY"
    )

    @property
    def embedding_url(self) -> str:
        return self.embed_url.rstrip("/") + "/v1/embeddings"

    @property
    def answer_url(self) -> str:
        return self.embed_url.rstrip("/") + "/v1/chat/completions"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (env_settings,)

    @field_validator("port", "sim_port", mode="before")
    @classmethod
    def validate_port(cls, value: object) -> int:
        if type(value) is int:
            return value
        if isinstance(value, str):
            digits = value.lstrip("0") or "0"
            if value.isascii() and value.isdigit() and len(digits) <= 5:
                return int(digits)
        raise ValueError("Expected ASCII digits from 1 to 65535.")

    @field_validator("data_dir", mode="before")
    @classmethod
    def validate_data_dir(cls, value: object) -> object:
        if value == "":
            raise ValueError("Expected a non-empty directory path.")
        return value

    @field_validator("embed_url")
    @classmethod
    def validate_embed_url(cls, value: str) -> str:
        url = AnyHttpUrl(value)
        if url.username is not None or url.password is not None:
            raise ValueError("Credentials must be supplied through EMBED_API_KEY.")
        if url.query is not None or url.fragment is not None:
            raise ValueError("Expected a base URL without a query or fragment.")
        return str(url).rstrip("/")

    @field_validator("embed_api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if not key or not key.isascii() or any(character.isspace() for character in key):
            raise ValueError("Expected a non-empty ASCII key without whitespace.")
        return value


class SettingsError(ValueError):
    def __init__(self, variable: str):
        self.variable = variable
        super().__init__(f"Invalid setting: {variable}.")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        location = error.errors(include_input=False, include_url=False)[0]["loc"]
        name = str(location[0]) if location else "configuration"
        variable = _VARIABLES.get(name, name if name in _VARIABLES.values() else "configuration")
        raise SettingsError(variable) from None
