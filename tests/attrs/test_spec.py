from attrs import define


@define
class Country:
    _collection = "country"

    name: str
    population: int
