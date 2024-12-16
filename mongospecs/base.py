from __future__ import annotations

import typing as t
from copy import deepcopy

from mongospecs.mixins.crud import CrudMixin
from mongospecs.mixins.index import IndexManagementMixin
from mongospecs.mixins.integrity import IntegrityMixin
from mongospecs.mixins.session import SessionTransactionMixin
from mongospecs.mixins.signal import SignalMixin
from mongospecs.types import SubSpecBaseType

T = t.TypeVar("T")


class SpecBase(CrudMixin, IndexManagementMixin, IntegrityMixin, SessionTransactionMixin, SignalMixin):
    pass


class SubSpecBase(SubSpecBaseType):
    _parent: t.ClassVar[t.Any] = SpecBase

    @classmethod
    def _apply_projection(cls, documents: list[t.Any], projection: t.Mapping[str, t.Any]) -> None:
        # Find reference and sub-spec mappings
        references = {}
        subs = {}
        for key, value in deepcopy(projection).items():
            if not isinstance(value, dict):
                continue

            # Store a reference/sub-spec projection
            if "$ref" in value:
                references[key] = value
            elif "$sub" in value or "$sub." in value:
                subs[key] = value

        # Dereference the documents (if required)
        if references:
            cls._parent._dereference(documents, references)

        # Add sub-specs to the documents (if required)
        if subs:
            cls._parent._apply_sub_specs(documents, subs)

    @classmethod
    def _projection_to_paths(cls, root_key: str, projection: t.Mapping[str, t.Any]) -> t.Any:
        """
        Expand a $sub/$sub. projection to a single projection of True (if
        inclusive) or a map of full paths (e.g `employee.company.tel`).
        """

        # Referenced projections are handled separately so just flag the
        # reference field to true.
        if "$ref" in projection:
            return True

        inclusive = True
        sub_projection: dict[str, t.Any] = {}
        for key, value in projection.items():
            if key in ["$sub", "$sub."]:
                continue

            if key.startswith("$"):
                sub_projection[root_key] = {key: value}
                inclusive = False
                continue

            sub_key = f"{root_key}.{key}"

            if isinstance(value, dict):
                sub_value = cls._projection_to_paths(sub_key, value)
                if isinstance(sub_value, dict):
                    sub_projection |= sub_value
                else:
                    sub_projection[sub_key] = True

            else:
                sub_projection[sub_key] = True
                inclusive = False

        if inclusive:
            # No specific keys so this is inclusive
            return True

        return sub_projection
