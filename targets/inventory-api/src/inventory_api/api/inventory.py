from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from inventory_api.database import get_db
from inventory_api.models import Product, User
from inventory_api.schemas.inventory import StockAdjustmentRequest, StockResponse
from inventory_api.security import require_admin
from inventory_api.services.inventory import adjust_stock

router = APIRouter(prefix="/products", tags=["inventory"])


def change_stock(
    product_id: int,
    data: StockAdjustmentRequest,
    direction: int,
    db: Session,
    user: User,
) -> StockResponse:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        quantity = adjust_stock(db, product, data.amount, direction, data.reason, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StockResponse(product_id=product.id, quantity=quantity, adjustment=direction * data.amount)


@router.post("/{product_id}/inventory/add", response_model=StockResponse)
def add_stock(
    product_id: int,
    data: StockAdjustmentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> StockResponse:
    return change_stock(product_id, data, 1, db, user)


@router.post("/{product_id}/inventory/remove", response_model=StockResponse)
def remove_stock(
    product_id: int,
    data: StockAdjustmentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> StockResponse:
    return change_stock(product_id, data, -1, db, user)

