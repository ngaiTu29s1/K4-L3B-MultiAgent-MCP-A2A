from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, timestamped_submission_path, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, *, resume: bool = False, force_cases: set[str] | None = None) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    force_cases = force_cases or set()
    if force_cases and not resume:
        raise ValueError("--force-case requires --resume")
    if unknown := force_cases - set(case_set.case_ids):
        raise ValueError(f"unknown --force-case IDs: {sorted(unknown)}")
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    elif any(output_root.glob("*.json")) and not trace_path.is_file():
        raise ValueError("cannot resume outputs without traces/trace.jsonl")
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for index, case_id in enumerate(case_set.case_ids, 1):
            target = output_root / f"{case_id}.json"
            if resume and target.is_file() and case_id not in force_cases:
                existing = json.loads(target.read_text(encoding="utf-8"))
                contracts.validate_output(existing, f"outputs/{case_id}.json")
                if existing.get("case_id") != case_id:
                    raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
                continue
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            if index % 10 == 0 or index == len(case_set.case_ids):
                print(f"Completed through {case_id} ({index}/{len(case_set.case_ids)})", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--resume", action="store_true", help="skip validated outputs already written")
    run.add_argument(
        "--force-case", action="append", default=[], help="rerun one case with --resume"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument(
        "--output",
        help="explicit ZIP path (default: dist/submission-YYYYMMDDTHHMMSSZ.zip)",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume, force_cases=set(args.force_case)))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = (
                root / args.output if args.output else timestamped_submission_path(root / "dist")
            )
            destination = package_submission(root, destination)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
