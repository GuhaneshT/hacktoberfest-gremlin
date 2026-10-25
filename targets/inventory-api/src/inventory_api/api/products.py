from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from inventory_api.database import get_db
from inventory_api.models import Product, User
from inventory_api.schemas.products import ProductCreate, ProductPage, ProductResponse, ProductUpdate
from inventory_api.security import get_current_user, require_admin
from inventory_api.services.products import create_product, list_products, update_product

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create(
    data: ProductCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Product:
    try:
        return create_product(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Unable to create product") from exc


@router.get("", response_model=ProductPage)
def list_all(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ProductPage:
    products, total = list_products(db, page, page_size)
    return ProductPage(items=products, page=page, page_size=page_size, total=total)


@router.get("/{product_id}", response_model=ProductResponse)
def get_one(product_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.patch("/{product_id}", response_model=ProductResponse)
def update(
    product_id: int,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        return update_product(db, product, data)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="SKU is already in use") from exc


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(product_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)) -> None:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(product)
    db.commit()

