"""``mongospecs-bench``: run benchmarks, stress scenarios and the tracing check.

::

    mongospecs-bench run B1 B2            # workloads (default: all)
    mongospecs-bench run B3 --topology sharded
    mongospecs-bench stress               # X1-X7
    mongospecs-bench trace                # Sentry SDK -> rustrak, end to end
    mongospecs-bench report               # markdown summary of the newest results

Add ``--quick`` to check that everything runs; quick numbers are not for conclusions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import typing as t

from mongospecs_bench.harness import RESULTS, Result, Settings, metadata, table, write_result


async def _run(names: list[str], registry: dict[str, t.Any], settings: Settings) -> int:
    meta = await metadata(settings)
    failed = 0
    for name in names:
        fn = registry[name]
        print(f"\n== {name} ({settings.topology}{', quick' if settings.quick else ''})", flush=True)
        result: Result = await fn(settings)
        path = write_result(result, meta)
        print(f"{result.title}\n{result.question}\n")
        print(table(result.rows))
        for note in result.notes:
            print(f"note: {note}")
        if result.conclusion:
            print(result.conclusion)
            failed += result.conclusion.startswith("FAIL")
        print(f"-> {path.relative_to(RESULTS.parent)}")
    return 1 if failed else 0


def report(folder: pathlib.Path | None = None) -> str:
    """Markdown summary of one results folder (default: the most recently written)."""
    if folder is None:
        folders = sorted((p for p in RESULTS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        if not folders:
            return "No results yet. Run `mongospecs-bench run` first."
        folder = folders[-1]
    lines = [f"# Benchmark results ({folder.name})", ""]
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text())
        meta = data["meta"]
        lines += [
            f"## {data['workload']}: {data['title']} ({meta['topology']}{', quick' if meta['quick'] else ''})",
            "",
            data["question"],
            "",
            f"MongoDB {meta['mongodb']}, Python {meta['python']}, {meta['machine']}, {meta['timestamp']}",
            "",
        ]
        rows = data["rows"]
        if rows:
            columns = list(dict.fromkeys(key for row in rows for key in row))
            lines.append("| " + " | ".join(columns) + " |")
            lines.append("|" + "---|" * len(columns))
            lines.extend("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |" for row in rows)
            lines.append("")
        lines.extend(f"- {note}" for note in data["notes"])
        if data["conclusion"]:
            lines += ["", f"**{data['conclusion']}**"]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from mongospecs_bench.stress import SCENARIOS
    from mongospecs_bench.workloads import WORKLOADS

    parser = argparse.ArgumentParser(
        prog="mongospecs-bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, choices in (("run", WORKLOADS), ("stress", SCENARIOS)):
        cmd = sub.add_parser(name)
        cmd.add_argument("names", nargs="*", choices=[*choices, []], default=[])
        cmd.add_argument("--topology", choices=("rs", "sharded"), default="rs")
        cmd.add_argument("--quick", action="store_true")
        cmd.add_argument("--backend", action="append", choices=("msgspec", "pydantic", "attrs"))
    sub.add_parser("trace")
    rep = sub.add_parser("report")
    rep.add_argument("folder", nargs="?")
    args = parser.parse_args(argv)

    if args.command == "trace":
        from mongospecs_bench.tracing import main as trace_main

        return trace_main()
    if args.command == "report":
        print(report(RESULTS / args.folder if args.folder else None))
        return 0
    registry = WORKLOADS if args.command == "run" else SCENARIOS
    settings = Settings(args.topology, args.quick, tuple(args.backend or ("msgspec", "pydantic", "attrs")))
    return asyncio.run(_run(list(args.names) or list(registry), registry, settings))


if __name__ == "__main__":
    sys.exit(main())
