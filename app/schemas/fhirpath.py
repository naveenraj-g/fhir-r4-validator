from typing import Any
from pydantic import BaseModel


class FHIRPathResultItem(BaseModel):
    type: str
    element: Any
