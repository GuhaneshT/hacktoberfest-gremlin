from pydantic import BaseModel, Field


class StockAdjustmentRequest(BaseModel):
    amount: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=255)


class StockResponse(BaseModel):
    product_id: int
    quantity: int
    adjustment: int

