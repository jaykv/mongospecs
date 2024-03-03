from msgspec import Struct

from mongospecs.empty import Empty
from mongospecs.msgspec import SpecAdapter

from .fixtures import mongo_client  # noqa


def test_msgspec_adapter(mongo_client) -> None:
    class Car(Struct):
        make: str
        model: str
        year: int

    CarSpec = SpecAdapter(Car, collection="cars", client=mongo_client)

    assert CarSpec.get_fields() == {"_id", "make", "model", "year"}
    car = CarSpec(make="test", model="test123", year=2000)
    assert car.to_dict() == {"_id": Empty, "make": "test", "model": "test123", "year": 2000}
    assert car.get_fields() == {"_id", "make", "model", "year"}

    car.insert()
    assert car.find({"make": "test"}) == [{"_id": car._id, "make": "test", "model": "test123", "year": 2000}]
    assert car.to_dict() == {"_id": car._id, "make": "test", "model": "test123", "year": 2000}
