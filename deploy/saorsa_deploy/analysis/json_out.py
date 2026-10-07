"""One destination for an `analysis` command's `--json-out`, so stdout stays parseable.

Every `analysis` command that can write its result as JSON grew its own copy of
`if args.json_out: open(...).write(...)`, and most of them also print the
object, a table or a `wrote <path>` trailer to stdout for the human reading the
transcript. That is fine for a file, and wrong for `/dev/stdout`: the JSON and
the human output land on the same stream, so a caller piping the command into
`jq` gets a concatenation of two objects plus a trailer line, which `jq` aborts
on rather than parsing. The `// 0` default in the shell helper then turns the
abort into a confident **0** -- which is how DEV-06 run 647 (V2-1438) reported
thirty zeros for a fleet that was demonstrably reachable.

So the destination decides the output:

* **`-` or `/dev/stdout`** -- "the JSON *is* the output". The object goes to
  stdout exactly once; no trailer, and :func:`console_for` hands the command a
  quiet console so its tables and preambles are suppressed too.
* **a path** -- today's behaviour. The object is written there and the human
  lines, the echo and the `wrote <path>` trailer all still go to stdout.
* **omitted** -- nothing is written; the command's human output is all there is.

The `tail -1` mitigation carried in several run issues does not cover this: the
failure is a parse abort on two concatenated objects, not a stray preamble line.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console

#: The two spellings a caller uses to mean "write the JSON to my stdout". `-`
#: is the conventional one; `/dev/stdout` is what the `cnum()` helper in the run
#: issues' `phases.sh` actually passes, so both have to be recognised.
STDOUT_TARGETS = frozenset({"-", "/dev/stdout"})


def is_stdout(json_out: str | None) -> bool:
    """True when the JSON destination is the stream the human output shares."""
    return json_out in STDOUT_TARGETS


def console_for(json_out: str | None) -> Console:
    """The console a command should print its human output through.

    Quiet when the JSON is going to stdout, so a table, a preamble or a `[dim]`
    footnote cannot end up in the caller's `jq` input.
    """
    return Console(quiet=is_stdout(json_out))


def emit_json(obj: Any, json_out: str | None, console: Console, *, echo: bool = False) -> None:
    """Write `obj` as JSON to `json_out`, and never twice to one stream.

    `echo` prints the object to stdout as the command's own human output, which
    the ES extractors do so a run issue's transcript carries the numbers
    inline. It is suppressed when stdout is the JSON destination -- echoing
    there is what emitted the object twice.
    """
    text = json.dumps(obj, indent=2)
    if is_stdout(json_out):
        sys.stdout.write(text + "\n")
        return
    if echo:
        print(text)
    if not json_out:
        return
    with open(json_out, "w") as fh:
        fh.write(text + "\n")
    console.print(f"wrote {json_out}")


def add_json_out_arg(parser) -> None:
    """The `--json-out` argument, with `-` documented where a caller will see it."""
    parser.add_argument(
        "--json-out",
        default=None,
        help=(
            "Write the result as JSON here. `-` or `/dev/stdout` writes it to stdout "
            "instead, as the command's only output, so it can be piped into `jq`."
        ),
    )
