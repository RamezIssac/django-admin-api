from django.db import models


class Client(models.Model):
    name = models.CharField(max_length=100)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Sales(models.Model):
    slug = models.CharField(max_length=20)
    client = models.ForeignKey(Client, on_delete=models.PROTECT)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return self.slug


class Secret(models.Model):
    name = models.CharField(max_length=100)
