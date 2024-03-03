from pydantic import BaseModel

from mongospecs.pydantic import SpecAdapter

from .fixtures import mongo_client  # noqa


def test_pydantic_adapter(mongo_client) -> None:
    class Car(BaseModel):
        """test"""

        make: str
        model: str
        year: int

    CarSpec = SpecAdapter(Car, collection="cars", client=mongo_client)
    assert CarSpec.get_fields() == {"id", "make", "model", "year"}

    car = CarSpec(make="test", model="test123", year=2000)
    assert car.to_dict() == {"_id": None, "make": "test", "model": "test123", "year": 2000}
    assert car.get_fields() == {"id", "make", "model", "year"}

    car.insert()
    assert car.find({"make": "test"}) == [{"_id": car._id, "make": "test", "model": "test123", "year": 2000}]
    assert car.to_dict() == {"_id": car._id, "make": "test", "model": "test123", "year": 2000}
