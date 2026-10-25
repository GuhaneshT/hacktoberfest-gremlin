from tests.conftest import auth


def create_product(client, token, sku="SKU-1", name="Widget", description="Useful", quantity=5):
    return client.post(
        "/api/v1/products",
        headers=auth(token),
        json={"sku": sku, "name": name, "description": description, "price": "12.50", "quantity": quantity},
    )


def test_product_crud_and_pagination(client, admin_token):
    test_client, _ = client
    first = create_product(test_client, admin_token)
    assert first.status_code == 201
    second = create_product(test_client, admin_token, "SKU-2", "Gadget")
    assert second.status_code == 201
    listed = test_client.get("/api/v1/products?page=1&page_size=10", headers=auth(admin_token))
    assert listed.status_code == 200
    assert [item["sku"] for item in listed.json()["items"]] == ["SKU-1", "SKU-2"]

    product_id = first.json()["id"]
    updated = test_client.patch(
        f"/api/v1/products/{product_id}",
        headers=auth(admin_token),
        json={"name": "Updated Widget", "price": "15.00", "quantity": 7},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Widget"
    fetched = test_client.get(f"/api/v1/products/{product_id}", headers=auth(admin_token))
    assert fetched.status_code == 200
    assert fetched.json()["quantity"] == 7
    assert test_client.delete(f"/api/v1/products/{product_id}", headers=auth(admin_token)).status_code == 204


def test_duplicate_sku_does_not_create_second_product(client, admin_token):
    test_client, _ = client
    assert create_product(test_client, admin_token).status_code == 201
    duplicate = create_product(test_client, admin_token)
    assert duplicate.status_code == 500
    listing = test_client.get("/api/v1/products", headers=auth(admin_token))
    assert listing.json()["total"] == 1

