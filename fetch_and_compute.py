#!/usr/bin/env python3
"""
EPL Minute-Adjusted Age tracker.

For each finished Premier League fixture, computes two per-team age metrics:
  - starting_xi_simple_avg_age_years: naive average age of the 11 starters
    (the way most outlets do it), to day precision.
  - minute_adjusted_age_years: age weighted by each player's actual minutes
    played in that match (so a 25-year-old who plays 45 minutes and a
    35-year-old who plays the other 45 count equally, producing the
    equivalent of a 30-year-old playing all 90).

Ages are always computed to exact day precision from real birthdates, then
expressed as a fractional year value (days / 365.25) for readability.

Data is cached and accumulated in the data/ directory so re-runs are cheap
and idempotent:
  data/config.json         - resolved league id + season
  data/players.json        - player_id -> {name, birth_date, team_id, team_name}
  data/matches.json        - one row per (fixture, team) already processed
  data/season_summary.json - recomputed every run from matches.json

Requires the API_FOOTBALL_KEY environment variable (a free api-football.com
key). Designed to run inside GitHub Actions, which has unrestricted outbound
network access (unlike the sandbox that authored this script).
"""
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests

API_BASE = "https://v3.football.api-sports.io"
DATA_DIR = Path(__file__).parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
PLAYERS_PATH = DATA_DIR / "players.json"
MATCHES_PATH = DATA_DIR / "matches.json"
SEASON_SUMMARY_PATH = DATA_DIR / "season_summary.json"

SEASON_LABEL = "2026-2027"  # informational only; season year resolved dynamically below

SESSION = requests.Session()
REQUEST_COUNT = 0


def api_get(path, params=None, retries=3):
    """GET against API-Football, with basic retry/backoff and request counting."""
    global REQUEST_COUNT
    url = f"{API_BASE}{path}"
    for attempt in range(retries):
        REQUEST_COUNT += 1
        resp = SESSION.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("errors"):
                # API-Football returns 200 with an "errors" object/list on logical errors
                errs = payload["errors"]
                if errs:
                    print(f"WARNING: API errors for {path} {params}: {errs}", file=sys.stderr)
            return payload
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"Rate limited on {path}, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed to fetch {path} after {retries} attempts")


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)


def resolve_league_and_season(config):
    """Find the Premier League's API-Football league id and the current season year."""
    if config.get("league_id") and config.get("season"):
        return config
    payload = api_get("/leagues", {"name": "Premier League", "country": "England"})
    responses = payload.get("response", [])
    if not responses:
        raise RuntimeError("Could not resolve Premier League from /leagues")
    league_entry = responses[0]
    league_id = league_entry["league"]["id"]
    # Find the season marked current=true, else the max year
    seasons = league_entry.get("seasons", [])
    current = [s for s in seasons if s.get("current")]
    season_year = current[0]["year"] if current else max(s["year"] for s in seasons)
    config["league_id"] = league_id
    config["season"] = season_year
    save_json(CONFIG_PATH, config)
    print(f"Resolved league_id={league_id} season={season_year}")
    return config


def refresh_squads(league_id, season, players_cache, force=False):
    """Populate players_cache with every current squad's players and birthdates."""
    last_refresh = players_cache.get("_meta", {}).get("last_refresh")
    if not force and last_refresh:
        days_since = (date.today() - date.fromisoformat(last_refresh)).days
        if days_since < 6:
            print(f"Squad cache is {days_since} day(s) old, skipping refresh.")
            return players_cache

        teams_payload = api_get("/teams", {"league": league_id, "season": season})
    teams = [t["team"] for t in teams_payload.get("response", [])]
    if not teams:
        print("WARNING: /teams returned 0 teams (API error or plan restriction?) - "
              "leaving squad cache as-is and NOT stamping last_refresh, so the next "
              "run will retry instead of treating this as a fresh, empty cache.")
        return players_cache
    print(f"Refreshing squads for {len(teams)} teams...")

    for team in teams:
        team_id = team["id"]
        team_name = team["name"]
        page = 1
        while True:
            payload = api_get("/players", {"team": team_id, "season": season, "page": page})
            for entry in payload.get("response", []):
                p = entry["player"]
                birth_date = (p.get("birth") or {}).get("date")
                if not birth_date:
                    continue
                players_cache[str(p["id"])] = {
                    "name": p["name"],
                    "birth_date": birth_date,
                    "team_id": team_id,
                    "team_name": team_name,
                }
            paging = payload.get("paging", {})
            if page >= paging.get("total", 1):
                break
            page += 1

    players_cache.setdefault("_meta", {})["last_refresh"] = date.today().isoformat()
    save_json(PLAYERS_PATH, players_cache)
    return players_cache


def fetch_missing_player(player_id, players_cache, season):
    """Fallback lookup for a player not in the squad cache (e.g. a mid-season signing)."""
    payload = api_get("/players", {"id": player_id, "season": season})
    resp = payload.get("response", [])
    if not resp:
        return None
    p = resp[0]["player"]
    birth_date = (p.get("birth") or {}).get("date")
    if not birth_date:
        return None
    info = {"name": p["name"], "birth_date": birth_date, "team_id": None, "team_name": None}
    players_cache[str(player_id)] = info
    return info


def age_in_days(birth_date_str, on_date_str):
    birth = date.fromisoformat(birth_date_str)
    on = date.fromisoformat(on_date_str)
    return (on - birth).days


