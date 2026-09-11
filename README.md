# fantasy-rosters — ZachBarrr's live fantasy football roster tracker

Automatically refreshed rosters for all three of Zach Barrington's 2026 fantasy
football leagues (ESPN "McCarthy Precon", Yahoo "Barringtons and Russes",
Yahoo "Dan B's Death League"), updated every ~2 hours by GitHub Actions.

Data files (public, no login):

- JSON: https://raw.githubusercontent.com/ZachBarrr/fantasy-rosters/main/rosters.json
- Plain text (one line per team): https://raw.githubusercontent.com/ZachBarrr/fantasy-rosters/main/rosters.txt

Each file starts with `updated_at` (UTC). Add `?t=<any number>` to the URL to
skip caches. See `SETUP.md` for how it works and how to fix expired cookies.
