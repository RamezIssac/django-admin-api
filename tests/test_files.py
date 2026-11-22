"""File and image fields: base64 uploads on the main form and in inline rows, over REST and MCP."""

import base64
import io
import json

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings
from PIL import Image

from admin_api.mcp import AdminMcpSite
from admin_api.writes import Uploads
from tests.test_mcp_http import post, token  # noqa: F401 (fixture)
from tests.testapp.files_admin import files_api_site, files_mcp
from tests.testapp.models import Attachment, Document

LIST = "/files-api/v1/testapp/document/"
EXPECTED_SHAPE = 'Expected {"filename": "...", "content_base64": "..."}.'


def upload(filename, content=b"hello"):
    return {"filename": filename, "content_base64": base64.b64encode(content).decode()}


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_URL = "/media/"
    return tmp_path


def stored(media):
    return sorted(str(p.relative_to(media)) for p in media.rglob("*") if p.is_file())


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def call(method, url, payload):
        resp = getattr(client, method)(url, data=json.dumps(payload), content_type="application/json")
        return resp.status_code, json.loads(resp.content)

    return call


@pytest.fixture
def call(rf, superuser):
    def do(name, args, site=files_mcp):
        request = rf.post("/mcp/")
        request.user = superuser
        result = site.call_tool(request, name, args)
        return result.data, result.is_error

    return do


@pytest.fixture
def doc(db):
    """A document with a file and two attachments."""
    document = Document.objects.create(title="Lease")
    document.file.save("lease.txt", ContentFile(b"v1"))
    for name in ("a.txt", "b.txt"):
        row = Attachment(document=document)
        row.file.save(name, ContentFile(name.encode()))
    return document


# --------------------------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_create_with_file_image_and_inline_file(api, media):
    payload = {
        "title": "Lease",
        "file": upload("lease.pdf", b"%PDF-1"),
        "image": upload("logo.png", png()),
        "inlines": {"attachments": [{"file": upload("terms.txt", b"terms"), "caption": "Terms"}]},
    }
    status, body = api("post", LIST, payload)
    assert status == 201, body
    assert body["file"] == {"name": "docs/lease.pdf", "url": "/media/docs/lease.pdf"}
    assert body["image"] == {"name": "images/logo.png", "url": "/media/images/logo.png"}
    assert body["inlines"]["attachments"][0]["file"] == {
        "name": "attachments/terms.txt",
        "url": "/media/attachments/terms.txt",
    }
    assert (media / "docs/lease.pdf").read_bytes() == b"%PDF-1"
    assert (media / "attachments/terms.txt").read_bytes() == b"terms"


@pytest.mark.django_db
def test_patch_without_the_file_keeps_it(api, doc):
    status, body = api("patch", f"{LIST}{doc.pk}/", {"title": "Lease 2"})
    assert status == 200, body
    assert body["file"]["name"] == "docs/lease.txt"
    assert [r["file"]["name"] for r in body["inlines"]["attachments"]] == ["attachments/a.txt", "attachments/b.txt"]


@pytest.mark.django_db
def test_put_without_the_file_keeps_it(api, doc):
    status, body = api("put", f"{LIST}{doc.pk}/", {"title": "Lease 3"})
    assert status == 200, body
    assert body["file"]["name"] == "docs/lease.txt"


@pytest.mark.django_db
def test_put_with_the_read_shape_keeps_the_file(api, doc):
    read_shape = {"name": doc.file.name, "url": doc.file.url}
    status, body = api("put", f"{LIST}{doc.pk}/", {"title": "Lease 3", "file": read_shape})
    assert status == 200, body
    assert body["file"]["name"] == "docs/lease.txt"


@pytest.mark.django_db
def test_null_clears_an_optional_file(api, doc):
    status, body = api("patch", f"{LIST}{doc.pk}/", {"file": None})
    assert status == 200, body
    assert body["file"] is None
    doc.refresh_from_db()
    assert not doc.file


@pytest.mark.django_db
def test_null_does_not_clear_a_required_inline_file(api, doc):
    row = doc.attachments.order_by("pk").first()
    status, body = api("patch", f"{LIST}{doc.pk}/", {"inlines": {"attachments": [{"pk": row.pk, "file": None}]}})
    assert status == 200, body
    assert body["inlines"]["attachments"][0]["file"]["name"] == "attachments/a.txt"


