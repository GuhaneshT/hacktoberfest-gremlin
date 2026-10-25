def test_register_login_and_me(client):
    test_client, _ = client
    registered = test_client.post(
        "/api/v1/auth/register",
        json={"email": "person@example.com", "password": "strong-password"},
    )
    assert registered.status_code == 201
    assert registered.json()["is_admin"] is False

    login = test_client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.com", "password": "strong-password"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    me = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "person@example.com"


def test_invalid_login_and_admin_authorization(client, user_token):
    test_client, _ = client
    invalid = test_client.post(
        "/api/v1/auth/login",
        json={"email": "user@example.com", "password": "wrong-password"},
    )
    assert invalid.status_code == 401
    denied = test_client.post(
        "/api/v1/products",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"sku": "NOPE", "name": "Nope", "price": "1.00"},
    )
    assert denied.status_code == 403

