import attrs

from mongospecs.attrs import SpecAdapter

from .fixtures import mongo_client  # noqa


def test_attrs_adapter(mongo_client) -> None:
    @attrs.define(kw_only=True)
    class Car:
        make: str
        model: str

    CarSpec = SpecAdapter(Car, collection="cars", client=mongo_client)
    assert CarSpec.get_fields() == {"make", "model"}
    car = CarSpec(make="test", model="test123")
    assert car.to_dict() == {"make": "test", "model": "test123"}
    assert car.get_fields() == {"make", "model"}
    car.insert()

    assert car.find({"make": "test"}) == [{"_id": car._id, "make": "test", "model": "test123"}]