@pytest.mark.django_db
def test_inline_rows_replace_keep_and_add_files(api, doc, media):
    first, second = doc.attachments.order_by("pk")
    rows = [{"pk": first.pk, "file": upload("a2.txt", b"new")}, {"file": upload("c.txt", b"c"), "caption": "C"}]
    status, body = api("patch", f"{LIST}{doc.pk}/", {"inlines": {"attachments": rows}})
    assert status == 200, body
    names = [(r["pk"], r["file"]["name"]) for r in body["inlines"]["attachments"]]
    assert names[:2] == [(first.pk, "attachments/a2.txt"), (second.pk, "attachments/b.txt")]
    assert names[2][1] == "attachments/c.txt"
    assert (media / "attachments/a2.txt").read_bytes() == b"new"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"filename": "x.txt", "content_base64": "not base64!"}, "content_base64 is not valid base64."),
        ({"content_base64": "aGk="}, EXPECTED_SHAPE),
        ({"filename": "", "content_base64": "aGk="}, EXPECTED_SHAPE),
        ({"filename": "x.txt", "content_base64": 5}, EXPECTED_SHAPE),
        ({"filename": "x.txt"}, EXPECTED_SHAPE),
        ({"filename": ["x.txt"], "content_base64": "aGk="}, EXPECTED_SHAPE),
        (upload(".."), "'..' is not a valid file name."),
    ],
)
def test_bad_upload_is_a_field_error(api, value, message):
    status, body = api("post", LIST, {"title": "T", "file": value})
    assert status == 400
    assert body["error"] == "validation_failed"
    assert body["fields"] == {"file": [message]}
    assert not Document.objects.exists()


@pytest.mark.django_db
def test_size_cap_is_the_sites(api, monkeypatch, media):
    assert files_api_site.max_upload_size == 5 * 1024 * 1024
    monkeypatch.setattr(files_api_site, "max_upload_size", 4)
    status, body = api("post", LIST, {"title": "T", "file": upload("x.txt", b"12345")})
    assert status == 400
    assert body["fields"] == {"file": ["The file has 5 bytes, the limit is 4."]}
    status, body = api("post", LIST, {"title": "T", "file": upload("x.txt", b"1234")})
    assert status == 201, body
    assert stored(media) == ["docs/x.txt"]


def test_uploads_guess_the_content_type():
    files = Uploads(10)
    files.add("a", upload("a.pdf"))
    files.add("b", upload("b.unknown-ext"))
    assert (files["a"].content_type, files["b"].content_type) == ("application/pdf", "application/octet-stream")


@pytest.mark.django_db
def test_bad_inline_upload_is_a_row_error(api, doc):
    row = doc.attachments.order_by("pk").first()
    rows = [{"pk": row.pk, "file": {"filename": "x.txt"}}, {"file": {"filename": "y.txt", "content_base64": "%"}}]
    status, body = api("patch", f"{LIST}{doc.pk}/", {"inlines": {"attachments": rows}})
    assert status == 400
    assert body["inlines"]["attachments"]["rows"] == [
        {"index": 0, "pk": row.pk, "errors": {"file": [EXPECTED_SHAPE]}},
        {"index": 2, "pk": None, "errors": {"file": ["content_base64 is not valid base64."]}},
    ]


@pytest.mark.django_db
def test_the_admins_own_validation_applies(api, media):
    payload = {
        "title": "T",
        "image": upload("logo.png", b"not an image"),
        "inlines": {
            "attachments": [{"file": upload("run.exe")}, {"file": upload("empty.txt", b"")}, {"caption": "No file"}]
        },
    }
    status, body = api("post", LIST, payload)
    assert status == 400
    assert body["fields"]["image"][0].startswith("Upload a valid image.")
    errors = [r["errors"]["file"][0] for r in body["inlines"]["attachments"]["rows"]]
    assert errors[0].startswith("File extension “exe” is not allowed.")
    assert errors[1:] == ["The submitted file is empty.", "This field is required."]
    assert stored(media) == []


