import datetime
import uuid

from django.db import models


class Client(models.Model):
    name = models.CharField(max_length=100)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=30)

    def __str__(self):
        return self.name


class Sales(models.Model):
    STATUS = [("draft", "Draft"), ("posted", "Posted")]

    slug = models.CharField(max_length=20)
    date = models.DateField(default=datetime.date(2025, 1, 1))
    client = models.ForeignKey(Client, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=STATUS, default="draft")
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tags = models.ManyToManyField(Tag, blank=True)

    class Meta:
        verbose_name_plural = "sales"

    def __str__(self):
        return self.slug


class SalesItem(models.Model):
    sales = models.ForeignKey(Sales, on_delete=models.CASCADE, related_name="items")
    product = models.CharField(max_length=50)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.quantity} x {self.product}"


class Secret(models.Model):
    name = models.CharField(max_length=100)


class Payment(models.Model):
    # clients with notes are on hold and cannot receive payments
    client = models.ForeignKey(Client, on_delete=models.PROTECT, limit_choices_to={"notes": ""})
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)


class PaymentLine(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="lines")
    sales = models.ForeignKey(Sales, on_delete=models.PROTECT)


class Party(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    joined = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "parties"

    def __str__(self):
        return self.name


class Vendor(Party):
    """A multi-table child of Party with no admin of its own."""

    city = models.CharField(max_length=50, blank=True)


class Ticket(models.Model):
    """A UUID primary key (the search engine's EXISTS scoping per pk type)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    title = models.CharField(max_length=50)

    def __str__(self):
        return self.title


class Voucher(models.Model):
    """A text primary key."""

    code = models.CharField(max_length=20, primary_key=True)
    title = models.CharField(max_length=50)

    def __str__(self):
        return self.title


class Holiday(models.Model):
    """A date primary key: neither an integer, a UUID nor text."""

    day = models.DateField(primary_key=True)
    title = models.CharField(max_length=50)

    def __str__(self):
        return self.title
