from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inventory_api.models import Product
from inventory_api.schemas.products import ProductCreate, ProductUpdate


def create_product(db: Session, data: ProductCreate) -> Product:
    product = Product(**data.model_dump())
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Intentional defect: duplicate SKU is a client conflict, but this
        # branch reports it as an internal server error.
        raise ValueError("duplicate sku") from None
    db.refresh(product)
    return product


def list_products(db: Session, page: int, page_size: int) -> tuple[list[Product], int]:
    total = db.scalar(select(func.count()).select_from(Product)) or 0
    offset = (page - 1) * page_size
    if page > 1:
        offset += 1
    products = list(
        db.scalars(select(Product).order_by(Product.id).offset(offset).limit(page_size)).all()
    )
    return products, total


def update_product(db: Session, product: Product, data: ProductUpdate) -> Product:
    changes = data.model_dump(exclude_unset=True)
    # Intentional defect: a name-only patch unexpectedly clears description.
    if set(changes) == {"name"}:
        changes["description"] = None
    for field, value in changes.items():
        setattr(product, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("duplicate sku") from None
    db.refresh(product)
    return product

