"""Git peer configuration independent of the API schema import graph."""

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GitSyncRemote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(repr=False, min_length=1)

    @model_validator(mode="after")
    def require_supported_endpoint(self) -> Self:
        # Credentials belong to SSH/credential storage, never replicated Markdown
        # or API errors. Explicit URLs also exclude Git's executable remote helpers.
        if any(ord(char) < 33 for char in self.url):
            raise ValueError("Git peer must be an absolute path or an ssh/https URL")
        if Path(self.url).is_absolute():
            return self
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"ssh", "https"}
            or not parsed.hostname
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "https" and parsed.username is not None)
        ):
            raise ValueError(
                "Git peer must be an absolute path or an ssh/https URL without secrets"
            )
        return self
