#!/usr/bin/env python3
"""
update_workout.py — pulls latest activity from Strava + Whoop,
finds the matching card in index.html, marks it Done with real stats.

Usage:
  python3 scripts/update_workout.py [--activity-id STRAVA_ID]

Environment variables (set via GitHub Actions secrets):
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
  WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET, WHOOP_REFRESH_TOKEN
"""

import json, os, re, subprocess, sys
from datetime import datetime, timezone

# ── Credentials ──────────────────────────────────────────────────────────────

def get_env(key):
    val = os.environ.get(key, '').strip()
    if not val:
        raise RuntimeError(f"Missing env var: {key}")
    return val

STRAVA_CLIENT_ID     = get_env('STRAVA_CLIENT_ID')
STRAVA_CLIENT_SECRET = get_env('STRAVA_CLIENT_SECRET')
STRAVA_REFRESH_TOKEN = get_env('STRAVA_REFRESH_TOKEN')
WHOOP_CLIENT_ID      = get_env('WHOOP_CLIENT_ID')
WHOOP_CLIENT_SECRET  = get_env('WHOOP_CLIENT_SECRET')
WHOOP_REFRESH_TOKEN  = get_env('WHOOP_REFRESH_TOKEN')

# ── Strava ───────────────────────────────────────────────────────────────────

def strava_refresh():
    r = subprocess.run(['curl','-s','-X','POST','https://www.strava.com/oauth/token',
        '-d','client_id='+STRAVA_CLIENT_ID,
        '-d','client_secret='+STRAVA_CLIENT_SECRET,
        '-d','refresh_token='+STRAVA_REFRESH_TOKEN,
        '-d','grant_type=refresh_token'], capture_output=True, text=True)
    d = json.loads(r.stdout)
    if 'access_token' not in d:
        raise RuntimeError(f"Strava refresh failed: {d}")
    return d['access_token']

def strava_get(path, token):
    auth = "Authorization: " + "Bearer " + token
    r = subprocess.run(['curl','-s', 'https://www.strava.com/api/v3' + path, '-H', auth],
        capture_output=True, text=True)
    return json.loads(r.stdout)

def get_strava_activity(activity_id=None):
    token = strava_refresh()
    if activity_id:
        return strava_get(f'/activities/{activity_id}', token)
    acts = strava_get('/athlete/activities?per_page=1', token)
    if not acts:
        raise RuntimeError("No Strava activities found")
    return acts[0]

# ── Whoop ────────────────────────────────────────────────────────────────────

def whoop_refresh():
    r = subprocess.run(['curl','-s','-X','POST',
        'https://api.prod.whoop.com/oauth/oauth2/token',
        '-H','Content-Type: application/x-www-form-urlencoded',
        '-d','grant_type=refresh_token'
            '&client_id='+WHOOP_CLIENT_ID+
            '&client_secret='+WHOOP_CLIENT_SECRET+
            '&refresh_token='+WHOOP_REFRESH_TOKEN],
        capture_output=True, text=True)
    d = json.loads(r.stdout)
    if 'access_token' not in d:
        raise RuntimeError(f"Whoop refresh failed: {d}")
    return d['access_token']

def whoop_get(path, token):
    auth = "Authorization: " + "Bearer " + token
    r = subprocess.run(['curl','-s',
        'https://api.prod.whoop.com/developer' + path, '-H', auth],
        capture_output=True, text=True)
    return json.loads(r.stdout)

def get_whoop_for_date(date_str):
    """Returns (workout_score, recovery_score) for a given YYYY-MM-DD."""
    token = whoop_refresh()

    # Workout
    wdata = whoop_get('/v2/activity/workout?limit=10', token)
    workout = None
    for w in wdata.get('records', []):
        if w.get('start', '')[:10] == date_str:
            workout = w.get('score') or {}
            break

    # Recovery
    rdata = whoop_get('/v2/recovery?limit=7', token)
    recovery = None
    for rec in rdata.get('records', []):
        if rec.get('created_at', '')[:10] == date_str:
            recovery = rec.get('score') or {}
            break

    return workout, recovery

# ── Stats builder ─────────────────────────────────────────────────────────────

