"""Request pieces shared by workflows: inline base64 files and the output size."""

from __future__ import annotations

import base64
import binascii

from pydantic import BaseModel, ConfigDict, Field, model_validator

MIN_ASPECT = 9 / 16
MAX_ASPECT = 16 / 9
ASPECT_TOLERANCE = 0.01


def decode_base64(value: str, max_bytes: int, what: str) -> bytes:
    """Strict base64 with a decoded-size limit; ``ValueError`` names what was wrong."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("data is not valid base64") from exc
    if not raw:
        raise ValueError("data is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"{what} larger than {max_bytes // (1024 * 1024)} MB")
    return raw


def aspect_ok(width: int, height: int) -> bool:
    return MIN_ASPECT - ASPECT_TOLERANCE <= width / height <= MAX_ASPECT + ASPECT_TOLERANCE


class OutputSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)

    @model_validator(mode="after")
    def _aspect(self) -> OutputSize:
        if not aspect_ok(self.width, self.height):
            raise ValueError("aspect ratio must be between 9:16 and 16:9")
        return self
