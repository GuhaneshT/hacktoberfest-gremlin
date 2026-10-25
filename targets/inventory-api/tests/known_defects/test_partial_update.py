import pytest

from tests.conftest import auth
from tests.test_products import create_product


@pytest.mark.xfail(strict=True, reason="Known defect: a name-only patch clears the existing description.")
def test_name_only_patch_preserves_description(client, admin_token):
    test_client, _ = client
    created = create_product(test_client, admin_token, description="Must remain")
    product_id = created.json()["id"]
    response = test_client.patch(
        f"/api/v1/products/{product_id}",
        headers=auth(admin_token),
        json={"name": "Renamed"},
    )
    assert response.status_code == 200
    assert response.json()["description"] == "Must remain"

