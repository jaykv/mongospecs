from mongospecs.pydantic import Spec


class Country(Spec):
    _collection = "country"

    name: str
    population: int
