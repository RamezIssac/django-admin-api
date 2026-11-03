"""Smoke test: django-admin-api over a real erp project (ra_erp store app, polymorphic movements)."""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from ra.erp.store.models import Client, ItemMovementItem, Product, Sales, Store

from admin_api.models import ApiToken


class RaErpApiSmoke(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("admin", "a@x.io", "pw")
        stamp = {"owner": cls.user, "lastmod_user": cls.user}
        cls.store = Store.objects.create(name="Main Store", **stamp)
        cls.widget = Product.objects.create(name="Widget", **stamp)
        cls.gadget = Product.objects.create(name="Gadget", **stamp)
        cls.customer = Client.objects.create(name="Test Customer", **stamp)
        _, cls.raw = ApiToken.objects.create_token(cls.user, "smoke")

    def call(self, method, url, payload=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {self.raw}"}
        if method == "get":
            resp = self.client.get(url, **kwargs)
        else:
            resp = getattr(self.client, method)(
                url, data=json.dumps(payload or {}), content_type="application/json", **kwargs
            )
        body = json.loads(resp.content) if resp.content else None
        print(f"\n{method.upper()} {url} -> {resp.status_code}\n{json.dumps(body, indent=1)[:1500]}")
        return resp.status_code, body

    def test_end_to_end(self):
        status, registry = self.call("get", "/api/v1/")
        self.assertEqual(status, 200)
        self.assertIn("sales", {m["model"] for a in registry["apps"] for m in a["models"]})

        status, schema = self.call("get", "/api/v1/store/sales/schema/")
        self.assertEqual(status, 200)
        inline = schema["form"]["inlines"][0]
        prefix = inline["prefix"]

        status, created = self.call(
            "post",
            "/api/v1/store/sales/",
            {
                "slug": "INV-1",
                "date": "2025-04-01T09:00:00+00:00",
                "client": self.customer.pk,
                "store": self.store.pk,
                "inlines": {
                    prefix: [
                        {"product": self.widget.pk, "quantity": "2", "price": "50"},
                        {"product": self.gadget.pk, "quantity": "1", "price": "30"},
                    ]
                },
            },
        )
        self.assertEqual(status, 201, created)
        sale = Sales.objects.get(pk=created["pk"])
        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(sale.type, "sales")
        self.assertEqual(sale.owner, self.user)

        status, listing = self.call("get", "/api/v1/store/sales/?q=INV")
        self.assertEqual(status, 200)
        self.assertEqual(listing["count"], 1)

        status, detail = self.call("get", f"/api/v1/store/sales/{sale.pk}/")
        self.assertEqual(status, 200)
        rows = detail["inlines"][prefix]
        self.assertEqual(len(rows), 2)

        widget_row = next(r for r in rows if r["product"]["pk"] == self.widget.pk)
        gadget_row = next(r for r in rows if r["product"]["pk"] == self.gadget.pk)
        status, patched = self.call(
            "patch",
            f"/api/v1/store/sales/{sale.pk}/",
            {
                "notes": "via api",
                "inlines": {
                    prefix: [{"pk": widget_row["pk"], "quantity": "5"}, {"pk": gadget_row["pk"], "_delete": True}]
                },
            },
        )
        self.assertEqual(status, 200, patched)
        sale.refresh_from_db()
        self.assertEqual(sale.notes, "via api")
        items = list(ItemMovementItem.objects.filter(item_movement=sale))
        self.assertEqual([(i.product_id, i.quantity) for i in items], [(self.widget.pk, Decimal(5))])

        status, preview = self.call("get", f"/api/v1/store/sales/{sale.pk}/delete-preview/")
        self.assertEqual(status, 200)
        self.assertTrue(preview["can_delete"])
        status, _ = self.call("delete", f"/api/v1/store/sales/{sale.pk}/")
        self.assertEqual(status, 200)
        self.assertFalse(Sales.objects.filter(pk=sale.pk).exists())
