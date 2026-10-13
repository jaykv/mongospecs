import logging

import pytest
from pymongo import errors as pe

from mongospecs import (
    ConfigurationError,
    ConnectionFailed,
    DocumentTooLarge,
    DriverError,
    DuplicateKeyError,
    EncodingError,
    MongoSpecsError,
    OperationFailed,
    OperationTimeout,
    PoolTimeout,
    Q,
    Registry,
    RegistryClosedError,
    logging_listener,
)
from mongospecs.errors import BulkWriteFailed, translate

from .conftest import RS_URI


def test_constructing_a_registry_does_no_io(m):
    reg = Registry("mongodb://203.0.113.1:1/none", models=[m.User], serverSelectionTimeoutMS=50)
    assert not reg.is_open and reg.models == (m.User,)
    with pytest.raises(RegistryClosedError):
        _ = reg.client
    with pytest.raises(RegistryClosedError):
        m.User.find().compile() and m.User.collection()


async def test_operations_fail_immediately_when_not_open(m, db_name):
    reg = Registry(RS_URI, database=db_name, models=[m.User])
    with pytest.raises(RegistryClosedError):
        await m.User(name="x").insert()  # no command buffering
    await reg.open()
    await reg.ping()
    await m.User(name="x").insert()
    await reg.client.drop_database(db_name)
    await reg.close()
    await reg.close()  # idempotent
    with pytest.raises(RegistryClosedError):
        await reg.open()  # closing is final
    with pytest.raises(RegistryClosedError):
        reg.bind(m.Article)
    from mongospecs import NotBoundError

    with pytest.raises(NotBoundError):
        await m.User.count()


async def test_unreachable_server_is_a_connection_error(m, db_name):
    async with Registry(
        "mongodb://127.0.0.1:1", database=db_name, models=[m.User], serverSelectionTimeoutMS=200
    ):
        with pytest.raises(ConnectionFailed) as info:
            await m.User.count()
        assert isinstance(info.value.__cause__, pe.PyMongoError)


async def test_missing_database_name_is_reported(m):
    async with Registry(RS_URI, models=[m.User]):
        with pytest.raises(ConfigurationError, match="database"):
            m.User.collection()


def test_every_driver_error_maps_to_one_error():
    cases = [
        (pe.DuplicateKeyError("dup", 11000, {"keyValue": {"email": "x"}}), DuplicateKeyError),
        (pe.ExecutionTimeout("slow", 50), OperationTimeout),
        (pe.NetworkTimeout("net"), OperationTimeout),
        (pe.WTimeoutError("w", 64), OperationTimeout),
        (pe.WaitQueueTimeoutError("pool"), PoolTimeout),
        (pe.ServerSelectionTimeoutError("sel"), ConnectionFailed),
        (pe.AutoReconnect("re"), ConnectionFailed),
        (pe.NotPrimaryError("np"), ConnectionFailed),
        (pe.ConnectionFailure("cf"), ConnectionFailed),
        (pe.WriteError("we", 2), OperationFailed),
        (pe.WriteConcernError("wc", 64), OperationFailed),
        (pe.CursorNotFound("cnf", 43), OperationFailed),
        (pe.OperationFailure("too big", 10334), DocumentTooLarge),
        (pe.OperationFailure("op", 1), OperationFailed),
        (pe.DocumentTooLarge("big"), DocumentTooLarge),
        (pe.InvalidOperation("inv"), DriverError),
        (pe.ConfigurationError("cfg"), DriverError),
        (pe.PyMongoError("generic"), DriverError),
        (pe.BulkWriteError({"writeErrors": [{"index": 0, "code": 2, "errmsg": "x"}]}), BulkWriteFailed),
        (
            pe.BulkWriteError(
                {"writeErrors": [{"index": 0, "code": 11000, "errmsg": "x", "keyValue": {"a": 1}}]}
            ),
            DuplicateKeyError,
        ),
    ]
    for exc, expected in cases:
        out = translate(exc)
        assert type(out) is expected, (exc, out)
        assert isinstance(out, MongoSpecsError)
        assert not isinstance(out, pe.PyMongoError)  # wrapped, never subclassed
    assert translate(cases[0][0]).fields == {"email": "x"}
    assert translate(cases[-1][0]).fields == {"a": 1}
    from bson.errors import InvalidDocument

    assert type(translate(InvalidDocument("bad"))) is EncodingError
    same = MongoSpecsError("x")
    assert translate(same) is same


async def test_timeout_and_encoding_errors_from_real_operations(m, registry):
    await m.User.insert_many([m.User(name=str(i)) for i in range(50)])
    from mongospecs import trusted

    with pytest.raises(OperationTimeout):
        await m.User.find(trusted({"$where": "sleep(200) || true"})).options(max_time_ms=20).all()

    class Weird:
        pass

    with pytest.raises(EncodingError):
        await m.User.collection().insert_one({"x": Weird()}) if False else await _insert_weird(m, Weird())


