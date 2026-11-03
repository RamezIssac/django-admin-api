import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory

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
