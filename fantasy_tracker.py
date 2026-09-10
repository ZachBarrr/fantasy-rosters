"""
Fantasy Roster Tracker
=======================
Pulls current rosters from ESPN + Yahoo fantasy football leagues and saves
them to rosters.json (league -> team -> players, each with their NFL team).

Normal run (fetch + save):        python fantasy_tracker.py
Look up a player (reads the file): python fantasy_tracker.py --find "josh allen"

Credentials come from environment variables (see SETUP.md). Locally, put them
in a .env file next to this script; in GitHub Actions they come from Secrets.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# ============ LEAGUE CONFIG (not secret — safe to keep in the repo) ============
# ESPN league ID: from the URL, e.g. ...leagueId=123456
ESPN_LEAGUES = [
    {"name": "McCarthy Precon", "league_id": 1912089737},
]
ESPN_YEAR = 2026

# Yahoo league ID: the number in the URL, e.g. .../f1/123456  -> 123456
YAHOO_LEAGUES = [
    {"name": "Yahoo League B", "league_id": 1585076},  # rename to whatever you call it
]

# Yahoo leagues set to "viewable by the public" (commissioner setting). These are
# read straight from the Yahoo website with no login or API approval needed.
YAHOO_PUBLIC_LEAGUES = [
    {"name": "Barringtons and Russes", "league_id": 202014},
]

OUTPUT_FILE = "rosters.json"
YAHOO_TOKEN_FILE = "oauth2.json"  # built from env vars each run, never committed
# ================================================================================


def fail(message):
    """Print a clear error and stop with a non-zero exit code (so GitHub Actions shows red)."""
    print("\nERROR: " + message, file=sys.stderr)
    sys.exit(1)


def require_env(name):
    """Read an environment variable or fail with a clear message if it's missing."""
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"Missing environment variable {name}. Add it to .env (local) or GitHub Secrets (Actions).")
    return value


# ----------------------------------------------------------------------------- ESPN

def get_espn_rosters():
    """Returns a list of league dicts in the final JSON shape."""
    from espn_api.football import League
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
        ESPNUnknownError,
    )

    espn_s2 = require_env("ESPN_S2")
    swid = require_env("ESPN_SWID")

    leagues = []
    for cfg in ESPN_LEAGUES:
        try:
            league = League(
                league_id=cfg["league_id"],
                year=ESPN_YEAR,
                espn_s2=espn_s2,
                swid=swid,
            )
        except ESPNAccessDenied:
            fail(
                f"ESPN denied access to league {cfg['league_id']} ({cfg['name']}).\n"
                "  Your espn_s2 / SWID cookies have most likely EXPIRED.\n"
                "  Fix: log into espn.com, copy fresh cookie values, update ESPN_S2 and ESPN_SWID."
            )
        except ESPNInvalidLeague:
            fail(f"ESPN league ID {cfg['league_id']} ({cfg['name']}) not found for {ESPN_YEAR}. Check the ID/year.")
        except ESPNUnknownError as e:
            fail(f"ESPN returned an unexpected error for league {cfg['league_id']}: {e}")

        teams = []
        for team in league.teams:
            players = [{"name": p.name, "position": p.position, "nfl_team": p.proTeam} for p in team.roster]
            teams.append({"team_name": team.team_name, "players": players})

        leagues.append({"league_name": cfg["name"], "source": "espn", "teams": teams})
        print(f"ESPN: {cfg['name']} — {len(teams)} teams")
    return leagues


# ----------------------------------------------------------------------------- Yahoo

def write_yahoo_token_file():
    """
    yahoo_oauth wants a JSON file. We build it from env vars so nothing secret
    lives in the repo. token_time=0 forces an immediate silent refresh using
    the refresh token — no browser needed.
    """
    data = {
        "consumer_key": require_env("YAHOO_CLIENT_ID"),
        "consumer_secret": require_env("YAHOO_CLIENT_SECRET"),
        "refresh_token": require_env("YAHOO_REFRESH_TOKEN"),
        "access_token": "expired",  # placeholder; gets replaced by the refresh
        "token_type": "bearer",
        "token_time": 0,
    }
    with open(YAHOO_TOKEN_FILE, "w") as f:
        json.dump(data, f)


def get_yahoo_rosters():
    """Returns a list of league dicts in the final JSON shape."""
    from yahoo_oauth import OAuth2
    from yahoo_fantasy_api import Game

    write_yahoo_token_file()

    try:
        oauth = OAuth2(None, None, from_file=YAHOO_TOKEN_FILE)
        if not oauth.token_is_valid():
            oauth.refresh_access_token()
    except Exception as e:
        fail(
            "Yahoo login failed. The refresh token is probably invalid or revoked.\n"
            "  Fix: run  python yahoo_login.py  once locally and update YAHOO_REFRESH_TOKEN.\n"
            f"  Details: {e}"
        )

    game = Game(oauth, "nfl")
    game_id = game.game_id()  # e.g. "461" — Yahoo's code for the current NFL season

    leagues = []
    for cfg in YAHOO_LEAGUES:
        league_key = f"{game_id}.l.{cfg['league_id']}"
        try:
            league = game.to_league(league_key)
            yahoo_teams = league.teams()
        except Exception as e:
            fail(f"Could not load Yahoo league {cfg['league_id']} ({cfg['name']}). Check the ID.\n  Details: {e}")

        teams = []
        for team_key, team_info in yahoo_teams.items():
            roster = league.to_team(team_key).roster()

            # Some versions of the library include the NFL team on the roster
            # entry; if not, look it up in one batch call via player_details().
            missing_ids = [p["player_id"] for p in roster if not p.get("editorial_team_abbr")]
            nfl_team_by_id = {}
            if missing_ids:
                for details in league.player_details(missing_ids):
                    nfl_team_by_id[int(details["player_id"])] = details.get("editorial_team_abbr", "")

            players = []
            for p in roster:
                nfl_team = p.get("editorial_team_abbr") or nfl_team_by_id.get(int(p["player_id"]), "")
                players.append({"name": p["name"], "position": p.get("selected_position", ""), "nfl_team": nfl_team.upper()})

            teams.append({"team_name": team_info["name"], "players": players})

        leagues.append({"league_name": cfg["name"], "source": "yahoo", "teams": teams})
        print(f"Yahoo: {cfg['name']} — {len(teams)} teams")
    return leagues


