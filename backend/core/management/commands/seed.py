import random

from django.core.management.base import BaseCommand

from core.models import Vehicle


class Command(BaseCommand):
    help = "Wipe and reseed demo data (deterministic via fixed random seed)."

    def handle(self, *args, **options):
        random.seed(42)  # same data every run -> reliable demo
        Vehicle.objects.all().delete()

        statuses = ["active", "active", "active", "idle", "maintenance"]
        vehicles = [
            Vehicle(name=f"Truck {100 + i}", status=random.choice(statuses))
            for i in range(10)
        ]
        Vehicle.objects.bulk_create(vehicles)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(vehicles)} vehicles"))
