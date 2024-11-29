from datetime import datetime

import pytest
from mongomock import MongoClient

from mongospecs.pydantic import Spec

from .models import ComplexDragon, Inventory, Lair

# Fixtures


@pytest.fixture
def mongo_client(request):
    """Connect to the test database"""

    # Connect to mongodb and create a test database
    Spec._client = MongoClient("mongodb://localhost:27017/mongospecs_test")

    yield Spec._client

    Spec._client.drop_database("mongospecs_test")
    del Spec._client


@pytest.fixture
def example_dataset_one(request):
    """Create an example set of data that can be used in testing"""
    inventory = Inventory(gold=1000, skulls=100)

    cave = Lair(name="Cave", inventory=inventory)
    cave.insert()

    burt = ComplexDragon(
        name="Burt", dob=datetime(1979, 6, 11), breed="Cold-drake", lair=cave, traits=["irritable", "narcissistic"]
    )
    burt.insert()


@pytest.fixture
def example_dataset_many(request):
    """Create an example set of data that can be used in testing"""

    # Burt
    cave = Lair(name="Cave", inventory=Inventory(gold=1000, skulls=100))
    cave.insert()

    burt = ComplexDragon(
        name="Burt", dob=datetime(1979, 6, 11), breed="Cold-drake", lair=cave, traits=["irritable", "narcissistic"]
    )
    burt.insert()

    # Fred
    castle = Lair(name="Castle", inventory=Inventory(gold=2000, skulls=200))
    castle.insert()

    fred = ComplexDragon(
        name="Fred", dob=datetime(1980, 7, 12), breed="Fire-drake", lair=castle, traits=["impulsive", "loyal"]
    )
    fred.insert()

    # Fred
    mountain = Lair(name="Mountain", inventory=Inventory(gold=3000, skulls=300))
    mountain.insert()

    albert = ComplexDragon(
        name="Albert", dob=datetime(1981, 8, 13), breed="Stone dragon", lair=mountain, traits=["reclusive", "cunning"]
    )
    albert.insert()
