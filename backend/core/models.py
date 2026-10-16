from django.db import models


# Placeholder model so seed/admin work end to end before the challenge.
# Replace or extend once we know the problem (vehicles, drivers, trips, events...).
class Vehicle(models.Model):
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=30, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def to_dict(self):
        return {"id": self.id, "name": self.name, "status": self.status}
