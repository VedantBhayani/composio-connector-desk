"""Command line: python -m connector_desk run [--dry-run] [--local] [--stale-days N]"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .agent import RunAborted, create_live_session, format_summary, run_agent
from .config import load_config
from .executor import ComposioBackend, new_run_id
from .ledger import Ledger, live_drafts_on
from .local_mode import LocalBackend, ScriptedLLM, local_tool_schemas, write_sample_csv

# Printed after every run. Update it only from real, recorded live runs.
NOT_TESTED_LIVE = [
    "Creating a real Composio session with this config (toolkits, tools.enable, direct_tools preset, require_approval, sandbox off)",
    "Exact tool slugs and argument names for Gmail/Sheets (docs.composio.dev was unreachable at build time)",
    "Gmail fetch by 'label:<GMAIL_LABEL>' query and the shape of its response",
    "Sheets batch-get/batch-update argument shapes and the Status tab write",
    "Real Claude behaviour on email 06 (prompt injection) and on matching emails 01-05",
    "Draft idempotency across live runs (ledger-based) and draft wording quality",
    "`connect` OAuth flow for gmail and googlesheets",
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
        if args.use_claude:
            if not (cfg.anthropic_api_key and cfg.anthropic_model):
                print("error: --use-claude needs ANTHROPIC_API_KEY and ANTHROPIC_MODEL in .env", file=sys.stderr)
                return 2
            import anthropic
            llm, model = anthropic.Anthropic(api_key=cfg.anthropic_api_key), cfg.anthropic_model
            print(f"Planner: Claude ({model}) on local fixtures")
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
        import anthropic
        session, tools, dropped, missing_tools = create_live_session(cfg.composio_api_key, cfg.composio_user_id)
        if dropped:
            print(f"note: session exposed non-allowed tools, hidden from the model: {dropped}")
        if missing_tools:
            print(f"WARNING: allow-listed slugs not exposed by the session (check slugs/connections): {missing_tools}")
        backend, sheet_id = ComposioBackend(session), cfg.sheet_id
        llm, model = anthropic.Anthropic(api_key=cfg.anthropic_api_key), cfg.anthropic_model
        already = live_drafts_on(cfg.ledger_path, today.isoformat()) if mode == "live" else set()

    led = Ledger(cfg.ledger_path, run_id, mode)
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="connector_desk", description="Connector approvals desk (drafts only).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process partner emails and update the tracker")
    r.add_argument("--dry-run", action="store_true", help="real reads, stubbed writes")
    r.add_argument("--local", action="store_true", help="fixtures only, no network")
    r.add_argument("--stale-days", type=int, default=None)
    r.add_argument("--use-claude", action="store_true", help="with --local: use the real model on fixtures")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("sample-csv", help="write the fixture tracker with real dates for import")
    s.add_argument("--out", default="tracker_import.csv")
    s.set_defaults(func=cmd_sample_csv)
    c = sub.add_parser("connect", help="print OAuth links for gmail and googlesheets")
    c.set_defaults(func=cmd_connect)
    args = p.parse_args(argv)
    return args.func(args)
