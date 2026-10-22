"""
cli.py — `tally_agent` command-line entry point (click). Commands:

  enroll              one-shot: exchange an enrollment token for
                       credentials, write config.yaml + credentials.dat
  rotate-credential   store a NEW credential_id/credential_secret pair
                       that an admin already obtained via
                       POST /tally-agents/{id}/rotate-credential and
                       relayed out of band — see docstring on that
                       command below for why this agent-side command
                       never calls that server route itself
  status              show config + stored credentials (secret masked)
  diagnostics         test BOTH legs independently: this machine's
                       local TallyPrime, and the server's WS endpoint
  run                 run in the foreground (Ctrl+C to stop) — the
                       command windows_service.py's SvcDoRun also calls
  service install|start|stop|remove|status   Windows Service control
                       (real, but unverified on this dev environment —
                       see windows_service.py's own docstring)
  uninstall           remove the service (if installed) + delete all
                       local config/credentials
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import click
import httpx

from . import credential_store
from .config import AgentConfig, ConfigError, default_config_dir, default_config_path, load_config, save_config
from .credential_store import AgentCredentials, CredentialStoreError
from .dispatcher import Dispatcher
from .logging_setup import configure_logging
from .tally_client import LocalTallyClient
from .ws_client import AgentWsClient, AuthRejectedError

_CONFIG_PATH_OPTION = click.option(
    "--config-path", type=click.Path(path_type=Path), default=None,
    help="Override the config file location (default: see config.default_config_path()).",
)


@click.group()
def cli() -> None:
    """Tally Agent — relays TallyPrime data to the DMS central server."""


@cli.command()
@click.option("--server-url", required=True, help="The DMS server's base URL, e.g. https://dip.example.com")
@click.option("--token", required=True, help="The enrollment token issued by an admin (POST /tally-agents/{id}/enrollment-tokens).")
@click.option("--tally-host", default="127.0.0.1", show_default=True)
@click.option("--tally-port", default=9000, show_default=True, type=int)
@_CONFIG_PATH_OPTION
def enroll(server_url: str, token: str, tally_host: str, tally_port: int, config_path: Path | None) -> None:
    """Exchange a one-time enrollment token for real credentials."""
    url = server_url.rstrip("/") + "/api/tally-agents/enroll"
    try:
        resp = httpx.post(url, json={"token": token, "agent_version": "0.1.0", "hostname": None, "os_version": None}, timeout=30)
    except httpx.RequestError as exc:
        raise click.ClickException(f"Could not reach {url}: {exc}")
    if resp.status_code != 200:
        raise click.ClickException(f"Enrollment failed (HTTP {resp.status_code}): {resp.text}")
    body = resp.json()

    config = AgentConfig(server_url=server_url, tally_host=tally_host, tally_port=tally_port)
    saved_config_path = save_config(config, config_path)
    config_dir = (config_path or default_config_path()).parent
    creds = AgentCredentials(
        installation_id=body["installation_id"], credential_id=body["credential_id"], credential_secret=body["credential_secret"],
    )
    credential_store.save_credentials(creds, config_dir)

    click.echo(f"Enrolled as installation_id={creds.installation_id}.")
    click.echo(f"Config written to {saved_config_path}")
    click.echo(f"Credentials stored in {config_dir / 'credentials.dat'}")


@cli.command("rotate-credential")
@click.option("--credential-id", required=True, help="The NEW credential_id an admin obtained via the admin rotate-credential API.")
@click.option("--credential-secret", required=True, help="The NEW credential_secret — shown only once by the admin API; paste it exactly.")
@_CONFIG_PATH_OPTION
def rotate_credential_cmd(credential_id: str, credential_secret: str, config_path: Path | None) -> None:
    """Store a credential an admin already rotated server-side.

    Deliberately does NOT call POST /tally-agents/{id}/rotate-credential
    itself — that route requires an admin JWT this agent process never
    holds (see api/tally_agents.py's RBAC gate), and rotating also force-
    disconnects whichever session is live, which only makes sense as an
    action the admin consciously takes. The realistic flow: an admin
    rotates via the admin UI/API, it returns the new credential pair
    exactly once, the admin relays it to whoever operates this machine,
    and THEY run this command to store it.
    """
    config_dir = (config_path or default_config_path()).parent
    try:
        existing = credential_store.load_credentials(config_dir)
        installation_id = existing.installation_id
    except CredentialStoreError:
        raise click.ClickException("No existing enrollment found — run 'tally_agent enroll' first, not rotate-credential.")
    creds = AgentCredentials(installation_id=installation_id, credential_id=credential_id, credential_secret=credential_secret)
    credential_store.save_credentials(creds, config_dir)
    click.echo(f"Stored rotated credential for installation_id={installation_id}.")


@cli.command()
@_CONFIG_PATH_OPTION
def status(config_path: Path | None) -> None:
    """Show the current config and stored credentials (secret masked)."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    config_dir = (config_path or default_config_path()).parent
    click.echo(f"server_url: {config.server_url}")
    click.echo(f"tally_host: {config.tally_host}:{config.tally_port}")
    click.echo(f"heartbeat_interval_seconds: {config.heartbeat_interval_seconds}")
    try:
        creds = credential_store.load_credentials(config_dir)
        masked = creds.credential_secret[:4] + "…" if len(creds.credential_secret) > 4 else "…"
        click.echo(f"installation_id: {creds.installation_id}")
        click.echo(f"credential_id: {creds.credential_id}")
        click.echo(f"credential_secret: {masked} (masked)")
    except CredentialStoreError as exc:
        click.echo(f"credentials: NOT FOUND ({exc})")


@cli.command()
@_CONFIG_PATH_OPTION
def diagnostics(config_path: Path | None) -> None:
    """Test both legs independently: local TallyPrime, and the server."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Testing local TallyPrime at {config.tally_host}:{config.tally_port} ...")
    local_client = LocalTallyClient(host=config.tally_host, port=config.tally_port)
    result = local_client.test_connection()
    click.echo(f"  -> {'OK' if result['success'] else 'FAILED'}: {result['message']}")

    config_dir = (config_path or default_config_path()).parent
    try:
        creds = credential_store.load_credentials(config_dir)
    except CredentialStoreError as exc:
        click.echo(f"Skipping server test — no stored credentials: {exc}")
        return

    click.echo(f"Testing server WebSocket at {config.ws_url()} ...")
    ok, message = asyncio.run(_test_server_handshake(config, creds))
    click.echo(f"  -> {'OK' if ok else 'FAILED'}: {message}")


async def _test_server_handshake(config: AgentConfig, creds: AgentCredentials) -> tuple[bool, str]:
    import platform

    import websockets

    from . import protocol
    try:
        async with websockets.connect(config.ws_url(), open_timeout=10) as ws:
            await ws.send(protocol.build_auth(
                credential_id=creds.credential_id, credential_secret=creds.credential_secret,
                agent_version="0.1.0", hostname=platform.node(), os_version=f"{platform.system()} {platform.release()}",
            ))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            ack = protocol.parse_message(raw)
            if protocol.is_auth_ack(ack):
                return True, f"Authenticated (installation_id={ack.get('installation_id')})"
            if protocol.is_error(ack):
                return False, ack.get("message") or "Authentication rejected."
            return False, f"Unexpected response: {ack}"
    except Exception as exc:  # noqa: BLE001 - diagnostics must report, not crash, on any connection failure
        return False, str(exc)


def build_dispatcher(config: AgentConfig) -> Dispatcher:
    client = LocalTallyClient(host=config.tally_host, port=config.tally_port)
    return Dispatcher(client, max_concurrent=config.max_concurrent_commands)


async def _run_async(config: AgentConfig, creds: AgentCredentials, *, register_stopper=None) -> None:
    dispatcher = build_dispatcher(config)
    client = AgentWsClient(config, creds, dispatcher)
    if register_stopper is not None:
        register_stopper(client.stop)

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, client.stop)
            except NotImplementedError:
                pass  # some event loop/platform combinations don't support signal handlers; Ctrl+C still raises KeyboardInterrupt
    try:
        await client.run_forever()
    except AuthRejectedError as exc:
        raise click.ClickException(f"Authentication rejected — credential may be rotated/revoked: {exc}")


@cli.command()
@_CONFIG_PATH_OPTION
def run(config_path: Path | None) -> None:
    """Run the agent in the foreground (Ctrl+C to stop)."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    config_dir = (config_path or default_config_path()).parent
    try:
        creds = credential_store.load_credentials(config_dir)
    except CredentialStoreError as exc:
        raise click.ClickException(str(exc))

    configure_logging(log_file=config.log_file, log_level=config.log_level)
    click.echo(f"Starting Tally Agent (installation_id={creds.installation_id}) — Ctrl+C to stop.")
    try:
        asyncio.run(_run_async(config, creds))
    except KeyboardInterrupt:
        click.echo("Stopped.")


def run_agent_blocking(*, stop_event_setter=None):  # pragma: no cover - only exercised via windows_service.py on Windows
    """Entry point windows_service.py's SvcDoRun calls — runs the agent
    until SvcStop() invokes the stopper this registers back. Returns
    the stopper callable immediately isn't possible since this blocks;
    instead the stopper is registered via `stop_event_setter` BEFORE the
    blocking run starts, matching asyncio's own single-threaded model."""
    config = load_config()
    config_dir = default_config_dir()
    creds = credential_store.load_credentials(config_dir)
    configure_logging(log_file=config.log_file, log_level=config.log_level, console=False)
    asyncio.run(_run_async(config, creds, register_stopper=stop_event_setter))


@cli.group()
def service() -> None:
    """Windows Service control (install/start/stop/remove/status).

    Real code in windows_service.py, but NOT runnable or live-verified
    on this project's own dev machines — every subcommand here raises a
    clear error on non-Windows rather than silently no-opping.
    """


@service.command("install")
def service_install() -> None:
    from . import windows_service
    windows_service.install_service()
    click.echo("Service installed.")


@service.command("start")
def service_start() -> None:
    from . import windows_service
    windows_service.start_service()
    click.echo("Service started.")


@service.command("stop")
def service_stop() -> None:
    from . import windows_service
    windows_service.stop_service()
    click.echo("Service stopped.")


@service.command("remove")
def service_remove() -> None:
    from . import windows_service
    windows_service.remove_service()
    click.echo("Service removed.")


@service.command("status")
def service_status_cmd() -> None:
    from . import windows_service
    click.echo(windows_service.service_status())


@cli.command()
@_CONFIG_PATH_OPTION
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def uninstall(config_path: Path | None, yes: bool) -> None:
    """Remove the Windows Service (if installed) and delete all local config/credentials."""
    if not yes and not click.confirm("This will remove the service and delete all local agent config/credentials. Continue?"):
        return
    if sys.platform == "win32":
        from . import windows_service
        try:
            windows_service.stop_service()
        except Exception:  # noqa: BLE001 - best-effort; may already be stopped/not installed
            pass
        try:
            windows_service.remove_service()
        except Exception:  # noqa: BLE001
            pass
    config_dir = (config_path or default_config_path()).parent
    credential_store.delete_credentials(config_dir)
    path = config_path or default_config_path()
    if path.exists():
        path.unlink()
    click.echo("Uninstalled.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
