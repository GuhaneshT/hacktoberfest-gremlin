import pytest

from tests.conftest import auth
from tests.test_products import create_product


@pytest.mark.xfail(strict=True, reason="Known defect: duplicate SKU is reported as HTTP 500 instead of HTTP 409.")
def test_duplicate_sku_returns_conflict(client, admin_token):
    test_client, _ = client
    assert create_product(test_client, admin_token, "DUP-1").status_code == 201
    response = create_product(test_client, admin_token, "DUP-1")
    assert response.status_code == 409

