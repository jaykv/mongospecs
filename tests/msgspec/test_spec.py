from mongospecs.msgspec import Spec


class Country(Spec):
    _collection = "country"

    name: str
    population: int
