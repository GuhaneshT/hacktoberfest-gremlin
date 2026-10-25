import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from inventory_api.database import Base, get_db
from inventory_api.main import app
from inventory_api.models import User
from inventory_api.security import hash_password


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client, TestingSession
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def admin_token(client):
    test_client, session_factory = client
    with session_factory() as db:
        admin = User(email="admin@example.com", password_hash=hash_password("admin-password"), is_admin=True)
        db.add(admin)
        db.commit()
    response = test_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "admin-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture()
def user_token(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "user-password"},
    )
    assert response.status_code == 201
    login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "user-password"},
    )
    return login.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

