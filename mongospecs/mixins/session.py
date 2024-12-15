import typing as t
from contextlib import contextmanager

from blinker import signal
from pymongo.client_session import ClientSession
from pymongo.errors import ConnectionFailure, OperationFailure

from mongospecs.mixins.base import MongoBaseMixin


class SessionTransactionMixin(MongoBaseMixin):
    @classmethod
    @contextmanager
    def transaction(cls, **start_transaction_kwargs: t.Any) -> t.Generator[ClientSession, t.Any, None]:
        """Context manager for handling MongoDB transactions."""
        if not cls._client:
            raise RuntimeError("MongoDB client (_client) is not set. Cannot start a transaction.")
        session = cls._client.start_session()
        session.start_transaction(**start_transaction_kwargs)

        try:
            yield session  # Allow operations to be performed within this session
            session.commit_transaction()  # Commit if no exceptions
            signal("transaction_committed").send(cls)  # Emit signal after commit
        except (ConnectionFailure, OperationFailure) as e:
            session.abort_transaction()  # Abort on error
            signal("transaction_aborted").send(cls)  # Emit signal after abort
            raise e  # Re-raise the exception for handling
        finally:
            session.end_session()
