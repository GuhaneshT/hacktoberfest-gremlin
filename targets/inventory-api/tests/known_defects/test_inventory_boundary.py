import pytest

from tests.conftest import auth
from tests.test_products import create_product


@pytest.mark.xfail(strict=True, reason="Known defect: removing all remaining units incorrectly rejects quantity zero.")
def test_removing_last_unit_is_allowed(client, admin_token):
    test_client, _ = client
    product_id = create_product(test_client, admin_token, quantity=1).json()["id"]
    response = test_client.post(
        f"/api/v1/products/{product_id}/inventory/remove",
        headers=auth(admin_token),
        json={"amount": 1},
    )
    assert response.status_code == 200
    assert response.json()["quantity"] == 0