def days_to_years(days):
    return days / 365.25


def format_age(days):
    years = days // 365  # not exact calendar years, but a readable approximation
    remainder_days = days - years * 365
    return f"{int(years)}y {int(remainder_days)}d"


def process_fixture(fixture, players_cache, season):
    fixture_id = fixture["fixture"]["id"]
    match_date = fixture["fixture"]["date"][:10]
    round_name = fixture["league"]["round"]
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    stats_payload = api_get("/fixtures/players", {"fixture": fixture_id})
    team_blocks = stats_payload.get("response", [])
    if len(team_blocks) < 2:
        print(f"WARNING: incomplete player stats for fixture {fixture_id}, skipping.")
        return []

    rows = []
    for block in team_blocks:
        team_id = block["team"]["id"]
        team_name = block["team"]["name"]
        opponent_name = away["name"] if team_id == home["id"] else home["name"]
        is_home = team_id == home["id"]

        weighted_day_minutes_sum = 0.0
        total_minutes = 0.0
        starter_ages_days = []

        for player_entry in block.get("players", []):
            p = player_entry["player"]
            stats = player_entry["statistics"][0] if player_entry.get("statistics") else {}
            games = stats.get("games") or {}
            minutes = games.get("minutes") or 0
            is_starter = (games.get("substitute") is False)

            info = players_cache.get(str(p["id"]))
            if info is None:
                info = fetch_missing_player(p["id"], players_cache, season)
            if info is None or not info.get("birth_date"):
                continue  # can't compute age without a birthdate

            days_old = age_in_days(info["birth_date"], match_date)

            if minutes and minutes > 0:
                weighted_day_minutes_sum += days_old * minutes
                total_minutes += minutes

            if is_starter:
                starter_ages_days.append(days_old)

        if total_minutes == 0 or not starter_ages_days:
            print(f"WARNING: no usable minutes/starters for team {team_name} in fixture {fixture_id}")
            continue

        minute_adjusted_days = weighted_day_minutes_sum / total_minutes
        simple_avg_days = sum(starter_ages_days) / len(starter_ages_days)

        rows.append({
            "fixture_id": fixture_id,
            "date": match_date,
            "round": round_name,
            "team_id": team_id,
            "team": team_name,
            "opponent": opponent_name,
            "home_away": "home" if is_home else "away",
            "total_minutes": total_minutes,
            "minute_adjusted_age_days": round(minute_adjusted_days, 2),
            "minute_adjusted_age_years": round(days_to_years(minute_adjusted_days), 3),
            "minute_adjusted_age_display": format_age(minute_adjusted_days),
            "starting_xi_simple_avg_age_days": round(simple_avg_days, 2),
            "starting_xi_simple_avg_age_years": round(days_to_years(simple_avg_days), 3),
        })

    return rows


def recompute_season_summary(matches):
    """Season-to-date minute-weighted age per team, from all processed matches."""
    by_team = {}
    for m in matches:
        t = by_team.setdefault(m["team"], {
            "team": m["team"], "matches_played": 0,
            "weighted_day_minutes_sum": 0.0, "total_minutes": 0.0,
        })
        t["matches_played"] += 1
        t["weighted_day_minutes_sum"] += m["minute_adjusted_age_days"] * m["total_minutes"]
        t["total_minutes"] += m["total_minutes"]

    summary = []
    for t in by_team.values():
        if t["total_minutes"] == 0:
            continue
        season_days = t["weighted_day_minutes_sum"] / t["total_minutes"]
        summary.append({
            "team": t["team"],
            "matches_played": t["matches_played"],
            "season_minute_adjusted_age_days": round(season_days, 2),
            "season_minute_adjusted_age_years": round(days_to_years(season_days), 3),
            "season_minute_adjusted_age_display": format_age(season_days),
        })
    summary.sort(key=lambda r: r["season_minute_adjusted_age_days"], reverse=True)
    return summary


def main():
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    SESSION.headers.update({"x-apisports-key": api_key})

    config = load_json(CONFIG_PATH, {})
    config = resolve_league_and_season(config)
    league_id, season = config["league_id"], config["season"]

    players_cache = load_json(PLAYERS_PATH, {})
    players_cache = refresh_squads(league_id, season, players_cache)

    matches = load_json(MATCHES_PATH, [])
    already_done = {(m["fixture_id"], m["team_id"]) for m in matches}

    fixtures_payload = api_get("/fixtures", {"league": league_id, "season": season, "status": "FT"})
    fixtures = fixtures_payload.get("response", [])
    fixtures.sort(key=lambda f: f["fixture"]["date"])

    new_rows_count = 0
    for fixture in fixtures:
        fid = fixture["fixture"]["id"]
        if any(fid == done_fid for done_fid, _ in already_done):
            continue
        rows = process_fixture(fixture, players_cache, season)
        matches.extend(rows)
        new_rows_count += len(rows)
        save_json(PLAYERS_PATH, players_cache)  # persist any fallback lookups promptly
        save_json(MATCHES_PATH, matches)

    summary = recompute_season_summary(matches)
    save_json(SEASON_SUMMARY_PATH, summary)

    print(f"Done. {new_rows_count} new team-match rows added. "
          f"{len(matches)} total. API requests used this run: {REQUEST_COUNT}")


if __name__ == "__main__":
    main()
