import pytest

from tests.conftest import auth
from tests.test_products import create_product


@pytest.mark.xfail(strict=True, reason="Known defect: page > 1 adds one to the pagination offset.")
def test_second_page_includes_the_next_product(client, admin_token):
    test_client, _ = client
    for index in range(3):
        assert create_product(test_client, admin_token, f"SKU-{index}", f"Product {index}").status_code == 201
    response = test_client.get("/api/v1/products?page=2&page_size=1", headers=auth(admin_token))
    assert [item["sku"] for item in response.json()["items"]] == ["SKU-1"]

