"""
Support for paginating specs.
"""

import math
import typing as t
from copy import deepcopy
from dataclasses import dataclass

from mongospecs.base import SpecBase
from mongospecs.helpers.query import Condition, Group
from mongospecs.utils import to_refs

__all__ = (
    # Exceptions
    "InvalidPage",
    # Classes
    "Page",
    "Paginator",
)


T = t.TypeVar("T")
TSpec = t.TypeVar("TSpec", bound=SpecBase)


class InvalidPage(Exception):
    """
    An error raised when an invalid page is requested.
    """


@dataclass
class Page(t.Generic[T]):
    """
    A class to represent one page of results.
    """

    offset: int
    """The offset of the first result in the page from the first result of
        the entire selection.
    """

    number: int
    """The page number"""

    items: list[T]
    """The results/specs for this page"""

    next: t.Optional[int]
    """Next page number"""

    prev: t.Optional[int]
    """Previous page number"""

    def __getitem__(self, i: int) -> T:
        return self.items[i]

    def __iter__(self) -> t.Iterator[T]:
        yield from self.items

    def __len__(self) -> int:
        return len(self.items)

    def get_offset(self, item: t.Any) -> int:
        """Return the offset for an item in the page"""
        return self.offset + self.items.index(item)


# TODO: REFACTOR


@dataclass
class Paginator(t.Generic[TSpec]):
    """
    A pagination class for slicing query results into pages. This class is
    designed to work with Spec classes.
    """

    spec_cls: type[TSpec]
    """The spec class results are being paginated for"""

    filter: t.Optional[t.Union[dict[str, t.Any], Condition, Group]] = None
    """The filter applied when selecting results from the database"""

    per_page: int = 20
    """The number of results that will be displayed per page"""

    orphans: int = 0
    """ If a value is specified for orphans then the last page will be able to
        hold the additional results (up to the value of orphans). This can
        help prevent users being presented with pages contain only a few
        results.
    """

    filter_kwargs: t.Any = None
    """Any additional filter arguments applied when selecting results such as sort and projection"""

    def __post_init__(self) -> None:
        # flattern the filter at this point which effectively deep copies.
        if isinstance(self.filter, (Condition, Group)):
            self.filter = self.filter.to_dict()
        else:
            self.filter = to_refs(self.filter)

        # Count the total results being paginated
        self._items_count = self.spec_cls.count(self.filter)

        # Calculated the number of pages
        total = self._items_count - self.orphans
        self._page_count = max(1, int(math.ceil(total / float(self.per_page))))

        # Create a list of page number that can be used to navigate the results
        self._page_numbers = range(1, self._page_count + 1)

    def __getitem__(self, page_number: int) -> Page[TSpec]:
        if page_number not in self._page_numbers:
            raise InvalidPage(page_number, self.page_count)

        # Calculate the next and previous page numbers
        next = page_number + 1 if page_number + 1 in self._page_numbers else None
        prev = page_number - 1 if page_number - 1 in self._page_numbers else None

        # Select the items for the page
        filter_args = deepcopy(self.filter_kwargs) or {}
        filter_args["skip"] = (page_number - 1) * self.per_page
        filter_args["limit"] = self.per_page

        # Check to see if we need to account for orphans
        if self.item_count - (page_number * self.per_page) <= self.orphans:
            filter_args["limit"] += self.orphans

        # Select the results for the page
        items = self.spec_cls.many(self.filter, **filter_args)

        # Build the page
        return Page(offset=filter_args["skip"], number=page_number, items=items, next=next, prev=prev)

    def __iter__(self) -> t.Generator[Page[TSpec], t.Any, t.Any]:
        for page_number in self._page_numbers:
            yield self[page_number]

    # Read-only properties
    @property
    def item_count(self) -> int:
        """Return the total number of items being paginated"""
        return self._items_count

    @property
    def page_count(self) -> int:
        """Return the total number of pages"""
        return self._page_count

    @property
    def page_numbers(self) -> range:
        """Return a list of page numbers"""
        return self._page_numbers
