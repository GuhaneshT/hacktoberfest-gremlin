from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    quantity: int = Field(default=0, ge=0)


class ProductUpdate(BaseModel):
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    quantity: int | None = Field(default=None, ge=0)


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str | None
    price: Decimal
    quantity: int


class ProductPage(BaseModel):
    items: list[ProductResponse]
    page: int
    page_size: int
    total: int

