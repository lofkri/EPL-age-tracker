#!/usr/bin/env python3
"""
One-off historical backfill: computes minute-adjusted age (and the naive
starting-XI simple average, for comparison) for every match of a single
COMPLETED Premier League season, and writes results to
data/seasons/<season>-<season+1>/.

This is separate from fetch_and_compute.py (the live weekly pipeline for the
current season) so a backfill run can never disturb the live data.

Usage: python3 backfill_season.py <season_start_year>
  e.g. python3 backfill_season.py 2025      (the 2025-26 season)

Requires the API_FOOTBALL_KEY environment variable. Meant to be run via the
"Backfill a past season" GitHub Actions workflow (workflow_dispatch with a
`season` input) - this is a manual, one-time-per-season job, not scheduled.
"""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

API_BASE = "https://v3.football.api-sports.io"
LEAGUE_ID = 39  # Premier League - same id the live pipeline resolved

SESSION = requests.Session()
REQUEST_COUNT = 0


def api_get(path, params=None, retries=4):
    global REQUEST_COUNT
    url = f"{API_BASE}{path}"
    for attempt in range(retries):
        REQUEST_COUNT += 1
        resp = SESSION.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("errors"):
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


def age_in_days(birth_date_str, on_date_str):
    return (date.fromisoformat(on_date_str) - date.fromisoformat(birth_date_str)).days


def days_to_years(days):
    return days / 365.25


def format_age(days):
    years = int(days // 365)
    remainder = int(days - years * 365)
    return f"{years}y {remainder}d"


def fetch_team_roster(team_id, team_name, season, players_cache):
    count = 0
    page = 1
    while True:
        payload = api_get("/players", {"team": team_id, "season": season, "page": page})
        for entry in payload.get("response", []):
            p = entry["player"]
            birth_date = (p.get("birth") or {}).get("date")
            if not birth_date:
                continue
            players_cache[str(p["id"])] = {
                "name": p["name"], "birth_date": birth_date,
                "team_id": team_id, "team_name": team_name,
            }
            count += 1
        paging = payload.get("paging", {})
        if page >= paging.get("total", 1):
            break
        page += 1
    return count


def fetch_missing_player(player_id, players_cache, season):
    for try_season in (season, season - 1, season + 1, season - 2):
        payload = api_get("/players", {"id": player_id, "season": try_season})
        resp = payload.get("response", [])
        if not resp:
            continue
        p = resp[0]["player"]
        birth_date = (p.get("birth") or {}).get("date")
        if not birth_date:
            continue
        info = {"name": p["name"], "birth_date": birth_date, "team_id": None, "team_name": None}
        players_cache[str(player_id)] = info
        return info
    return None


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
                continue

            days_old = age_in_days(info["birth_date"], match_date)

            if minutes and minutes > 0:
                weighted_day_minutes_sum += days_old * minutes
                total_minutes += minutes
            if is_starter:
                starter_ages_days.append(days_old)

        if total_minutes == 0 or not starter_ages_days:
            print(f"WARNING: no usable minutes/starters for {team_name} in fixture {fixture_id}")
            continue

        minute_adjusted_days = weighted_day_minutes_sum / total_minutes
        simple_avg_days = sum(starter_ages_days) / len(starter_ages_days)

        rows.append({
            "fixture_id": fixture_id, "date": match_date, "round": round_name,
            "team_id": team_id, "team": team_name, "opponent": opponent_name,
            "home_away": "home" if is_home else "away", "total_minutes": total_minutes,
            "minute_adjusted_age_days": round(minute_adjusted_days, 2),
            "minute_adjusted_age_years": round(days_to_years(minute_adjusted_days), 3),
            "minute_adjusted_age_display": format_age(minute_adjusted_days),
            "starting_xi_simple_avg_age_days": round(simple_avg_days, 2),
            "starting_xi_simple_avg_age_years": round(days_to_years(simple_avg_days), 3),
        })
    return rows


def recompute_season_summary(matches):
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
            "team": t["team"], "matches_played": t["matches_played"],
            "season_minute_adjusted_age_days": round(season_days, 2),
            "season_minute_adjusted_age_years": round(days_to_years(season_days), 3),
            "season_minute_adjusted_age_display": format_age(season_days),
        })
    summary.sort(key=lambda r: -r["season_minute_adjusted_age_days"])
    return summary


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 backfill_season.py <season_start_year>", file=sys.stderr)
        sys.exit(1)
    season = int(sys.argv[1])

    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    SESSION.headers.update({"x-apisports-key": api_key})

    out_dir = Path(__file__).parent / "data" / "seasons" / f"{season}-{season + 1}"
    out_dir.mkdir(parents=True, exist_ok=True)

    players_cache = {}
    teams_payload = api_get("/teams", {"league": LEAGUE_ID, "season": season})
    teams = [t["team"] for t in teams_payload.get("response", [])]
    if not teams:
        print(f"ERROR: no teams found for season {season} - your plan may not cover this season.",
              file=sys.stderr)
        sys.exit(1)
    print(f"{len(teams)} teams found for {season}-{season + 1}. Fetching squads...")
    for team in teams:
        n = fetch_team_roster(team["id"], team["name"], season, players_cache)
        print(f"  {team['name']}: {n} players")

    fixtures_payload = api_get("/fixtures", {"league": LEAGUE_ID, "season": season, "status": "FT"})
    fixtures = fixtures_payload.get("response", [])
    fixtures.sort(key=lambda f: f["fixture"]["date"])
    print(f"{len(fixtures)} finished fixtures found for {season}-{season + 1}.")
    if not fixtures:
        print("ERROR: 0 finished fixtures - stopping without writing output.", file=sys.stderr)
        sys.exit(1)

    matches = []
    for i, fixture in enumerate(fixtures):
        rows = process_fixture(fixture, players_cache, season)
        matches.extend(rows)
        if (i + 1) % 20 == 0:
            print(f"  processed {i + 1}/{len(fixtures)} fixtures ({REQUEST_COUNT} requests so far)")
        time.sleep(0.25)  # gentle on rate limits across a big backfill

    summary = recompute_season_summary(matches)

    (out_dir / "players.json").write_text(
        json.dumps(players_cache, indent=2, sort_keys=True, default=str))
    (out_dir / "matches.json").write_text(
        json.dumps(matches, indent=2, sort_keys=True, default=str))
    (out_dir / "season_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str))

    print(f"Done. {len(matches)} team-match rows across {len(fixtures)} fixtures written to "
          f"{out_dir}. Total API requests used: {REQUEST_COUNT}")


if __name__ == "__main__":
    main()
