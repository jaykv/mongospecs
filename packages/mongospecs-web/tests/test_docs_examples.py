"""The guide pages are executable: every ``python`` block on a page runs, in order, against MongoDB.

Blocks fenced as ``py`` are illustrative and are not run.
"""

import ast
import pathlib
import re
import uuid

import pytest

DOCS = pathlib.Path(__file__).resolve().parents[3] / "docs"
PAGES = sorted(p for p in (*DOCS.glob("guide/*.md"), *DOCS.glob("web/*.md")) if "```python" in p.read_text())
BLOCK = re.compile(r"```python\n(.*?)```", re.S)
RS = "mongodb://localhost:27117/{db}?replicaSet=rs0"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_page_examples_run(page):
    import os

    db = f"ms_docs_{uuid.uuid4().hex[:10]}"
    uri = os.environ.get("MONGOSPECS_TEST_URI", RS.format(db=db))
    if "{db}" not in RS or db not in uri:  # a custom URI without a database: add ours
        base, _, query = uri.partition("?")
        uri = f"{base.rstrip('/')}/{db}" + (f"?{query}" if query else "")
    source = "\n".join(BLOCK.findall(page.read_text()))
    assert source.strip(), f"{page} has no runnable examples"
    source = (
        source.replace("mongodb://localhost/app", uri)
        .replace('"app_archive"', f'"{db}_archive"')
        .replace('drop_database("app")', f'drop_database("{db}")')
    )
    code = compile(source, str(page), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    namespace = {"__name__": f"docs_{page.stem}"}
    result = eval(code, namespace)
    if result is not None:
        await result


def test_every_guide_page_is_covered():
    assert {p.stem for p in PAGES} >= {
        "models",
        "queries",
        "writes",
        "relations",
        "plugins",
        "operations",
        "resources",
    }
