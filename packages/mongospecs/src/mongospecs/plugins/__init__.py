"""Built-in plugins. Each is activated by a field marker; none adds fields."""

from mongospecs.plugins.revision import Revisions
from mongospecs.plugins.soft_delete import SoftDelete
from mongospecs.plugins.timestamps import Timestamps

__all__ = ["Revisions", "SoftDelete", "Timestamps"]
