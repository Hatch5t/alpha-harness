"""Shared, data-only Template Lab document types. No execution or application state."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..catalog.queries import FieldFilter
from . import template


class FieldsVariable(BaseModel):
    """A variable whose values are data fields: the chosen datasets', narrowed by a filter."""

    kind: Literal["fields"]
    dataset_ids: list[str] = Field(default_factory=list, max_length=500)
    #: The Data Explorer's filter, for anything beyond the datasets: Region Exclusive,
    #: coverage, field types. Field types left out follow where the template writes it.
    filter: FieldFilter | None = None
    #: With matrix and vector fields both searched: what a vector field is reduced with.
    vector_operators: list[str] = Field(default_factory=list, max_length=20)


class ValuesVariable(BaseModel):
    """A variable whose values are typed: numbers, groups, operator names or expressions."""

    kind: Literal["values"]
    values: str = Field(default="", max_length=4000)


Variable = Annotated[FieldsVariable | ValuesVariable, Field(discriminator="kind")]


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    text: str = Field(default="", max_length=template.MAX_TEXT)
    variables: dict[str, Variable] = Field(default_factory=dict, max_length=64)
