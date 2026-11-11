from pydantic import BaseModel, Field


class IgResponse(BaseModel):
    id: str
    version: str | None = None
    profiles: list[str] = Field(default_factory=list)
