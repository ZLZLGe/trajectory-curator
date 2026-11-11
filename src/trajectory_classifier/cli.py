"""Separate command line entry point for scenario/capability/subject classification."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from tempfile import gettempdir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Classify normalized trajectories by scenario, capability, and subject."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("local", "s3"):
        command = commands.add_parser(
            name, help=f"Classify a normalized {name} dataset"
        )
        command.add_argument(
            "--input", required=True, help="Input root containing manifest.json"
        )
        command.add_argument(
            "--output", required=True, help="Separate classification output root"
        )
        command.add_argument("--api-url", default=os.environ.get("CLASSIFIER_API_URL"))
        command.add_argument("--model", default="deepseek-v4.1-flash")
        command.add_argument(
            "--api-key-file", type=Path, help="Alternatively set CLASSIFIER_API_KEY"
        )
        command.add_argument("--taxonomy", type=Path)
        command.add_argument("--workers", type=int, default=4)
        command.add_argument("--max-retries", type=int, default=20)
        command.add_argument("--timeout", type=float, default=180.0)
        command.add_argument("--max-context-chars", type=int, default=600_000)
        command.add_argument("--max-output-tokens", type=int, default=4096)
        command.add_argument("--requests-per-minute", type=int, default=90)
        command.add_argument("--tokens-per-minute", type=int, default=4_500_000)
        command.add_argument(
            "--rate-limit-state",
            type=Path,
            default=os.environ.get("CLASSIFIER_RATE_LIMIT_STATE") or None,
            help="One shared state file for all jobs using the same model quota",
        )
        command.add_argument("--workspace", type=Path, default=Path(gettempdir()))
        command.add_argument(
            "--state", type=Path, help="Successful-label SQLite cache path"
        )
        if name == "s3":
            command.add_argument(
                "--endpoint-url", required=True, help="S3-compatible endpoint"
            )
            command.add_argument("--region", default="us-east-1")
            command.add_argument(
                "--credentials",
                type=Path,
                help="Owner-only JSON file, or use the AWS credential chain",
            )
    commands.add_parser(
        "taxonomy", help="Print the installed scenario, capability, and subject names"
    )
    args = parser.parse_args(argv)
    try:
        from .client import ClassificationAPIError
        from .jobs import LabelJobError, run_local_label_job, run_s3_label_job
        from .taxonomy import ScenarioTaxonomy
    except ImportError:
        parser.error(
            "Install classification dependencies: pip install '.[classification]'"
        )
    if args.command == "taxonomy":
        taxonomy = ScenarioTaxonomy.load()
        print(
            json.dumps(
                {
                    "scenario_labels": taxonomy.scenario_names,
                    "capability_labels": taxonomy.capability_names,
                    "subject_labels": taxonomy.subject_names,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not args.api_url:
        parser.error(
            "Set --api-url or CLASSIFIER_API_URL to a full /chat/completions URL"
        )
    key = os.environ.get("CLASSIFIER_API_KEY", "").strip()
    if not key and args.api_key_file is not None:
        try:
            key = args.api_key_file.expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            parser.error("The classifier API key file could not be read")
    if not key or any(character.isspace() for character in key):
        parser.error("Set a valid CLASSIFIER_API_KEY or --api-key-file")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    options = {
        "classifier_api_url": args.api_url,
        "classifier_model": args.model,
        "classifier_api_key": key,
        "workspace_parent": args.workspace,
        "state_path": args.state,
        "max_workers": args.workers,
        "max_retries": args.max_retries,
        "request_timeout_seconds": args.timeout,
        "max_context_chars": args.max_context_chars,
        "max_output_tokens": args.max_output_tokens,
        "requests_per_minute": args.requests_per_minute,
        "tokens_per_minute": args.tokens_per_minute,
        "rate_limit_state_path": args.rate_limit_state,
    }
    if args.taxonomy is not None:
        options["taxonomy_path"] = args.taxonomy
    try:
        if args.command == "s3":
            result = run_s3_label_job(
                args.input,
                args.output,
                args.endpoint_url,
                region_name=args.region,
                credentials_path=args.credentials,
                **options,
            )
        else:
            result = run_local_label_job(args.input, args.output, **options)
    except ImportError:
        parser.exit(1, "Install S3 dependencies: pip install '.[classification,s3]'\n")
    except Exception as error:  # noqa: BLE001 - do not expose raw SDK errors in CLI output
        # Configuration errors and key-file failures have safe messages. Storage
        # SDK exceptions may contain endpoint details, so only expose their type.
        if isinstance(error, (ClassificationAPIError, LabelJobError, ValueError)):
            parser.exit(1, f"classification failed: {error}\n")
        parser.exit(1, f"classification failed: {type(error).__name__}\n")
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
