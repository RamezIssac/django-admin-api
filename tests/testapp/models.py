import datetime

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
