"""
Fantasy Roster Tracker
=======================
Pulls current rosters from ESPN + Yahoo fantasy football leagues and saves
them to rosters.json (league -> team -> players, each with their NFL team)
plus a compact, human/AI-readable rosters.txt.

Normal run (fetch + save):        python fantasy_tracker.py
Look up a player (reads the file): python fantasy_tracker.py --find "josh allen"

Credentials come from environment variables (see SETUP.md). Locally, put them
in a .env file next to this script; in GitHub Actions they come from Secrets.

Resilience: if one source fails (expired cookies, Yahoo layout change, etc.)
the run does NOT stop. That league's previous rosters are carried forward and
marked "stale_since", the problem is recorded under "errors", and the script
exits with code 2 so GitHub Actions still shows red (after committing the data).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

# ============ LEAGUE CONFIG (not secret — safe to keep in the repo) ============
# ESPN league ID: from the URL, e.g. ...leagueId=123456
ESPN_LEAGUES = [
    {"name": "McCarthy Precon", "league_id": 1912089737},
]
ESPN_YEAR = 2026

# Yahoo leagues read through the official API (needs YAHOO_REFRESH_TOKEN; only
# used once Yahoo approves API access). Leave empty to skip.
YAHOO_LEAGUES = [
    {"name": "Dan B's Death League", "league_id": 1585076},
]

# Yahoo leagues read straight from the Yahoo website. Public leagues need no
# login; private ones need the YAHOO_Y / YAHOO_T cookies.
YAHOO_PUBLIC_LEAGUES = [
    {"name": "Barringtons and Russes", "league_id": 202014},
    {"name": "Dan B's Death League", "league_id": 1585076},  # private: needs YAHOO_Y / YAHOO_T cookies
]

OUTPUT_FILE = "rosters.json"
TEXT_FILE = "rosters.txt"
YAHOO_TOKEN_FILE = "oauth2.json"  # built from env vars each run, never committed
# ================================================================================


class SourceError(Exception):
    """A data source could not be read. The run continues with the previous data for that league."""


def fail(message):
    """Print a clear error and stop with a non-zero exit code (only for setup mistakes)."""
    print("\nERROR: " + message, file=sys.stderr)
    sys.exit(1)


def env(name):
    """Read an environment variable ('' if missing). Whitespace is removed because
    values copied out of a terminal often pick up a stray line break."""
    return "".join(os.environ.get(name, "").split())


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- ESPN

def get_espn_rosters():
    """Returns a list of league dicts in the final JSON shape. Raises SourceError on trouble."""
    from espn_api.football import League
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
        ESPNUnknownError,
    )

    espn_s2 = env("ESPN_S2")
    swid = env("ESPN_SWID")
    if not espn_s2 or not swid:
        raise SourceError("ESPN_S2 / ESPN_SWID are not set (add them to .env locally or GitHub Secrets).")

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
            raise SourceError(
                f"ESPN denied access to league {cfg['league_id']} ({cfg['name']}). "
                "The espn_s2 / SWID cookies have most likely EXPIRED. "
                "Fix: log into espn.com, copy fresh cookie values, update the ESPN_S2 and ESPN_SWID secrets."
            )
        except ESPNInvalidLeague:
            raise SourceError(f"ESPN league ID {cfg['league_id']} ({cfg['name']}) not found for {ESPN_YEAR}.")
        except ESPNUnknownError as e:
            raise SourceError(f"ESPN returned an unexpected error for league {cfg['league_id']}: {e}")
        except Exception as e:  # network blips etc.
            raise SourceError(f"ESPN request failed for league {cfg['league_id']}: {e}")

        teams = []
        for team in league.teams:
            players = [{"name": p.name, "position": p.position, "nfl_team": p.proTeam} for p in team.roster]
            teams.append({"team_name": team.team_name, "players": players})
        if not teams:
            raise SourceError(f"ESPN returned no teams for league {cfg['league_id']} ({cfg['name']}).")

        leagues.append({"league_name": cfg["name"], "source": "espn", "teams": teams})
        print(f"ESPN: {cfg['name']} — {len(teams)} teams")
    return leagues


# ----------------------------------------------------------------------------- Yahoo (official API)

def yahoo_api_is_configured():
    """True only when a real Yahoo refresh token is present (not blank, not the .env placeholder)."""
    token = env("YAHOO_REFRESH_TOKEN")
    return bool(token) and token != "filled_in_by_step_3"


def write_yahoo_token_file():
    data = {
        "consumer_key": env("YAHOO_CLIENT_ID"),
        "consumer_secret": env("YAHOO_CLIENT_SECRET"),
        "refresh_token": env("YAHOO_REFRESH_TOKEN"),
        "access_token": "expired",  # placeholder; gets replaced by the refresh
        "token_type": "bearer",
        "token_time": 0,
    }
    with open(YAHOO_TOKEN_FILE, "w") as f:
        json.dump(data, f)


def get_yahoo_api_rosters():
    """Yahoo official API path. Returns league dicts. Raises SourceError on trouble."""
    from yahoo_oauth import OAuth2
    from yahoo_fantasy_api import Game

    write_yahoo_token_file()
    try:
        oauth = OAuth2(None, None, from_file=YAHOO_TOKEN_FILE)
        if not oauth.token_is_valid():
            oauth.refresh_access_token()
    except Exception as e:
        raise SourceError(
            "Yahoo API login failed (refresh token invalid or revoked). "
            f"Fix: run python yahoo_login.py once locally and update YAHOO_REFRESH_TOKEN. Details: {e}"
        )

    game = Game(oauth, "nfl")
    game_id = game.game_id()

    leagues = []
    for cfg in YAHOO_LEAGUES:
        league_key = f"{game_id}.l.{cfg['league_id']}"
        try:
            league = game.to_league(league_key)
            yahoo_teams = league.teams()
        except Exception as e:
            raise SourceError(f"Could not load Yahoo league {cfg['league_id']} ({cfg['name']}) via API: {e}")

        teams = []
        for team_key, team_info in yahoo_teams.items():
            roster = league.to_team(team_key).roster()
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

        leagues.append({"league_name": cfg["name"], "source": "yahoo-api", "teams": teams})
        print(f"Yahoo (API): {cfg['name']} — {len(teams)} teams")
    return leagues


# ----------------------------------------------------------------------------- Yahoo (website)

BROWSER_HEADERS = {
    # Yahoo serves the normal page to anything that looks like a browser.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Team links on the league's /teams page look like:
#   <a href="https://football.fantasysports.yahoo.com/f1/202014/4">Go Sports</a>
# (the league home page only shows a few teams in some league types, so we use /teams)
TEAM_LINK_RE = re.compile(r'<a[^>]*href="[^"]*/f1/(\d+)/(\d+)"[^>]*>([^<]{1,80})</a>')

# Each roster row has the player name in a link, then "Was - QB" in a small span:
#   <a class="Nowrap name F-link playernote" ... title="Jayden Daniels">Jayden Daniels</a>
#   ... <span class="Fz-xxs">Was - QB</span>
PLAYER_RE = re.compile(
    r'<a[^>]*class="[^"]*\bname\b[^"]*"[^>]*title="([^"]+)"[^>]*>[^<]*</a>'  # name
    r'.*?<span class="Fz-xxs">([A-Za-z]{2,3}) - ([A-Z,/]+)</span>',              # team - position
    re.DOTALL,
)


def yahoo_cookies():
    """Optional login cookies (Y and T from Chrome) so private leagues can be read."""
    y, t = env("YAHOO_Y"), env("YAHOO_T")
    if y and t and y != "paste_here":
        return {"Y": y, "T": t}
    return {}


def fetch_html(url):
    import requests
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, cookies=yahoo_cookies(), timeout=30)
    except Exception as e:
        raise SourceError(f"Request to {url} failed: {e}")
    if resp.status_code != 200:
        raise SourceError(f"Yahoo returned HTTP {resp.status_code} for {url}")
    if "login.yahoo.com" in resp.url:
        raise SourceError(
            f"Yahoo redirected {url} to a login page. The league is private and the YAHOO_Y / YAHOO_T "
            "cookies are missing or EXPIRED. Fix: copy fresh Y and T cookies from Chrome and update the secrets."
        )
    return resp.text


def scrape_public_league(cfg):
    """Read one league from the Yahoo website. Raises SourceError if it can't."""
    base = f"https://football.fantasysports.yahoo.com/f1/{cfg['league_id']}"
    html = fetch_html(f"{base}/teams")

    team_names = {}
    for league_id, team_num, name in TEAM_LINK_RE.findall(html):
        name = name.strip()
        if int(league_id) == cfg["league_id"] and team_num not in team_names and name and name != "My Team":
            team_names[team_num] = name
    if not team_names:
        raise SourceError(f"Found no teams on {base}/teams — league is not viewable (private without cookies) or the page layout changed.")

    teams = []
    for team_num in sorted(team_names, key=int):
        page = fetch_html(f"{base}/{team_num}")
        players, seen = [], set()
        for name, nfl_team, position in PLAYER_RE.findall(page):
            if name in seen:          # the page repeats a few players in side widgets
                continue
            seen.add(name)
            players.append({"name": name, "position": position, "nfl_team": nfl_team.upper()})
        if not players:
            raise SourceError(f"Found no players on {base}/{team_num} — page layout may have changed.")
        teams.append({"team_name": team_names[team_num], "players": players})

    return {"league_name": cfg["name"], "source": "yahoo-web", "teams": teams}


