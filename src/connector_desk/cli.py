"""Command line: python -m connector_desk run [--dry-run] [--local] [--stale-days N]"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .agent import RunAborted, create_live_session, format_summary, run_agent
from .config import load_config
from .executor import ComposioBackend, new_run_id
from .ledger import Ledger, live_drafts_on
from .llm import make_llm
from .tools_policy import schema_check_slugs
from .local_mode import LocalBackend, ScriptedLLM, local_tool_schemas, write_sample_csv

# Printed after every run. Update it only from real, recorded live runs.
NOT_TESTED_LIVE = [
    "The C008 stage-lock fix in a live run (added after live run 9135a3c3; covered offline)",
    "Draft idempotency across two live runs on the same day (ledger-based)",
    "The Mistral endpoint and the Anthropic provider (live runs used OpenRouter only)",
    "Volume: more than 6 emails, more than 10 tracker rows, more than one model",
]


def _print_footer(led: Ledger) -> None:
    counts = led.counts()
    print("\nLEDGER SUMMARY (this run)")
    for key in ("total_decisions", "allowed", "dry_run", "blocked", "executed_ok", "executed_failed"):
        print(f"  {key}: {counts.get(key, 0)}")
    print(f"  ledger file: {led.path}")
    print("\nNOT TESTED LIVE (by the build; see README Results for real runs)")
    for item in NOT_TESTED_LIVE:
        print(f"  - {item}")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    stale_days = args.stale_days if args.stale_days is not None else cfg.stale_days
    today = date.today()
    run_id = new_run_id()

    if args.local:
        print("LOCAL MODE: fixtures only, no Composio, no Gmail, no Sheets.")
        if args.dry_run:
            print("(--dry-run ignored: local mode never writes anywhere external)")
        mode, backend, tools, sheet_id, already = "local", LocalBackend(today), local_tool_schemas(), "local-sheet", set()
        if args.use_llm:
            problems = cfg.llm_problems()
            if problems:
                print(f"error: --use-llm needs in .env: {', '.join(problems)}", file=sys.stderr)
                return 2
            llm, model = make_llm(cfg)
            print(f"Planner: {cfg.llm_provider} model {model} on local fixtures")
        else:
            llm, model = ScriptedLLM(), "scripted-local-planner"
            print("Planner: deterministic scripted planner (not the real model)")
    else:
        missing = cfg.missing_for_live()
        if missing:
            print(f"error: missing in .env: {', '.join(missing)} (or use --local)", file=sys.stderr)
            return 2
        mode = "dry_run" if args.dry_run else "live"
        print(f"{mode.upper()} MODE" + (": reads are real, every write is stubbed and logged" if args.dry_run else ": drafts and sheet edits are real; nothing is ever sent"))
        session, tools, dropped, missing_tools = create_live_session(cfg.composio_api_key, cfg.composio_user_id)
        if dropped:
            print(f"note: session exposed non-allowed tools, hidden from the model: {dropped}")
        if missing_tools:
            print(f"WARNING: allow-listed slugs not exposed by the session (check slugs/connections): {missing_tools}")
        backend, sheet_id = ComposioBackend(session), cfg.sheet_id
        llm, model = make_llm(cfg)
        print(f"Planner: {cfg.llm_provider} model {model}")
        already = live_drafts_on(cfg.ledger_path, today.isoformat()) if mode == "live" else set()

    led = Ledger(cfg.ledger_path, run_id, mode, day=today.isoformat())
    try:
        report = run_agent(mode=mode, llm=llm, model=model, backend=backend, tools=tools, ledger=led,
                           today=today, stale_days=stale_days, gmail_label=cfg.gmail_label,
                           sheet_id=sheet_id, already_drafted=already)
    except RunAborted as exc:
        print(f"RUN ABORTED: {exc}", file=sys.stderr)
        _print_footer(led)
        return 1
    print()
    print(format_summary(report.summary))
    if report.final_text:
        print(f"\nAGENT SAYS:\n  {report.final_text}")
    for r in report.refused_calls:
        print(f"  refused: {r[:160]}")
    _print_footer(led)
    return 0


def cmd_sample_csv(args: argparse.Namespace) -> int:
    dest = write_sample_csv(date.today(), Path(args.out))
    print(f"wrote {dest} (fictional rows, dates relative to today) - import it as the 'Tracker' tab")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not (cfg.composio_api_key and cfg.composio_user_id):
        print("error: COMPOSIO_API_KEY and COMPOSIO_USER_ID are required", file=sys.stderr)
        return 2
    session, *_ = create_live_session(cfg.composio_api_key, cfg.composio_user_id)
    for kit in ("gmail", "googlesheets"):
        req = session.authorize(kit)
        print(f"{kit}: open this link with the THROWAWAY Google account -> {req.redirect_url}")
    return 0


def _summarize_tool(t: Any) -> dict[str, Any]:
    inputs = (getattr(t, "input_parameters", None) or {})
    outputs = (getattr(t, "output_parameters", None) or {})
    flag = getattr(t, "is_deprecated", None)
    return {
        "deprecated": None if flag is None else bool(flag),  # None: Composio did not say
        "required": list(inputs.get("required") or []),
        "inputs": list((inputs.get("properties") or {}).keys()),
        "outputs": list((outputs.get("properties") or {}).keys()),
        "input_parameters": inputs,
        "output_parameters": outputs,
    }


def cmd_schemas(args: argparse.Namespace) -> int:
    """Print the real Composio definitions of every tool this project names. Needs only the API key."""
    cfg = load_config()
    if not cfg.composio_api_key:
        print("error: COMPOSIO_API_KEY is required", file=sys.stderr)
        return 2
    from composio import Composio

    composio = Composio(api_key=cfg.composio_api_key)
    report: dict[str, Any] = {}
    for slug in schema_check_slugs():
        try:
            info = _summarize_tool(composio.tools.get_raw_composio_tool_by_slug(slug))
        except Exception as exc:  # unknown slug, auth, network: report and keep going
            report[slug] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"{slug}: ERROR {report[slug]['error']}")
            continue
        report[slug] = info
        flag = {True: " DEPRECATED", False: "", None: " (deprecated: unknown)"}[info["deprecated"]]
        print(f"{slug}{flag}\n  required: {', '.join(info['required']) or '-'}\n"
              f"  inputs: {', '.join(info['inputs']) or '-'}\n  outputs: {', '.join(info['outputs']) or '-'}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"\nfull schemas saved to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="connector_desk", description="Connector approvals desk (drafts only).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process partner emails and update the tracker")
    r.add_argument("--dry-run", action="store_true", help="real reads, stubbed writes")
    r.add_argument("--local", action="store_true", help="fixtures only, no network")
    r.add_argument("--stale-days", type=int, default=None)
    r.add_argument("--use-llm", "--use-claude", dest="use_llm", action="store_true",
                   help="with --local: use the configured real model (LLM_PROVIDER) on the fixtures")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("sample-csv", help="write the fixture tracker with real dates for import")
    s.add_argument("--out", default="tracker_import.csv")
    s.set_defaults(func=cmd_sample_csv)
    c = sub.add_parser("connect", help="print OAuth links for gmail and googlesheets")
    c.set_defaults(func=cmd_connect)
    sc = sub.add_parser("schemas", help="print the real Composio tool definitions (needs only COMPOSIO_API_KEY)")
    sc.add_argument("--out", default=str(Path("ledger") / "tool_schemas.json"))
    sc.set_defaults(func=cmd_schemas)
    args = p.parse_args(argv)
    return args.func(args)