def build_stats(activity, workout, recovery):
    """
    Build the stats string for the card.
    Returns a string with <br> line breaks, using <b> for highlights.
    Follows the skill's formatting rule: no CSS grid, simple rows.
    """
    lines = []

    # Row 1: distance / time / power
    dist = activity.get('distance', 0)
    duration = activity.get('moving_time', 0)
    avg_watts = activity.get('average_watts')
    np_watts = activity.get('weighted_average_watts')
    elev = activity.get('total_elevation_gain', 0)

    row1_parts = []
    if dist and dist > 100:
        row1_parts.append(f'<b>{round(dist/1000, 1)}km</b>')
    row1_parts.append(f'{round(duration/60)}min')
    if avg_watts:
        row1_parts.append(f'{round(avg_watts)}W avg')
    if np_watts and np_watts != avg_watts:
        row1_parts.append(f'NP <b>{round(np_watts)}W</b>')
    if elev and elev > 50:
        row1_parts.append(f'{round(elev)}m elev')
    if row1_parts:
        lines.append(' · '.join(row1_parts))

    # Row 2: HR + Whoop strain
    avg_hr = activity.get('average_heartrate')
    max_hr = activity.get('max_heartrate')
    strain = workout.get('strain') if workout else None

    row2_parts = []
    if avg_hr:
        row2_parts.append(f'HR <b>{round(avg_hr)}</b> avg')
    if max_hr:
        row2_parts.append(f'{round(max_hr)} max')
    if strain:
        row2_parts.append(f'Strain <b>{round(strain, 1)}</b>')
    if row2_parts:
        lines.append(' · '.join(row2_parts))

    # Row 3: Whoop recovery
    if recovery:
        rec_pct = recovery.get('recovery_score')
        hrv = recovery.get('hrv_rmssd_milli')
        rhr = recovery.get('resting_heart_rate')
        row3_parts = []
        if rec_pct is not None:
            row3_parts.append(f'Recovery <b>{round(rec_pct)}%</b>')
        if hrv:
            row3_parts.append(f'HRV <b>{round(hrv)}ms</b>')
        if rhr:
            row3_parts.append(f'RHR {round(rhr)}')
        if row3_parts:
            lines.append(' · '.join(row3_parts))

    return '<br>'.join(lines) if lines else ''

# ── HTML patcher ──────────────────────────────────────────────────────────────

def patch_calendar(date_str, stats_html):
    """
    Finds the card for date_str in index.html and marks it Done with stats.
    The card is in the JS TRAINING data object as:
      { date: 'YYYY-MM-DD', ... cls: '', badge: 'b-xxx', badgeText: 'Xxx', ... }
    """
    html_path = os.path.join(os.path.dirname(__file__), '..', 'index.html')
    html_path = os.path.normpath(html_path)

    with open(html_path, 'r') as f:
        content = f.read()

    # Find the block for this date
    date_pattern = rf"(date:\s*'{re.escape(date_str)}'[^{{}}]*?)\n(\s*\}},)"

    # More robust: find the date line and the closing brace of that object
    # Look for the date key and capture until we see the next date: or closing of days array
    # Strategy: find line with date, then find that whole object and patch cls/badge/stats

    # Find position of date key
    date_marker = f"date: '{date_str}'"
    pos = content.find(date_marker)
    if pos == -1:
        print(f"WARNING: date {date_str} not found in index.html — skipping patch")
        return False

    # Find the end of this card object (next },  at same indent level)
    # Look forward from pos for the closing },
    end_pos = content.find('\n        },', pos)
    if end_pos == -1:
        end_pos = content.find('\n        }', pos)
    if end_pos == -1:
        print(f"WARNING: couldn't find end of card object for {date_str}")
        return False

    card_block = content[pos:end_pos]

    # Already done? Check
    if "cls: 'done'" in card_block:
        print(f"Card {date_str} already marked done — updating stats only")

    # Build patched block
    patched = card_block

    # Update cls to 'done'
    patched = re.sub(r"cls:\s*'[^']*'", "cls: 'done'", patched)

    # Update badge
    patched = re.sub(r"badge:\s*'[^']*'", "badge: 'b-done'", patched)
    patched = re.sub(r"badgeText:\s*'[^']*'", "badgeText: 'Done'", patched)

    # Update or insert stats (escape for JS string)
    stats_escaped = stats_html.replace("'", "\\'")
    if 'stats:' in patched:
        patched = re.sub(r"stats:\s*'[^']*'", f"stats: '{stats_escaped}'", patched)
    else:
        # Insert stats before the closing of the block
        patched = patched.rstrip() + f",\n          stats: '{stats_escaped}'"

    content = content[:pos] + patched + content[end_pos:]

    with open(html_path, 'w') as f:
        f.write(content)

    print(f"✅ Patched card for {date_str}")
    return True

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    activity_id = None
    if len(sys.argv) > 2 and sys.argv[1] == '--activity-id':
        activity_id = sys.argv[2]

    print("Fetching Strava activity...")
    activity = get_strava_activity(activity_id)
    date_str = activity['start_date_local'][:10]
    print(f"Activity: {activity['name']} on {date_str}")
    print(f"  Distance: {round(activity.get('distance',0)/1000,1)}km")
    print(f"  Duration: {round(activity.get('moving_time',0)/60)}min")
    print(f"  Avg HR: {activity.get('average_heartrate')}")
    print(f"  Avg Watts: {activity.get('average_watts')}")

    print("Fetching Whoop data...")
    workout, recovery = get_whoop_for_date(date_str)
    if workout:
        print(f"  Whoop strain: {workout.get('strain')}")
    if recovery:
        print(f"  Recovery: {recovery.get('recovery_score')}% · HRV: {recovery.get('hrv_rmssd_milli')}")

    stats = build_stats(activity, workout, recovery)
    print(f"Stats HTML: {stats}")

    patched = patch_calendar(date_str, stats)
    if not patched:
        print("No card patched — date may not be in the training plan yet")
        sys.exit(0)

    print("Done!")

if __name__ == '__main__':
    main()