@pytest.mark.django_db
@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=100)
def test_body_over_the_django_limit_is_a_json_413(api):
    status, body = api("post", LIST, {"title": "T", "file": upload("x.txt", b"x" * 200)})
    assert status == 413
    assert body["error"] == "payload_too_large"
    assert "DATA_UPLOAD_MAX_MEMORY_SIZE" in body["detail"]


@pytest.mark.django_db
def test_mcp_http_body_over_the_django_limit_is_a_json_413(client, token):  # noqa: F811
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=100):
        resp = post(client, {"jsonrpc": "2.0", "id": 1, "method": "ping", "padding": "x" * 200}, token=token)
    assert resp.status_code == 413
    assert json.loads(resp.content)["error"] == "payload_too_large"


# --------------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------------
UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "minLength": 1},
        "content_base64": {"type": "string", "contentEncoding": "base64"},
    },
    "required": ["filename", "content_base64"],
    "additionalProperties": False,
}


@pytest.mark.django_db
def test_describe_model_shows_the_upload_shape(call):
    out, err = call("describe_model", {"model": "testapp.document"})
    assert not err, out
    create = out["input_schema"]["create"]
    optional = create["properties"]["file"]
    assert {k: optional[k] for k in UPLOAD_SCHEMA} == {**UPLOAD_SCHEMA, "type": ["object", "null"]}
    assert "required" not in create or "file" not in create["required"]
    row = create["properties"]["inlines"]["properties"]["attachments"]["items"]
    assert {k: row["properties"]["file"][k] for k in UPLOAD_SCHEMA} == UPLOAD_SCHEMA
    assert row["required"] == ["file"]


# --------------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------------
def create_args(**extra):
    data = {"title": "Lease", "file": upload("lease.txt"), "inlines": {"attachments": [{"file": upload("t.txt")}]}}
    return {"model": "testapp.document", "data": data, **extra}


@pytest.mark.django_db
def test_mcp_create_and_read(call, media):
    out, err = call("create_object", create_args())
    assert not err, out
    assert out["object"]["file"] == {"name": "docs/lease.txt", "url": "/media/docs/lease.txt"}
    assert out["object"]["inlines"]["attachments"][0]["file"]["name"] == "attachments/t.txt"
    got, _ = call("get_object", {"model": "testapp.document", "id": out["object"]["id"]})
    assert got["file"] == out["object"]["file"]
    assert stored(media) == ["attachments/t.txt", "docs/lease.txt"]


@pytest.mark.django_db
def test_mcp_dry_run_stores_no_file(call, media, doc):
    before = stored(media)
    out, err = call("create_object", create_args(dry_run=True))
    assert not err and out["valid"] is True and "object" not in out, out
    assert Document.objects.count() == 1
    rows = [{"id": doc.attachments.order_by("pk").first().pk, "file": upload("a3.txt")}]
    args = {"model": "testapp.document", "id": doc.pk, "data": {"inlines": {"attachments": rows}}, "dry_run": True}
    out, err = call("update_object", args)
    assert not err and out["valid"] is True, out
    assert stored(media) == before


@pytest.mark.django_db
def test_mcp_dry_run_reports_upload_errors(call):
    args = create_args(dry_run=True)
    args["data"]["file"] = {"filename": "x.txt", "content_base64": "!"}
    out, err = call("create_object", args)
    assert not err and out["valid"] is False
    assert out["errors"]["fields"] == {"file": ["content_base64 is not valid base64."]}


@pytest.mark.django_db
def test_mcp_confirm_token_write_matches_its_dry_run(call, media):
    gated = AdminMcpSite(files_api_site, name="files_gated", require_dry_run=True)
    out, err = call("create_object", create_args(), site=gated)
    assert err and out["error"] == "dry_run_required"
    dry, err = call("create_object", create_args(dry_run=True), site=gated)
    assert not err and dry["valid"] is True, dry
    assert stored(media) == []
    out, err = call("create_object", create_args(confirm_token=dry["confirm_token"]), site=gated)
    assert not err, out
    assert out["object"]["file"]["name"] == "docs/lease.txt"
    assert stored(media) == ["attachments/t.txt", "docs/lease.txt"]
