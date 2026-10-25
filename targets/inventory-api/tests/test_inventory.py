from tests.conftest import auth
from tests.test_products import create_product


def test_add_and_remove_stock(client, admin_token):
    test_client, _ = client
    created = create_product(test_client, admin_token, quantity=5)
    product_id = created.json()["id"]
    added = test_client.post(
        f"/api/v1/products/{product_id}/inventory/add",
        headers=auth(admin_token),
        json={"amount": 3, "reason": "delivery"},
    )
    assert added.status_code == 200
    assert added.json()["quantity"] == 8
    removed = test_client.post(
        f"/api/v1/products/{product_id}/inventory/remove",
        headers=auth(admin_token),
        json={"amount": 2, "reason": "sale"},
    )
    assert removed.status_code == 200
    assert removed.json()["quantity"] == 6


def test_inventory_rejects_negative_result(client, admin_token):
    test_client, _ = client
    product_id = create_product(test_client, admin_token, quantity=1).json()["id"]
    response = test_client.post(
        f"/api/v1/products/{product_id}/inventory/remove",
        headers=auth(admin_token),
        json={"amount": 2},
    )
    assert response.status_code == 400
    product = test_client.get(f"/api/v1/products/{product_id}", headers=auth(admin_token))
    assert product.json()["quantity"] == 1


def test_health():
    from fastapi.testclient import TestClient
    from inventory_api.main import app

    assert TestClient(app).get("/health").json() == {"status": "ok"}

