"""The generic nested layer (no nesting library needed). End-to-end: tests/test_subadmin.py."""

import pytest

from admin_api.errors import NotFound
from admin_api.nested import BadModelPath, ModelPath
from admin_api.views.nested import parse_nested


def test_model_path_parse():
    path = ModelPath.parse(" Store.Client/Sales/salesitem ")
    assert path == ModelPath("store", "client", ("sales", "salesitem"))
    assert path.label == "store.client/sales/salesitem"
    assert path.root_label == "store.client" and path.depth == 2
    assert ModelPath.parse("store.client").segments == ()
    for bad in ("", "client", "store.", ".client", "store.client/", "store.client//sales"):
        with pytest.raises(BadModelPath):
            ModelPath.parse(bad)


def test_model_path_urls():
    path = ModelPath("store", "client", ("sales", "salesitem"))
    assert path.url("/api/v1/", [1, "a/b"]) == "/api/v1/store/client/1/sales/a_2Fb/salesitem/"
    assert path.url_template("/api/v1/") == "/api/v1/store/client/{0}/sales/{1}/salesitem/"
    assert ModelPath("store", "client").url("/api/v1/") == "/api/v1/store/client/"


@pytest.mark.parametrize(
    "nested, expected",
    [
        ("1/sales", (["sales"], ["1"], "changelist", {})),
        ("1/sales/schema", (["sales"], ["1"], "schema", {})),
        ("1/sales/actions/delete_selected", (["sales"], ["1"], "action", {"action_name": "delete_selected"})),
        ("1/sales/7", (["sales"], ["1"], "object", {"object_id": "7"})),
        ("1/sales/7/delete-preview", (["sales"], ["1"], "delete_preview", {"object_id": "7"})),
        ("1/sales/7/salesitem", (["sales", "salesitem"], ["1", "7"], "changelist", {})),
        ("1/sales/7/salesitem/3", (["sales", "salesitem"], ["1", "7"], "object", {"object_id": "3"})),
    ],
)
def test_parse_nested(nested, expected):
    assert parse_nested(nested) == expected


@pytest.mark.parametrize("nested", ["1", "1//sales", "1/sales/7/delete-preview/x", "1/Sales"])
def test_parse_nested_rejects(nested):
    with pytest.raises(NotFound):
        parse_nested(nested)
