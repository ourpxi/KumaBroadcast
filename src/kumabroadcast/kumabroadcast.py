#!/usr/bin/env python3

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


DEFAULT_AVATAR_URL = "https://example.com/avatar.png"

def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    with open(dotenv_path) as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"[ERROR] Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


INCIDENT_COLORS = {
    "primary": 0x5CDC8A,
    "info": 0x15C9EF,
    "warning": 0xF8A20E,
    "danger": 0xDB3847,
    "light": 0xF8F9FA,
    "dark": 0x262A2D,
}
MAINTENANCE_COLOR = 0x1D49F5


def build_config() -> dict:
    load_dotenv(Path.cwd() / ".env")
    return {
        "status_page_url": required_env("STATUS_PAGE_URL"),
        "discord_webhook_url": required_env("DISCORD_WEBHOOK_URL"),
        "embed_link_url": os.getenv("EMBED_LINK_URL"),
        "webhook_username": os.getenv("WEBHOOK_USERNAME"),
        "webhook_avatar": os.getenv("WEBHOOK_AVATAR"),
        "state_file": Path(os.getenv("KUMA_STATE_FILE", "kuma_state.json")),
    }


def js_to_dict(js_text: str) -> dict:
    """Parse JavaScript-like object to dict with robust error handling."""
    try:
        return json.loads(js_text)
    except json.JSONDecodeError:
        pass

    # Normalize some JS-only literals
    js_text = re.sub(r'\bundefined\b', 'null', js_text)
    js_text = re.sub(r'\bNaN\b', 'null', js_text)
    js_text = re.sub(r'\bInfinity\b', 'null', js_text)

    # Convert single-quoted JS strings to JSON double-quoted strings
    def _replace_single_quoted(m: re.Match) -> str:
        inner = m.group(1)
        # Unescape common JS escapes but preserve actual escaped sequences
        inner = inner.replace('\\\'', "'").replace('\\"', '"').replace('\\\\', '\\')
        # IMPORTANT: Don't convert escaped newlines like \n to actual newlines yet
        # Keep them as escaped sequences for JSON parsing
        inner = inner.replace('\\t', '\t')
        inner = inner.replace('\\r', '\r')
        inner = inner.replace('\\b', '\b')
        inner = inner.replace('\\f', '\f')
        inner = inner.replace('\\0', '\x00')
        inner = inner.replace('"', '\\"')
        return f'"{inner}"'

    js_text = re.sub(r"'((?:\\.|[^\\'])*)'", _replace_single_quoted, js_text)

    # Quote unquoted object keys: foo: -> "foo":
    js_text = re.sub(r'(?<!["\\w])([a-zA-Z_]\w*)\s*(?=\s*:\s*)', r'"\1"', js_text)

    # Fix common JSON parsing issues
    # Remove trailing commas
    js_text = re.sub(r',\s*([}\]]\s*)', r'\1', js_text)

    # Remove truly problematic control characters (not valid JSON escape sequences)
    js_text = re.sub(r'[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]', '', js_text)

    try:
        result = json.loads(js_text)
    except json.JSONDecodeError:
        print(f"[ERROR] Failed to parse JSON after standard fixes", file=sys.stderr)
        
        # Handle specific case: literal control chars in CSS-like content
        # This is a targeted fix for Uptime Kuma data with literal newlines in strings
        if '\n' in js_text and '"customCSS"' in js_text and '"' in js_text:
            # Reconstruct JSON by escaping problematic newlines in string content
            # Simple approach: replace literal newlines with escaped newlines in strings only
            result = re.sub(r'(\"customCSS\":\"[^\"]*?)\n([^\"]*\"[^,}]*)', r'\1\\n\2', js_text)
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        
        # Final fallback
        if isinstance(result, str) or not isinstance(result, dict):
            return {
                "config": {
                    "slug": "uptime_kuma",
                    "title": "Uptime Kuma Status Page",
                    "description": "",
                    "icon": "",
                    "autoRefreshInterval": 60,
                    "theme": "auto",
                    "published": True,
                    "showTags": False,
                    "customCSS": "",
                    "footerText": "",
                    "showPoweredBy": True,
                    "analyticsId": None,
                    "analyticsScriptUrl": None,
                    "analyticsType": None,
                    "showCertificateExpiry": False,
                    "showOnlyLastHeartbeat": False,
                    "rssTitle": None
                },
                "incidents": [],
                "maintenanceList": [],
                "publicGroupList": []
            }

    def _restore_newlines(obj):
        if isinstance(obj, str):
            return obj.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t')
        elif isinstance(obj, dict):
            return {k: _restore_newlines(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_restore_newlines(item) for item in obj]
        return obj

    return _restore_newlines(result)


def fetch_preload_data(url: str) -> dict:
    """Download the status page and extract window.preloadData."""
    headers = {"User-Agent": "Mozilla/5.0 (kuma-notify/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    html = resp.text
    match = re.search(r"window\.preloadData\s*=\s*(\{.*?\})\s*;?\s*\n", html, re.DOTALL)
    if not match:
        raise ValueError("window.preloadData not found in page HTML")
    return js_to_dict(match.group(1))


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {"last_incident": None, "last_incident_post_successful": True, "last_maintenance": {}}


def save_state(state: dict, state_file: Path):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def html_to_discord_markdown(html_content: str) -> str:
    text = html_content
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.S)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.S)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.S)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.S)
    text = re.sub(r'<s>(.*?)</s>', r'~~\1~~', text, flags=re.S)
    text = re.sub(r'<del>(.*?)</del>', r'~~\1~~', text, flags=re.S)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.S)
    text = re.sub(r'<pre>(.*?)</pre>', r'```\n\1\n```', text, flags=re.S)
    text = re.sub(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.S)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<p>(.*?)</p>', r'\1\n', text, flags=re.S)
    text = re.sub(r'<li>(.*?)</li>', r'• \1\n', text, flags=re.S)
    text = re.sub(r'<[uo]l>(.*?)</[uo]l>', r'\1', text, flags=re.S)
    text = re.sub(r'<[^>]+>', '', text)
    entities = [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&nbsp;', ' ')]
    for ent, char in entities:
        text = text.replace(ent, char)
    return text.strip()


def post_to_discord(payload: dict, config: dict) -> bool:
    """Post payload to Discord webhook. Returns True if successful (200 or 204), False otherwise."""
    def _identity_fields() -> dict:
        fields = {}
        username_env = config["webhook_username"]
        avatar_env = config["webhook_avatar"]
        if username_env is None:
            fields["username"] = "KumaBroadcast"
        elif isinstance(username_env, str) and username_env.strip().lower() == "none":
            pass
        elif username_env != "":
            fields["username"] = username_env
        if avatar_env is None:
            fields["avatar_url"] = DEFAULT_AVATAR_URL
        elif isinstance(avatar_env, str) and avatar_env.strip().lower() == "none":
            pass
        elif avatar_env != "":
            fields["avatar_url"] = avatar_env
        return fields
    meta = _identity_fields()
    final_payload = {**meta, **payload}
    resp = requests.post(config["discord_webhook_url"], json=final_payload, headers={"Content-Type": "application/json"}, timeout=10)
    if resp.status_code not in (200, 204):
        print(f"[WARN] Discord returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    else:
        print(f"[OK] Discord notified (status {resp.status_code})")
        return True

def incident_embed(incident: dict, config: dict) -> dict:
    embed_url = config["embed_link_url"] or config["status_page_url"]
    style = incident.get("style", "light")
    color = INCIDENT_COLORS.get(style, INCIDENT_COLORS["light"])
    description = html_to_discord_markdown(incident.get("content", ""))
    return {
        "content": None,
        "embeds": [{
            "title": incident.get("title", "Incident"),
            "description": description,
            "url": embed_url,
            "color": color,
        }],
        "attachments": []
    }

def dt_from_iso_tz(iso_str: str, tz_name: str) -> datetime:
    naive = datetime.fromisoformat(iso_str)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    local_dt = naive.replace(tzinfo=tz)
    return local_dt.astimezone(timezone.utc)

def maintenance_embed(maintenance: dict, config: dict) -> dict:
    embed_url = config["embed_link_url"] or config["status_page_url"]
    title = maintenance.get("title", "Maintenance")
    description = html_to_discord_markdown(maintenance.get("description", ""))
    tz_name = maintenance.get("timezone") or maintenance.get("timezoneOption") or "UTC"

    timeslots = maintenance.get("timeslotList", [])
    is_manual = not timeslots

    now_utc = datetime.now(timezone.utc)

    fields = []
    author_name = "Scheduled Maintenance"

    if not is_manual and timeslots:
        slot = timeslots[0]
        start_dt = dt_from_iso_tz(slot["startDate"], tz_name)
        end_dt = dt_from_iso_tz(slot["endDate"], tz_name)

        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        if now_utc < start_dt:
            author_name = "Scheduled Maintenance"
        elif start_dt <= now_utc < end_dt:
            author_name = "Maintenance Started"
        else:
            author_name = "Maintenance Ended"

        fields = [
            {
                "name": "Start Date",
                "value": f"<t:{start_ts}:F> <t:{start_ts}:R>"
            },
            {
                "name": "End Date",
                "value": f"<t:{end_ts}:F> <t:{end_ts}:R>"
            }
        ]
    else:
        author_name = "Maintenance Started"

    affected = _affected_monitors_field(maintenance)
    if affected:
        fields.append({"name": "Affected Services", "value": affected})

    embed = {
        "title": title,
        "description": description,
        "url": embed_url,
        "color": MAINTENANCE_COLOR,
        "author": {"name": author_name},
    }
    if fields:
        embed["fields"] = fields

    return {"content": None, "embeds": [embed], "attachments": []}

def _affected_monitors_field(maintenance: dict) -> str:
    monitors = maintenance.get("monitorList") or []
    if not monitors:
        return ""
    names = [m.get("name", str(m.get("id", ""))) for m in monitors]
    return ", ".join(names)

def maintenance_phase(maintenance: dict) -> str:
    timeslots = maintenance.get("timeslotList", [])
    if not timeslots:
        return "active"

    tz_name = maintenance.get("timezone") or "UTC"
    slot = timeslots[0]
    start_dt = dt_from_iso_tz(slot["startDate"], tz_name)
    end_dt = dt_from_iso_tz(slot["endDate"], tz_name)
    now_utc = datetime.now(timezone.utc)

    if now_utc < start_dt:
        return "scheduled"
    elif now_utc < end_dt:
        return "active"
    else:
        return "ended"

def main():
    config = build_config()

    print(f"[INFO] Fetching {config['status_page_url']}")
    try:
        data = fetch_preload_data(config["status_page_url"])
    except Exception as e:
        print(f"[ERROR] Failed to fetch status page: {e}", file=sys.stderr)
        sys.exit(1)

    state = load_state(config["state_file"])
    changed = False

    current_incident = data.get("incident")

    last_incident = state.get("last_incident")
    last_incident_post_successful = state.get("last_incident_post_successful", True)

    if current_incident:
        inc_id = current_incident.get("id")

        notify_incident = False
        if last_incident is None:
            notify_incident = True
        elif last_incident.get("id") != inc_id:
            notify_incident = True
        elif last_incident.get("lastUpdatedDate") != current_incident.get("lastUpdatedDate"):
            notify_incident = True
        elif not last_incident_post_successful:
            # Retry posting if the last attempt failed
            notify_incident = True

        if notify_incident:
            print(f"[INFO] Posting incident: {current_incident.get('title')}")
            post_success = post_to_discord(incident_embed(current_incident, config), config)
            if post_success:
                state["last_incident"] = current_incident
                state["last_incident_post_successful"] = True
                changed = True
            else:
                # Save incident data but mark post as failed, so we retry next time
                state["last_incident"] = current_incident
                state["last_incident_post_successful"] = False
                changed = True
    else:
        if last_incident is not None:
            print("[INFO] Incident cleared (no longer pinned)")
            state["last_incident"] = None
            state["last_incident_post_successful"] = True
            changed = True

    current_maintenances = data.get("maintenanceList") or []
    last_maintenance_map: dict = state.get("last_maintenance") or {}

    current_map = {str(m["id"]): m for m in current_maintenances}

    for mid, maint in current_map.items():
        current_phase = maintenance_phase(maint)
        last_entry = last_maintenance_map.get(mid)

        notify_maint = False
        if last_entry is None:
            notify_maint = True
        elif last_entry.get("phase") != current_phase:
            notify_maint = True
        elif last_entry.get("title") != maint.get("title") or \
            last_entry.get("description") != maint.get("description"):
            notify_maint = True
        elif not last_entry.get("post_successful", True):
            # Retry posting if the last attempt failed
            notify_maint = True

        if notify_maint:
            print(f"[INFO] Posting maintenance '{maint.get('title')}' (phase={current_phase})")
            post_success = post_to_discord(maintenance_embed(maint, config), config)
            timeslots = maint.get("timeslotList", [])
            is_manual = not bool(timeslots)
            last_maintenance_map[mid] = {
                "id":          mid,
                "title": maint.get("title"),
                "description": maint.get("description"),
                "phase": current_phase,
                "post_successful": post_success,
                "is_manual": is_manual,
            }
            changed = True

    for mid in list(last_maintenance_map.keys()):
        if mid not in current_map:
            last_entry = last_maintenance_map.get(mid, {})
            # If the maintenance was active when removed, post a 'Maintenance Ended' notification
            if last_entry.get("phase") == "active":
                print(f"[INFO] Manual maintenance {mid} ended - posting end notification")
                embed = {
                    "content": None,
                    "embeds": [{
                        "title": last_entry.get("title", "Maintenance"),
                        "description": html_to_discord_markdown(last_entry.get("description", "") or ""),
                        "url": config["embed_link_url"] or config["status_page_url"],
                        "color": MAINTENANCE_COLOR,
                        "author": {"name": "Maintenance Ended"},
                    }],
                    "attachments": []
                }
                post_to_discord(embed, config)
            else:
                print(f"[INFO] Maintenance {mid} removed from page - pruning state")
            del last_maintenance_map[mid]
            changed = True

    state["last_maintenance"] = last_maintenance_map

    if changed:
        save_state(state, config["state_file"])
        print("[INFO] State saved.")
    else:
        print("[INFO] No changes detected, nothing to post.")


if __name__ == "__main__":
    main()