# ----------------------------------------------------------------------------- Assemble + output

def load_previous():
    """Previous rosters.json (if any) so a failing league can be carried forward."""
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        return {lg["league_name"]: lg for lg in data.get("leagues", [])}
    except (FileNotFoundError, ValueError, KeyError):
        return {}


def collect_all():
    """Run every source. Returns (leagues, errors). Never raises for a single bad source."""
    previous = load_previous()
    results = {}   # league_name -> league dict (fresh)
    errors = []    # {"league": ..., "message": ...}

    def record(league_name, message):
        print(f"PROBLEM [{league_name}]: {message}")
        errors.append({"league": league_name, "message": message})

    # ESPN
    try:
        for lg in get_espn_rosters():
            results[lg["league_name"]] = lg
    except SourceError as e:
        for cfg in ESPN_LEAGUES:
            record(cfg["name"], str(e))
    except Exception as e:
        for cfg in ESPN_LEAGUES:
            record(cfg["name"], f"Unexpected ESPN error: {e!r}")

    # Yahoo website (public leagues, plus private ones when cookies are set)
    for cfg in YAHOO_PUBLIC_LEAGUES:
        try:
            lg = scrape_public_league(cfg)
            results[lg["league_name"]] = lg
            print(f"Yahoo (web): {cfg['name']} — {len(lg['teams'])} teams")
        except SourceError as e:
            record(cfg["name"], str(e))
        except Exception as e:
            record(cfg["name"], f"Unexpected Yahoo error: {e!r}")

    # Yahoo official API (only if configured) — fills in anything the website path missed
    if yahoo_api_is_configured():
        try:
            for lg in get_yahoo_api_rosters():
                if lg["league_name"] not in results:
                    results[lg["league_name"]] = lg
        except SourceError as e:
            record("Yahoo API", str(e))
        except Exception as e:
            record("Yahoo API", f"Unexpected Yahoo API error: {e!r}")
    else:
        print("Yahoo (API): not configured — skipped (website path is used instead)")

    # Final league list in config order; carry forward anything that failed this run.
    ordered_names = [c["name"] for c in ESPN_LEAGUES]
    for c in YAHOO_PUBLIC_LEAGUES + YAHOO_LEAGUES:
        if c["name"] not in ordered_names:
            ordered_names.append(c["name"])

    leagues = []
    for name in ordered_names:
        if name in results:
            lg = results[name]
            lg.pop("stale_since", None)
            lg["fetched_at"] = now_utc()
            leagues.append(lg)
        elif name in previous:
            lg = previous[name]
            lg.setdefault("stale_since", lg.get("fetched_at") or now_utc())
            leagues.append(lg)
            print(f"CARRIED FORWARD [{name}]: using previous rosters (stale since {lg['stale_since']})")
        else:
            print(f"MISSING [{name}]: no fresh data and nothing to carry forward")

    # Errors only count if some league ended up without fresh data (e.g. the API
    # path failing is harmless when the website path already got that league).
    fresh = {lg["league_name"] for lg in leagues if "stale_since" not in lg}
    if all(name in fresh for name in ordered_names):
        errors = []
    else:
        errors = [e for e in errors if e["league"] not in fresh]
    return leagues, errors


