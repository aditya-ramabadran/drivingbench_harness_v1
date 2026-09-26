"""Install clients, configure connectivity, serve the shared laptop UI, sync traces."""

import argparse
import json
import sys
from pathlib import Path

import httpx
from drivingbench.gateway import artifacts, traces
from drivingbench.gateway.install import (
    install_clients,
    install_gateway,
    install_runtime,
    installation_status,
)
from drivingbench.gateway.setup import Config, bring_online, config_path, mirror_recordings


def sync(config: Config, root: Path, mirror: Path) -> dict:
    """The one post-drive command: mirror, publish, attach chats, upload labeled sessions."""
    mirror = mirror_recordings(config, mirror)
    report = {"mirror": str(mirror), "segments": traces.publish_segments(mirror, root)}
    report["sessions"] = traces.publish_sessions(mirror, root)
    known = traces.recorded_image_hashes(mirror)
    report["chats"] = {
        segment: [
            {**traces.attach_chat(root, segment, path, client, known), "matched": matches}
            for client, path, matches in traces.find_chats(
                root / "segments" / segment, traces.transcript_roots(config)
            )
        ]
        for segment in traces.segments_without_chat(root)
    }
    # Stage every pending session first (the only comma-dependent step), then upload; the comma
    # can leave the network once staging is done.
    staging = mirror.parent / "artifacts"
    todo = artifacts.pending(root, report["sessions"]["published"]) if config.dataset else []
    for session in todo:
        artifacts.stage(config, root, mirror, session, staging)
    report["staged"] = [
        session["id"] for session in todo
    ]  # `drivingbench upload` pushes them, comma-free
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="drivingbench")
    commands = parser.add_subparsers(dest="operation", required=True)
    install = commands.add_parser("install", help="Install selected MCP clients and laptop gateway")
    install.add_argument(
        "--client", action="append", choices=["codex", "claude", "cursor"], required=True
    )
    install.add_argument("--host", help="Comma hostname or existing SSH config alias")
    install.add_argument("--user", default="comma")
    install.add_argument("--key", type=Path)
    install.add_argument(
        "--dataset", help="Hugging Face dataset repo for labeled-session artifacts"
    )
    install.add_argument("--config", type=Path, default=config_path())
    install.add_argument("--codex-config", type=Path)
    install.add_argument("--claude-config", type=Path)
    install.add_argument("--cursor-config", type=Path)
    install.add_argument(
        "--no-service", action="store_true", help="Run serve yourself instead of launchd"
    )
    for operation in ("serve", "online", "status"):
        command = commands.add_parser(operation)
        command.add_argument("--config", type=Path, default=config_path())

    def traces_args(command):
        command.add_argument("--config", type=Path, default=config_path())
        command.add_argument("--traces", type=Path, default=Path("traces"))
        command.add_argument(
            "--mirror", type=Path, default=Path("runs/recordings"), help="Ignored full mirror"
        )

    traces_args(
        commands.add_parser(
            "sync", help="Mirror the comma, publish segments and sessions, attach chats, upload"
        )
    )
    traces_args(
        commands.add_parser(
            "upload",
            help="Upload staged labeled sessions to the dataset; needs no comma (after sync staged them)",
        )
    )
    fetch = commands.add_parser("fetch", help="Download a labeled session's bulk artifacts")
    traces_args(fetch)
    fetch.add_argument("session")
    session = commands.add_parser("session", help="Start, end, or label a benchmark session")
    action = session.add_subparsers(dest="action", required=True)
    start = action.add_parser("start")
    start.add_argument("--model", required=True)
    start.add_argument("--harness", required=True, choices=["codex", "claude", "cursor", "other"])
    start.add_argument("--notes", default="")
    start.add_argument("--config", type=Path, default=config_path())
    end = action.add_parser("end")
    end.add_argument("--outcome", required=True, choices=["completed", "collision", "aborted"])
    end.add_argument("--note", default="")
    end.add_argument("--config", type=Path, default=config_path())
    label = action.add_parser("label", help="Label already published segments after the fact")
    label.add_argument("segments", nargs="+")
    label.add_argument("--model", required=True)
    label.add_argument("--harness", required=True, choices=["codex", "claude", "cursor", "other"])
    label.add_argument("--notes", default="")
    label.add_argument("--outcome", required=True, choices=["completed", "collision", "aborted"])
    label.add_argument("--note", default="")
    label.add_argument("--traces", type=Path, default=Path("traces"))
    chat = commands.add_parser(
        "attach-chat",
        help="File the driving chat's transcript under traces/segments/<segment>/chat/",
    )
    chat.add_argument("segment", help="Published segment id, e.g. 2026-09-16-3f9a1c")
    chat.add_argument("transcript", type=Path, nargs="?", help="The chat app's transcript file")
    chat.add_argument("--client", choices=["codex", "claude", "cursor"])
    chat.add_argument("--find", action="store_true", help="Attach every transcript that drove it")
    chat.add_argument("--traces", type=Path, default=Path("traces"))
    chat.add_argument("--mirror", type=Path, default=Path("runs/recordings"))
    chat.add_argument("--config", type=Path, default=config_path())
    deploy = commands.add_parser("bundle", help="Build a versioned device installation bundle")
    deploy.add_argument("--source", type=Path, default=Path.cwd(), help="Clean source checkout")
    deploy.add_argument("--output", type=Path, required=True)
    deploy.add_argument(
        "--settings-file",
        type=Path,
        required=True,
        help="Reviewed shared settings including retained longitudinal coefficients",
    )
    args = parser.parse_args(argv)

    if args.operation == "attach-chat":
        if args.find:
            roots = traces.transcript_roots(Config.load(args.config))
            pairs = traces.find_chats(args.traces / "segments" / args.segment, roots)
        elif args.transcript and args.client:
            pairs = [(args.client, args.transcript, None)]
        else:
            parser.error("give TRANSCRIPT with --client, or --find")
        known = traces.recorded_image_hashes(args.mirror)
        print(
            json.dumps(
                [
                    {**traces.attach_chat(args.traces, args.segment, p, c, known), "matched": m}
                    for c, p, m in pairs
                ]
            )
        )
        return
    if args.operation == "session" and args.action == "label":
        print(
            json.dumps(
                traces.label_session(
                    args.traces,
                    args.segments,
                    args.model,
                    args.harness,
                    args.notes,
                    args.outcome,
                    args.note,
                ),
                indent=2,
            )
        )
        return
    if args.operation == "bundle":
        from drivingbench.device.deploy import build_bundle

        print(build_bundle(args.source.resolve(), args.output, args.settings_file))
        return
    config = Config.load(args.config)
    if args.operation == "install":
        from dataclasses import replace

        changes = {}
        if args.host:
            changes.update(ssh_host=args.host, ssh_user=args.user)
        if args.key:
            changes["ssh_key"] = str(args.key.expanduser().resolve())
        if args.dataset:
            changes["dataset"] = args.dataset
        client_configs = dict(config.client_configs)
        for client in config.client_paths:
            if path := getattr(args, f"{client}_config"):
                client_configs[client] = str(path.expanduser().resolve())
        repo = Path(__file__).resolve().parents[1]
        config = replace(config, client_configs=client_configs, repo=str(repo), **changes)
        executables = install_runtime(repo)
        result = install_clients(
            args.client,
            executables / "drivingbench-sandbox",
            f"http://127.0.0.1:{config.web_port}",
            paths=config.client_paths,
        )
        config.save(args.config)
        service = (
            None if args.no_service else install_gateway(executables / "drivingbench", args.config)
        )
        print(
            json.dumps(
                {
                    "clients": result,
                    "gateway": service,
                    "next": "Restart the selected AI apps, then open the UI and Bring online.",
                },
                indent=2,
            )
        )
    elif args.operation == "serve":
        import uvicorn
        from drivingbench.gateway.app import create_app

        app = create_app(
            config.producer_url,
            online=lambda: bring_online(config),
            install_status=lambda: installation_status(paths=config.client_paths),
            repo=Path(config.repo) if config.repo else None,
        )
        uvicorn.run(app, host="127.0.0.1", port=config.web_port)
    elif args.operation == "online":
        print(json.dumps(bring_online(config), indent=2))
    elif args.operation == "sync":
        print(json.dumps(sync(config, args.traces, args.mirror), indent=2))
    elif args.operation == "upload":
        print(
            json.dumps(
                artifacts.upload(config, args.traces, args.mirror.parent / "artifacts"),
                indent=2,
            )
        )
    elif args.operation == "fetch":
        print(
            json.dumps(
                artifacts.fetch(config, args.session, args.traces, args.mirror.parent / "artifacts")
            )
        )
    elif args.operation == "session":
        body = (
            {"model": args.model, "harness": args.harness, "notes": args.notes}
            if args.action == "start"
            else {"outcome": args.outcome, "note": args.note}
        )
        response = httpx.post(
            f"http://127.0.0.1:{config.web_port}/api/session/{args.action}", json=body, timeout=10
        )
        print(json.dumps(response.json(), indent=2))
        sys.exit(0 if response.is_success else 1)
    else:
        print(json.dumps(installation_status(paths=config.client_paths), indent=2))


if __name__ == "__main__":
    main()
