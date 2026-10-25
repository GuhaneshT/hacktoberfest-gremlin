from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from inventory_api.api import auth, inventory, products
from inventory_api.config import get_settings
from inventory_api.database import Base, SessionLocal, engine
from inventory_api.models import User
from inventory_api.security import hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    settings = get_settings()
    if settings.admin_email and settings.admin_password:
        with SessionLocal() as db:
            if db.scalar(select(User).where(User.email == settings.admin_email.lower())) is None:
                db.add(
                    User(
                        email=settings.admin_email.lower(),
                        password_hash=hash_password(settings.admin_password),
                        is_admin=True,
                    )
                )
                db.commit()
    yield


app = FastAPI(title="Inventory API", version="0.1.0", lifespan=lifespan)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(products.router, prefix="/api/v1")
app.include_router(inventory.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

