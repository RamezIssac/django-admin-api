import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory

from admin_api.contrib.search.indexer import Indexer, set_indexer
from admin_api.models import ApiToken
from tests.search_site import mcp_site
from tests.testapp.admin import site
from tests.testapp.models import Client, Sales


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def api_request(rf):
    req = rf.get("/")
    req.is_api = True
    return req


@pytest.fixture
def html_request(rf):
    req = rf.get("/")
    req.is_api = False
    return req


@pytest.fixture
def client_admin():
    return site._registry[Client]


@pytest.fixture
def sales_admin():
    return site._registry[Sales]


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_superuser("root", "r@x.io", "pw")


@pytest.fixture
def staff_view_clients(db):
    user = get_user_model().objects.create_user("staff", "s@x.io", "pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_client"))
    return user


@pytest.fixture
def plain_user(db):
    return get_user_model().objects.create_user("plain", "p@x.io", "pw")


# --- search contrib (tests/test_search_*.py) ---------------------------------------------------
@pytest.fixture
def indexer(db):
    indexer = Indexer(sites=[mcp_site])
    set_indexer(indexer)
    yield indexer
    set_indexer(None)


@pytest.fixture
def indexed(indexer, django_capture_on_commit_callbacks):
    """Create objects inside this context manager: their post_save rows are written on exit."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


@pytest.fixture
def search_resolve(rf, superuser, indexer):
    def do(*terms, user=superuser, site=None, scopes=None, **args):
        site = site or mcp_site
        request = rf.post("/mcp/")
        request.user = user
        if scopes is not None:
            request.api_token = ApiToken.objects.create_token(user, "t", scopes=scopes)[0]
        terms = [t if isinstance(t, dict) else {"text": t} for t in terms]
        result = site.call_tool(request, "resolve_entities", {"terms": terms, **args})
        if result.is_error:
            return result.data
        return result.data["results"][0] if len(terms) == 1 else result.data["results"]

    return do