# ----------------------------------------------------------------------------- Yahoo (public leagues, no login)

import re

BROWSER_HEADERS = {
    # Yahoo serves the normal page to anything that looks like a browser.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Team links on the league home page look like:
#   <a class="F-link" href="https://football.fantasysports.yahoo.com/f1/202014/4">Go Sports</a>
TEAM_LINK_RE = re.compile(r'<a[^>]*class="F-link"[^>]*href="[^"]*/f1/(\d+)/(\d+)"[^>]*>([^<]+)</a>')

# Each roster row has the player name in a link, then "Was - QB" in a small span:
#   <a class="Nowrap name F-link playernote" ... title="Jayden Daniels">Jayden Daniels</a>
#   ... <span class="Fz-xxs">Was - QB</span>
PLAYER_RE = re.compile(
    r'<a[^>]*class="[^"]*\bname\b[^"]*"[^>]*title="([^"]+)"[^>]*>[^<]*</a>'  # name
    r'.*?<span class="Fz-xxs">([A-Za-z]{2,3}) - ([A-Z,/]+)</span>',              # team - position
    re.DOTALL,
)


def fetch_html(url):
    import requests
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    if resp.status_code != 200:
        fail(f"Yahoo returned HTTP {resp.status_code} for {url}. If the league is no longer public, this stops working.")
    return resp.text


def get_yahoo_public_rosters():
    """Scrape rosters from Yahoo leagues that are publicly viewable. No credentials needed."""
    leagues = []
    for cfg in YAHOO_PUBLIC_LEAGUES:
        base = f"https://football.fantasysports.yahoo.com/f1/{cfg['league_id']}"
        html = fetch_html(base)

        # Build {team_number: team_name}, keeping the first name seen for each number.
        team_names = {}
        for league_id, team_num, name in TEAM_LINK_RE.findall(html):
            if int(league_id) == cfg["league_id"] and team_num not in team_names:
                team_names[team_num] = name.strip()
        if not team_names:
            fail(f"Found no teams on {base}. Is the league set to 'viewable by the public'?")

        teams = []
        for team_num in sorted(team_names, key=int):
            page = fetch_html(f"{base}/{team_num}")
            players = []
            seen = set()
            for name, nfl_team, position in PLAYER_RE.findall(page):
                if name in seen:          # the page repeats a few players in side widgets
                    continue
                seen.add(name)
                players.append({"name": name, "position": position, "nfl_team": nfl_team.upper()})
            teams.append({"team_name": team_names[team_num], "players": players})

        leagues.append({"league_name": cfg["name"], "source": "yahoo-public", "teams": teams})
        print(f"Yahoo (public): {cfg['name']} — {len(teams)} teams")
    return leagues


def yahoo_is_configured():
    """True only when a real Yahoo refresh token is present (not blank, not the .env placeholder)."""
    token = os.environ.get("YAHOO_REFRESH_TOKEN", "").strip()
    return bool(token) and token != "filled_in_by_step_3"


# ----------------------------------------------------------------------------- Output

def save_to_json(leagues):
    data = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "leagues": leagues,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)
    total = sum(len(t["players"]) for lg in leagues for t in lg["teams"])
    print(f"\nSaved {total} players across {len(leagues)} leagues to {OUTPUT_FILE}")


def lookup_player(query):
    """Read rosters.json and print every team that owns a matching player."""
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        fail(f"{OUTPUT_FILE} not found. Run the script without --find first.")

    query = query.lower()
    hits = []
    for lg in data["leagues"]:
        for team in lg["teams"]:
            for p in team["players"]:
                if query in p["name"].lower():
                    hits.append((p["name"], p.get("position", ""), p["nfl_team"], team["team_name"], lg["league_name"]))

    print(f"Data as of {data['updated_at']}")
    if not hits:
        print(f"No one owns a player matching '{query}' in any league.")
    for name, position, nfl_team, team, league in hits:
        print(f"  {name} ({position}, {nfl_team}) -> {team}  [{league}]")


# ----------------------------------------------------------------------------- Main

if __name__ == "__main__":
    # Load .env if present (local runs). Harmless in GitHub Actions where it won't exist.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Fantasy roster tracker")
    parser.add_argument("--find", help="Look up who owns a player (reads rosters.json, no fetching)")
    args = parser.parse_args()

    if args.find:
        lookup_player(args.find)
    else:
        all_leagues = get_espn_rosters()
        all_leagues += get_yahoo_public_rosters()
        if yahoo_is_configured():
            all_leagues += get_yahoo_rosters()
        else:
            print("Yahoo: skipped (YAHOO_REFRESH_TOKEN not set yet — run yahoo_login.py once Yahoo approves API access)")
        save_to_json(all_leagues)
