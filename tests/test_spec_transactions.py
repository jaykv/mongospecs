from unittest.mock import MagicMock, patch

import pytest
from pymongo.errors import ConnectionFailure, OperationFailure

from mongospecs.pydantic import Spec


@pytest.fixture
def mock_client():
    """Fixture to create a mock MongoDB client with operation verification."""
    with patch("pymongo.MongoClient") as mock_client:
        # Create mock session and database
        mock_session = MagicMock()
        mock_db = MagicMock()
        mock_db.Spec = MagicMock()

        # Set up returns
        mock_client.start_session.return_value = mock_session
        mock_client.get_default_database.return_value = mock_db

        # Store session for verification
        mock_client.mock_session = mock_session
        mock_client.mock_db = mock_db

        def verify_operation_used_session(operation_name):
            """Verify that a database operation was called with the session."""
            operation = getattr(mock_db.Spec, operation_name)
            assert any(
                call.kwargs.get("session") == mock_session for call in operation.call_args_list
            ), f"{operation_name} was not called with the correct session"

        # Add verification helper
        mock_client.verify_operation_used_session = verify_operation_used_session

        yield mock_client


@pytest.fixture(autouse=True)
def setup_and_teardown(mock_client):
    """Setup and teardown for each test."""
    Spec._client = mock_client
    yield
    Spec._client = None  # Reset after each test


def test_operations_use_session(mock_client):
    with Spec.transaction() as session:
        spec_instance = Spec()
        spec_instance.insert(session=session)
        spec_instance.update(session=session)
        Spec.find(session=session)
        Spec.find_one(session=session)
        mock_client.verify_operation_used_session("insert_one")
        mock_client.verify_operation_used_session("update_one")
        mock_client.verify_operation_used_session("find")
        mock_client.verify_operation_used_session("find_one")

    session.commit_transaction.assert_called_once()
    session.end_session.assert_called_once()


def test_transaction_success(mock_client):
    """Test that a transaction can be successfully committed."""
    with Spec.transaction() as session:
        # Simulate successful operations
        session.commit_transaction.assert_not_called()  # Ensure commit is not called yet

        # Simulate an insert operation
        spec_instance = Spec()
        spec_instance.insert(session=session)

    # Commit should be called after the operations
    session.commit_transaction.assert_called_once()
    session.end_session.assert_called_once()


@pytest.mark.parametrize("exception", [OperationFailure, ConnectionFailure])
def test_transaction_abort(mock_client, exception):
    """Test that a transaction is aborted on error."""
    with pytest.raises(exception):
        with Spec.transaction() as session:
            # Simulate an operation that raises an exception
            raise exception("Simulated failure")

        # Ensure that abort_transaction is called
        session.abort_transaction.assert_called_once()


def test_missing_client():
    """Test that a RuntimeError is raised if _client is not set."""
    Spec._client = None  # Ensure the client is not set
    with pytest.raises(RuntimeError, match=r"MongoDB client \(_client\) is not set\. Cannot start a transaction\."):
        with Spec.transaction():
            pass


def test_transaction_signals_committed(mock_client):
    """Test that transaction signals are emitted correctly."""
    committed_signal_received = []

    def record_commit(sender):
        committed_signal_received.append(sender)

    Spec.listen("transaction_committed", record_commit)

    with Spec.transaction() as session:
        # Simulate successful operations
        spec_instance = Spec()
        spec_instance.insert(session=session)

    assert len(committed_signal_received) == 1
    assert committed_signal_received[0] == Spec

    Spec.stop_listening("transaction_committed", record_commit)


def test_transaction_signals_aborted(mock_client):
    """Test that transaction signals are emitted correctly for aborted."""
    aborted_signal_received = []

    def abort_commit(sender):
        aborted_signal_received.append(sender)

    Spec.listen("transaction_aborted", abort_commit)

    with pytest.raises(OperationFailure):
        with Spec.transaction() as session:
            spec_instance = Spec()
            spec_instance.insert(session=session)
            raise OperationFailure("Simulated operation failure")

    assert len(aborted_signal_received) == 1
    assert aborted_signal_received[0] == Spec

    Spec.stop_listening("transaction_aborted", abort_commit)
