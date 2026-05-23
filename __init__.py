"""
OpenCode Go Usage Monitor - Hermes Agent Plugin

Scrapes https://opencode.ai/workspace/wrk_{id}/go for usage stats
from the embedded script data and DOM elements.

Usage:
  - Tool: check_opencode_usage (called by the agent)
  - CLI: hermes opencode-usage setup | status | check
"""

import json
import os
import re
import subprocess
import time
import argparse
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
CONFIG_PATH = PLUGIN_DIR / "config.json"


# ── Config helpers ──────────────────────────────────────────────────────

def _load_config() -> dict:
    defaults = {
        "workspace_id": "",
        "auth_cookie": "",
        "check_interval_hours": 6,
        "alert_thresholds": {"warning": 70, "critical": 90},
        "cron_enabled": False,
        "last_check": None,
        "last_status": None,
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            defaults.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── HTTP fetch ─────────────────────────────────────────────────────────

def _fetch_page(workspace_id: str, auth_cookie: str) -> dict:
    """Fetch the Go usage page with cookie auth. Captures response headers
    to detect Set-Cookie refresh. Returns dict with 'success' and either
    'html' (+ optional 'new_cookie') or 'error'."""
    import tempfile
    url = f"https://opencode.ai/workspace/wrk_{workspace_id}/go"

    try:
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.headers', delete=False) as hf, \
             tempfile.NamedTemporaryFile(mode='w+', suffix='.body', delete=False) as bf:
            hdr_path = hf.name
            body_path = bf.name

        result = subprocess.run(
            ["curl", "-s", "-L",
             "-D", hdr_path,
             "-o", body_path,
             "-w", "%{http_code}:%{url_effective}",
             "-H", f"Cookie: auth={auth_cookie}", url],
            capture_output=True, text=True, timeout=30,
        )
        meta = result.stdout.strip()
        if not meta:
            _cleanup_files([hdr_path, body_path])
            return {"success": False, "error": "Empty response from curl"}

        http_code, effective_url = meta.split(":", 1)

        # Read body
        try:
            with open(body_path, 'r') as f:
                body = f.read()
        except Exception:
            body = ""

        # Read headers and look for Set-Cookie
        new_cookie = None
        try:
            with open(hdr_path, 'r') as f:
                headers_text = f.read()
            for line in headers_text.splitlines():
                # Look for "set-cookie: auth=..." (case-insensitive)
                stripped = line.strip()
                if stripped.lower().startswith("set-cookie:"):
                    # Parse: set-cookie: auth=VALUE; path=/; ...
                    match = re.search(r';\s*auth=([^;]+)', stripped, re.IGNORECASE)
                    if not match:
                        match = re.search(r'^set-cookie:\s*auth=([^;]+)', stripped, re.IGNORECASE)
                    if match:
                        new_cookie = match.group(1)
        except Exception:
            pass

        _cleanup_files([hdr_path, body_path])

        # Detect redirect to login / auth failure
        if "login" in effective_url.lower() or http_code in ("301", "302", "401", "403"):
            return {
                "success": False,
                "error": f"Cookie expired or invalid (HTTP {http_code}, redirect to {effective_url})",
                "auth_failure": True,
            }

        if http_code != "200":
            return {"success": False, "error": f"HTTP {http_code}"}

        # Also check body for auth-related redirect content
        if "authorize?client_id" in body or "login" in body.lower()[:500]:
            return {
                "success": False,
                "error": "Response contains login page, cookie likely expired",
                "auth_failure": True,
            }

        result = {"success": True, "html": body}
        if new_cookie:
            result["new_cookie"] = new_cookie
        return result

    except subprocess.TimeoutExpired:
        _cleanup_files([hdr_path, body_path])
        return {"success": False, "error": "Request timed out after 30s"}
    except FileNotFoundError:
        return {"success": False, "error": "curl not found on system"}


def _cleanup_files(paths: list):
    """Best-effort cleanup of temp files."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# ── HTML parsing ───────────────────────────────────────────────────────

def _extract_script_data(html: str) -> dict:
    """Extract JSON embedded in <script> tags containing usage data."""
    data = {}
    # Match any <script>...</script> block, look for JSON-like content
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for script in scripts:
        # Look for key patterns: rollingUsage, weeklyUsage, monthlyUsage, period
        for key in ("rollingUsage", "weeklyUsage", "monthlyUsage", "period"):
            if key in script:
                # Try to extract the value after the key
                # Pattern: "key": { ... } or "key": "value"
                m = re.search(
                    rf'"{key}"\s*:\s*(\{{[^}}]+}}|"[^"]*"|null|true|false|\d+)',
                    script,
                )
                if m:
                    raw = m.group(1)
                    try:
                        data[key] = json.loads(raw)
                    except json.JSONDecodeError:
                        data[key] = raw
    return data


def _extract_dom_data(html: str) -> dict:
    """Extract usage data from DOM-like data-slot attributes using regex."""
    result = {}

    # Helper: extract text content including comment-template markers
    def _inner_text(tag_html: str) -> str:
        """Strip comment markers like <!--$--> and <!--/--> from inner content."""
        clean = re.sub(r'<!--/?\$?-->', '', tag_html)
        return clean.strip()

    # Usage labels: <tag data-slot="usage-label">text</tag>
    labels = re.findall(r'data-slot="usage-label"[^>]*>\s*([^<]+?)\s*<', html)
    result["labels"] = labels if labels else []

    # Usage values: <tag data-slot="usage-value">...</tag> -> may contain <!--$-->N<!--/-->%
    value_blocks = re.findall(
        r'data-slot="usage-value"[^>]*>(.*?)</[a-zA-Z]+>', html, re.DOTALL
    )
    values = []
    for block in value_blocks:
        clean = _inner_text(block)
        if clean:
            values.append(clean)
        else:
            # Try to extract just the number
            nums = re.findall(r'(\d+)', block)
            if nums:
                values.append(f"{nums[0]}%")
    result["values"] = values

    # Progress bar widths: <tag data-slot="progress-bar" style="width:48%">
    widths = re.findall(r'data-slot="progress-bar"[^>]*style="[^"]*width:\s*(\d+%)', html)
    result["progress_widths"] = widths if widths else []

    # Reset times: <tag data-slot="reset-time">...</tag> -> may contain <!--$-->text<!--/-->
    reset_blocks = re.findall(
        r'data-slot="reset-time"[^>]*>(.*?)</[a-zA-Z]+>', html, re.DOTALL
    )
    resets = []
    for block in reset_blocks:
        clean = _inner_text(block)
        if clean:
            resets.append(clean)
    result["reset_times"] = resets

    # Map into named buckets (rolling, weekly, monthly)
    bucket_labels = ["rolling", "weekly", "monthly"]
    for i, label in enumerate(labels):
        if i < len(bucket_labels):
            result[f"{bucket_labels[i]}_label"] = label.strip()
        if i < len(values):
            result[f"{bucket_labels[i]}_value_pct"] = values[i].strip()
        if i < len(widths):
            result[f"{bucket_labels[i]}_width"] = widths[i].strip()
        if i < len(resets):
            result[f"{bucket_labels[i]}_reset"] = resets[i].strip()

    return result


def _parse_pct(text: str) -> float:
    """Parse '48%' -> 48.0, or 0 on failure."""
    m = re.match(r"(\d+(?:\.\d+)?)", text.strip())
    return float(m.group(1)) if m else 0.0


# ── Threshold check ────────────────────────────────────────────────────

def _check_thresholds(values: list[str], thresholds: dict) -> list[dict]:
    """Compare usage percentages to thresholds and return alerts."""
    alerts = []
    labels = ["rolling", "weekly", "monthly"]
    for i, val in enumerate(values):
        if i >= len(labels):
            break
        pct = _parse_pct(val)
        bucket = labels[i]
        status = "ok"
        if pct >= thresholds.get("critical", 90):
            status = "critical"
        elif pct >= thresholds.get("warning", 70):
            status = "warning"

        alerts.append({
            "bucket": bucket,
            "usage_pct": pct,
            "status": status,
        })
    return alerts


# ── Formatting helpers ──────────────────────────────────────────────────

def _status_emoji(status: str) -> str:
    if status == "ok":
        return "✅"
    elif status == "warning":
        return "⚠️"
    elif status == "critical":
        return "🔴"
    return "❓"


def _fmt_bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    empty = width - filled
    return "█" * filled + "░" * empty


def _format_check_response(data: dict) -> str:
    """Format usage data into a Telegram-friendly string."""
    dom = data.get("dom", {})
    alerts = data.get("alerts", [])
    overall = data.get("overall_status", "unknown")

    lines = []
    lines.append("📊 **OpenCode Go Usage**")
    lines.append("")

    for alert in alerts:
        bucket = alert["bucket"]
        pct = alert["usage_pct"]
        status = alert["status"]
        emoji = _status_emoji(status)
        label = dom.get(f"{bucket}_label", bucket)
        reset = dom.get(f"{bucket}_reset", "")
        bar = _fmt_bar(pct)
        lines.append(f"{emoji} **{label}:** {pct:.0f}%")
        lines.append(f"   `{bar}`")
        if reset:
            lines.append(f"   _{reset}_")

    lines.append("")
    overall_emoji = _status_emoji(overall)
    lines.append(f"{overall_emoji} **Overall:** {overall.upper()}")

    if data.get("cookie_refreshed"):
        lines.append("")
        lines.append("🔄 Auth cookie was auto-refreshed by the server.")

    return "\n".join(lines)

def _handle_check(params: dict, **kwargs) -> str:
    """Handler for check_opencode_usage tool."""
    config = _load_config()

    workspace_id = params.get("workspace_id") or config.get("workspace_id", "")
    auth_cookie = params.get("auth_cookie") or config.get("auth_cookie", "")

    # Auto-save if both provided and not configured yet
    if params.get("workspace_id") and params.get("auth_cookie"):
        if not config.get("workspace_id") or not config.get("auth_cookie"):
            config["workspace_id"] = workspace_id
            config["auth_cookie"] = auth_cookie
            _save_config(config)

    if not workspace_id:
        return "❌ **No workspace configured.** Call `configure_opencode_usage` to set it up, or pass `workspace_id` and `auth_cookie` directly."

    if not auth_cookie:
        return "❌ **No auth cookie configured.** Call `configure_opencode_usage` to set it up, or pass `auth_cookie` directly."

    fetch = _fetch_page(workspace_id, auth_cookie)
    if not fetch["success"]:
        if fetch.get("auth_failure"):
            # If cookie expired, clear it so user knows to refresh
            if config.get("auth_cookie") == auth_cookie:
                config["auth_cookie"] = ""
                _save_config(config)
            return "🔴 **Auth cookie expired or invalid.** The cookie has been cleared. Please refresh it with `configure_opencode_usage`."
        return f"❌ **Error fetching usage:** {fetch.get('error', 'Unknown error')}"

    html = fetch["html"]

    # Auto-refresh cookie if server sent a new one via Set-Cookie
    cookie_refreshed = False
    if fetch.get("new_cookie") and fetch["new_cookie"] != auth_cookie:
        config["auth_cookie"] = fetch["new_cookie"]
        _save_config(config)
        cookie_refreshed = True

    script_data = _extract_script_data(html)
    dom_data = _extract_dom_data(html)

    # Build response
    thresholds = config.get("alert_thresholds", {"warning": 70, "critical": 90})
    alerts = _check_thresholds(dom_data.get("values", []), thresholds)

    response = {
        "success": True,
        "workspace_id": workspace_id,
        "dom": dom_data,
        "embedded": script_data,
        "alerts": alerts,
        "cookie_refreshed": cookie_refreshed,
        "config": {
            "check_interval_hours": config.get("check_interval_hours", 6),
            "thresholds": thresholds,
            "cron_enabled": config.get("cron_enabled", False),
        },
    }

    # Determine overall status
    statuses = {a["status"] for a in alerts}
    if "critical" in statuses:
        response["overall_status"] = "critical"
    elif "warning" in statuses:
        response["overall_status"] = "warning"
    else:
        response["overall_status"] = "ok"

    # Update last check in config
    now = int(time.time())
    config["last_check"] = now
    config["last_status"] = response["overall_status"]
    _save_config(config)

    return _format_check_response(response)


# ── CLI commands ────────────────────────────────────────────────────────


def _register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the argparse tree for ``hermes opencode-usage``."""
    subs = subparser.add_subparsers(dest="opencode_usage_command")

    p_setup = subs.add_parser("setup", help="Interactive configuration wizard")
    p_setup.add_argument(
        "--workspace-id", "-w", help="OpenCode workspace ID (e.g. abc12345)"
    )
    p_setup.add_argument(
        "--auth-cookie", "-c", help="Auth cookie value from browser DevTools"
    )
    p_setup.add_argument(
        "--warning", type=int, default=None, help="Warning threshold % (default: 70)"
    )
    p_setup.add_argument(
        "--critical", type=int, default=None, help="Critical threshold % (default: 90)"
    )
    p_setup.add_argument(
        "--interval", type=int, default=None, help="Check interval in hours (default: 6)"
    )

    subs.add_parser("status", help="Show current configuration and last check")

    p_check = subs.add_parser("check", help="Run a usage check immediately")
    p_check.add_argument(
        "--workspace-id", "-w", help="Override workspace ID"
    )
    p_check.add_argument(
        "--auth-cookie", "-c", help="Override auth cookie"
    )

    subparser.set_defaults(func=_cli_dispatch)


def _cli_dispatch(args: argparse.Namespace) -> int:
    """Dispatch CLI subcommands."""
    sub = getattr(args, "opencode_usage_command", None)
    if sub == "setup":
        _cli_setup(args)
    elif sub == "status":
        _cli_status()
    elif sub == "check":
        _cli_check(args)
    else:
        print("usage: hermes opencode-usage {setup,status,check}")
        return 2
    return 0


def _cli_setup(args: argparse.Namespace) -> None:
    """Interactive or argument-driven setup."""
    config = _load_config()

    # If all args provided, do non-interactive setup
    if args.workspace_id and args.auth_cookie:
        config["workspace_id"] = args.workspace_id.strip()
        config["auth_cookie"] = args.auth_cookie.strip()
        if args.warning is not None:
            config.setdefault("alert_thresholds", {})["warning"] = args.warning
        if args.critical is not None:
            config.setdefault("alert_thresholds", {})["critical"] = args.critical
        if args.interval is not None:
            config["check_interval_hours"] = args.interval
        _save_config(config)
        cron = _manage_cron_job(config)
        c = "✅" if cron.get("cron_setup") else "⚠️"
        print(f"✅ Configuration saved to ~/.hermes/plugins/opencode-usage/config.json")
        print(f"{c} Cron: {cron.get('cron_action', cron.get('reason', 'unknown'))} ({cron.get('schedule', '?')})")
        return

    # Interactive mode
    print("\n=== OpenCode Usage Monitor Setup ===\n")

    current = config.get("workspace_id", "")
    prompt = f"Workspace ID [{current}]: " if current else "Workspace ID: "
    wid = input(prompt).strip()
    if wid:
        config["workspace_id"] = wid

    current_cookie = "(set)" if config.get("auth_cookie") else "(not set)"
    print(f"\nAuth cookie [{current_cookie}]")
    print("To get it: open https://opencode.ai, login, open DevTools > Application > Cookies,")
    print("copy the 'auth' cookie value.")
    cookie = input("Auth cookie payload (leave empty to keep current): ").strip()
    if cookie:
        config["auth_cookie"] = cookie

    current_int = config.get("check_interval_hours", 6)
    interval_str = input(f"\nCheck interval hours [{current_int}]: ").strip()
    if interval_str:
        try:
            config["check_interval_hours"] = int(interval_str)
        except ValueError:
            print(f"  Invalid number, keeping {current_int}")

    current_warn = config.get("alert_thresholds", {}).get("warning", 70)
    warn_str = input(f"Warning threshold % [{current_warn}]: ").strip()
    if warn_str:
        try:
            config.setdefault("alert_thresholds", {})["warning"] = int(warn_str)
        except ValueError:
            pass

    current_crit = config.get("alert_thresholds", {}).get("critical", 90)
    crit_str = input(f"Critical threshold % [{current_crit}]: ").strip()
    if crit_str:
        try:
            config.setdefault("alert_thresholds", {})["critical"] = int(crit_str)
        except ValueError:
            pass

    _save_config(config)
    print("\n✅ Configuration saved.")

    # Auto-manage cron after interactive setup too
    cron = _manage_cron_job(config)
    c = "✅" if cron.get("cron_setup") else "⚠️"
    print(f"{c} Cron: {cron.get('cron_action', cron.get('reason', 'unknown'))} ({cron.get('schedule', '?')})")


def _cli_status() -> None:
    """Show current config and last check status."""
    config = _load_config()
    print("\n=== OpenCode Usage Monitor ===\n")
    print(f"  Workspace ID: {config.get('workspace_id', '(not set)')}")
    print(f"  Auth cookie: {'✓ set' if config.get('auth_cookie') else '✗ not set'}")
    print(f"  Check interval: {config.get('check_interval_hours', 6)}h")
    print(f"  Thresholds: warn @ {config.get('alert_thresholds', {}).get('warning', 70)}%, "
          f"critical @ {config.get('alert_thresholds', {}).get('critical', 90)}%")
    last_check = config.get("last_check")
    if last_check:
        from datetime import datetime
        dt = datetime.fromtimestamp(last_check)
        print(f"  Last check: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Last status: {config.get('last_status', 'unknown')}")
    else:
        print("  Last check: never")


def _cli_check(args: argparse.Namespace) -> None:
    """Run a usage check and display results."""
    params = {}
    if getattr(args, "workspace_id", None):
        params["workspace_id"] = args.workspace_id
    if getattr(args, "auth_cookie", None):
        params["auth_cookie"] = args.auth_cookie

    result_json = _handle_check(params, task_id=None)
    data = json.loads(result_json)
    if not data.get("success"):
        print(f"❌ Error: {data.get('error', 'unknown')}")
        return

    print("\n=== OpenCode Go Usage ===\n")
    dom = data.get("dom", {})
    alerts = data.get("alerts", [])

    for alert in alerts:
        bucket = alert["bucket"]
        pct = alert["usage_pct"]
        status = alert["status"]
        icon = {"ok": "✅", "warning": "⚠️", "critical": "🔴"}.get(status, "❓")
        label = dom.get(f"{bucket}_label", bucket)
        reset = dom.get(f"{bucket}_reset", "")
        print(f"  {icon} {label}: {pct}%")
        if reset:
            print(f"     Resets: {reset}")

    if not alerts:
        print("  (no usage data found, check credentials)")

    print(f"\n  Overall: {data.get('overall_status', 'unknown').upper()}")


# ── Tool handler: configure ─────────────────────────────────────────────

def _manage_cron_job(config: dict) -> dict:
    """Create or update the Hermes cron job for periodic usage checks.
    Returns dict with status info. Falls back gracefully on failure."""
    try:
        from cron.jobs import create_job, update_job, load_jobs
    except ImportError:
        return {"cron_setup": False, "reason": "cron module not available"}

    interval = config.get("check_interval_hours", 6)
    schedule = f"every {interval}h"
    job_id = config.get("cron_job_id")

    prompt = (
        "Check OpenCode Go plan usage by calling check_opencode_usage tool.\n\n"
        "If the tool returns success and overall_status is NOT \"ok\" "
        "(i.e. \"warning\" or \"critical\"):\n"
        "1. Extract the individual bucket statuses and usage percentages\n"
        "2. Deliver a detailed alert to the user formatted clearly for Telegram\n"
        "3. Include the reset times\n\n"
        "If overall_status is \"ok\", respond with exactly \"[SILENT]\" "
        "to suppress delivery.\n\n"
        "If the tool returns an auth error, deliver the error so the user "
        "knows to refresh the cookie."
    )

    try:
        if job_id:
            # Update existing job
            existing = load_jobs()
            found = any(j.get("id") == job_id for j in existing)
            if found:
                update_job(job_id, {
                    "schedule": schedule,
                    "prompt": prompt,
                })
                return {"cron_setup": True, "cron_action": "updated", "schedule": schedule}
        # Create new job
        job = create_job(
            prompt=prompt,
            schedule=schedule,
            name="OpenCode Usage Monitor",
            skills=["hermes-agent"],
            deliver="origin",
        )
        config["cron_job_id"] = job["id"]
        config["cron_enabled"] = True
        _save_config(config)
        return {"cron_setup": True, "cron_action": "created", "schedule": schedule, "job_id": job["id"]}
    except Exception as e:
        return {"cron_setup": False, "reason": str(e)}


def _handle_configure(params: dict, **kwargs) -> str:
    """Handler for configure_opencode_usage tool. Saves config and manages cron."""
    config = _load_config()

    workspace_id = params.get("workspace_id", "").strip()
    auth_cookie = params.get("auth_cookie", "").strip()

    if not workspace_id or not auth_cookie:
        return "❌ **Both `workspace_id` and `auth_cookie` are required.**"

    config["workspace_id"] = workspace_id
    config["auth_cookie"] = auth_cookie

    if "warning_threshold" in params:
        config.setdefault("alert_thresholds", {})["warning"] = int(params["warning_threshold"])
    if "critical_threshold" in params:
        config.setdefault("alert_thresholds", {})["critical"] = int(params["critical_threshold"])
    if "check_interval_hours" in params:
        config["check_interval_hours"] = int(params["check_interval_hours"])

    _save_config(config)

    # Auto-manage cron job
    interval = config.get("check_interval_hours", 6)
    cron_result = _manage_cron_job(config)

    return (
        "✅ **OpenCode Usage Monitor configured successfully**\n\n"
        f"Workspace: `{config['workspace_id']}`\n"
        f"Auth cookie: ✓ set (hidden)\n"
        f"Warning threshold: {config.get('alert_thresholds', {}).get('warning', 70)}%\n"
        f"Critical threshold: {config.get('alert_thresholds', {}).get('critical', 90)}%\n"
        f"Check interval: every {interval}h\n\n"
        f"Cron: {cron_result.get('cron_action', cron_result.get('reason', 'unknown'))}"
    )


# ── Registration ────────────────────────────────────────────────────────

def register(ctx):
    """Register the opencode-usage tools and CLI commands."""
    # --- Tool: configure_opencode_usage ---
    setup_schema = {
        "name": "configure_opencode_usage",
        "description": (
            "Configure or update OpenCode Go usage monitor settings. "
            "Saves workspace_id, auth_cookie, and alert thresholds permanently. "
            "Call this once before check_opencode_usage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Your OpenCode workspace ID (e.g. 'abc12345' from the URL wrk_{id})",
                },
                "auth_cookie": {
                    "type": "string",
                    "description": "Auth cookie value from opencode.ai browser DevTools > Application > Cookies > auth",
                },
                "warning_threshold": {
                    "type": "integer",
                    "description": "Warning threshold percentage (default: 70)",
                    "default": 70,
                },
                "critical_threshold": {
                    "type": "integer",
                    "description": "Critical threshold percentage (default: 90)",
                    "default": 90,
                },
                "check_interval_hours": {
                    "type": "integer",
                    "description": "How often to check usage in hours (default: 6)",
                    "default": 6,
                },
            },
            "required": ["workspace_id", "auth_cookie"],
        },
    }

    ctx.register_tool(
        name="configure_opencode_usage",
        toolset="opencode_usage",
        schema=setup_schema,
        handler=_handle_configure,
        description="Set up OpenCode Go usage monitor with workspace ID and auth cookie",
    )

    # --- Tool: check_opencode_usage ---
    check_schema = {
        "name": "check_opencode_usage",
        "description": (
            "Check OpenCode Go plan usage statistics. Returns rolling, weekly, "
            "and monthly usage percentages, reset times, and alert status. "
            "Requires workspace_id and auth_cookie configured via "
            "`hermes opencode-usage setup`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Override the configured workspace ID (e.g. 'abc12345')",
                },
                "auth_cookie": {
                    "type": "string",
                    "description": "Override the configured auth cookie (for testing)",
                },
            },
        },
    }

    ctx.register_tool(
        name="check_opencode_usage",
        toolset="opencode_usage",
        schema=check_schema,
        handler=_handle_check,
        description="Check OpenCode Go plan usage (rolling, weekly, monthly)",
    )

    ctx.register_cli_command(
        name="opencode-usage",
        help="Monitor OpenCode Go plan usage (setup, status, check)",
        setup_fn=_register_cli,
        handler_fn=_cli_dispatch,
        description="Configure and check OpenCode Go usage via CLI",
    )