async def _insert_weird(m, value):
    from mongospecs import Set

    await m.User.find().update(Set({"address.city": value}))


async def test_default_max_time_ms_from_registry_and_model(m, make_registry):
    events = []
    await make_registry(m.User, max_time_ms=1234, on_command=events.append)
    await m.User.find().all()
    await m.User.find().options(max_time_ms=99).all()
    await m.User.count()
    finds = [e.command for e in events if e.phase == "started" and e.command_name == "find"]
    assert [c["maxTimeMS"] for c in finds] == [1234, 99]
    counts = [e.command for e in events if e.phase == "started" and e.command_name == "aggregate"]
    assert counts[0]["maxTimeMS"] == 1234
    # every path that runs a pipeline carries the default too (found by the storefront e2e tests)
    events.clear()
    await m.User.aggregate([{"$match": {}}])
    await m.User.find().union(m.User.find())
    pipelines = [e.command for e in events if e.phase == "started" and e.command_name == "aggregate"]
    assert len(pipelines) >= 2 and all(c["maxTimeMS"] == 1234 for c in pipelines)


async def test_command_events_carry_model_and_operation(m, make_registry, caplog):
    events = []
    await make_registry(m.User, on_command=[events.append, logging_listener(level=logging.INFO)])
    with caplog.at_level(logging.INFO, logger="mongospecs.commands"):
        user = await m.User(name="ann").insert()
        await m.User.find(Q.name == "ann").all()
        await m.User.update(user.id, age=3)
        user.age = 4
        await user.save()
        await m.User.count()
        await user.delete()
        with pytest.raises(OperationFailed):
            await m.User.aggregate([{"$nope": 1}])
    seen = [(e.model, e.op, e.command_name) for e in events if e.phase == "started"]
    assert seen == [
        ("User", "insert", "insert"),
        ("User", "find", "find"),
        ("User", "update", "findAndModify"),
        ("User", "save", "update"),
        ("User", "count", "aggregate"),
        ("User", "delete", "delete"),
        ("User", "aggregate", "aggregate"),
    ]
    ok = [e for e in events if e.phase == "succeeded"]
    assert len(ok) == 6 and all(e.duration_ms is not None and e.duration_ms >= 0 for e in ok)
    (failed,) = [e for e in events if e.phase == "failed"]
    assert failed.op == "aggregate" and failed.failure
    text = caplog.text
    assert "User.insert" in text and "ok in" in text and "failed in" in text


async def test_raw_driver_use_is_reported_without_model(m, make_registry):
    events = []
    reg = await make_registry(m.User, on_command=events.append)
    await reg.database().command("ping")
    assert [(e.model, e.op) for e in events if e.phase == "started"] == [(None, None)]


async def test_otel_listener_creates_spans(m, make_registry):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from mongospecs import otel_listener

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    await make_registry(m.User, on_command=otel_listener(provider.get_tracer("test")))
    await m.User(name="ann").insert()
    await m.User.find().all()
    with pytest.raises(OperationFailed):
        await m.User.aggregate([{"$nope": 1}])
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["User.insert", "User.find", "User.aggregate"]
    assert spans[0].attributes["db.system"] == "mongodb" and spans[0].attributes["mongospecs.model"] == "User"
    assert spans[2].status.status_code.name == "ERROR" and spans[0].status.status_code.name != "ERROR"


async def test_sentry_listener_records_spans_in_the_active_transaction(m, make_registry):
    import sentry_sdk
    from sentry_sdk.transport import Transport

    from mongospecs import sentry_listener

    captured = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            captured.extend(item.payload.json for item in envelope.items if item.type == "transaction")

    sentry_sdk.init(
        dsn="http://key@localhost:1/1",
        traces_sample_rate=1.0,
        transport=Capture,
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    try:
        await make_registry(m.User, on_command=sentry_listener())
        with sentry_sdk.start_transaction(name="request", op="http.server"):
            await m.User(name="ann").insert()
            await m.User.find().all()
            with pytest.raises(OperationFailed):
                await m.User.aggregate([{"$nope": 1}])
        await m.User.count()  # outside any transaction: nothing to attach to, nothing sent
        sentry_sdk.flush()
    finally:
        # Switch the SDK off again, without letting it auto-enable framework integrations.
        sentry_sdk.init(dsn=None, default_integrations=False, auto_enabling_integrations=False)
    (event,) = captured
    spans = event["spans"]
    assert [s["description"] for s in spans] == ["User.insert", "User.find", "User.aggregate"]
    assert all(s["op"] == "db" and s["origin"] == "auto.db.mongospecs" for s in spans)
    assert [s["status"] for s in spans] == ["ok", "ok", "internal_error"]
    assert spans[0]["data"]["db.system"] == "mongodb" and spans[0]["data"]["mongospecs.model"] == "User"
    assert {s["trace_id"] for s in spans} == {event["contexts"]["trace"]["trace_id"]}
