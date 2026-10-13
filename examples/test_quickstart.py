"""The quickstart is run as written, and the README shows exactly that file."""

import importlib.util
import os
import pathlib
import re
import uuid

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def load():
    spec = importlib.util.spec_from_file_location("quickstart_example", HERE / "quickstart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_quickstart_runs(capsys):
    from mongospecs import Registry

    base = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")
    host, _, query = base.partition("?")
    db = f"quickstart_{uuid.uuid4().hex[:8]}"
    uri = f"{host.rstrip('/')}/{db}?{query}"
    try:
        await load().main(uri)
    finally:
        async with Registry(uri) as registry:
            await registry.client.drop_database(db)
    assert capsys.readouterr().out == "Hello, world by ann\ntags: ['intro', 'published']\n"


def test_the_readmes_show_the_quickstart():
    source = (HERE / "quickstart.py").read_text()
    for readme in (ROOT / "README.md", ROOT / "packages" / "mongospecs" / "README.md"):
        blocks = re.findall(r"```python\n(.*?)```", readme.read_text(), re.S)
        assert source in blocks, f"{readme} does not show examples/quickstart.py as it is"


def test_the_package_readme_is_the_root_readme():
    # PyPI shows the package README; it differs from the root one only in where links point.
    root = (ROOT / "README.md").read_text()
    package = (ROOT / "packages" / "mongospecs" / "README.md").read_text()
    blob, tree = (
        "https://github.com/jaykv/mongospecs/blob/main/",
        "https://github.com/jaykv/mongospecs/tree/main/",
    )
    absolute = re.sub(
        r"\]\((?!https?://|#)([^)]+)\)",
        lambda m: f"]({tree if '.' not in m[1].rsplit('/', 1)[-1] else blob}{m[1]})",
        root,
    )
    assert package == absolute
