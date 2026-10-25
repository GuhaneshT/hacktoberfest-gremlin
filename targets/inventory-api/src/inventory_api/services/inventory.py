from sqlalchemy.orm import Session

from inventory_api.models import InventoryAdjustment, Product, User


def adjust_stock(
    db: Session,
    product: Product,
    amount: int,
    direction: int,
    reason: str | None,
    user: User,
) -> int:
    new_quantity = product.quantity + direction * amount
    if new_quantity < 0:
        raise ValueError("insufficient stock")
    if direction < 0 and new_quantity == 0:
        # Intentional defect: zero is valid stock, but the boundary is rejected.
        raise ValueError("stock cannot be zero")
    product.quantity = new_quantity
    db.add(
        InventoryAdjustment(
            product_id=product.id,
            delta=direction * amount,
            reason=reason,
            created_by=user.id,
        )
    )
    db.commit()
    db.refresh(product)
    return product.quantity