def save_outputs(leagues, errors):
    data = {
        "updated_at": now_utc(),
        "status": "ok" if not errors else "problem",
        "errors": errors,
        "leagues": leagues,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    # Compact text version: one line per team. Easy for people and AI models to read.
    lines = ["FANTASY ROSTERS SNAPSHOT",
             f"updated_at: {data['updated_at']} (UTC; Phoenix = UTC-7)",
             f"leagues: {len(leagues)} | teams: {sum(len(lg['teams']) for lg in leagues)} | "
             f"players: {sum(len(t['players']) for lg in leagues for t in lg['teams'])}",
             f"status: {data['status']}"]
    for e in errors:
        lines.append(f"error: [{e['league']}] {e['message']}")
    for lg in leagues:
        stale = f" — STALE, data from {lg['stale_since']}" if lg.get("stale_since") else ""
        lines.append("")
        lines.append(f"## {lg['league_name']} ({lg['source']}){stale}")
        for t in lg["teams"]:
            plist = "; ".join(f"{p['name']} {p['position']}-{p['nfl_team']}" for p in t["players"])
            lines.append(f"{t['team_name']}: {plist}")
    lines.append("")
    lines.append("END")
    with open(TEXT_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")

    total = sum(len(t["players"]) for lg in leagues for t in lg["teams"])
    print(f"\nSaved {total} players across {len(leagues)} leagues to {OUTPUT_FILE} and {TEXT_FILE}")
    if errors:
        print(f"{len(errors)} problem(s) recorded — see 'errors' in {OUTPUT_FILE}")


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
        stale = f" (STALE since {lg['stale_since']})" if lg.get("stale_since") else ""
        for team in lg["teams"]:
            for p in team["players"]:
                if query in p["name"].lower():
                    hits.append((p["name"], p.get("position", ""), p["nfl_team"], team["team_name"], lg["league_name"] + stale))

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
        all_leagues, problems = collect_all()
        if not all_leagues:
            fail("No league data at all (every source failed and there is no previous rosters.json).")
        save_outputs(all_leagues, problems)
        # Exit 2 when something needs attention. The workflow commits the data first
        # and checks this afterwards, so the Actions tab turns red without losing data.
        sys.exit(2 if problems else 0)
