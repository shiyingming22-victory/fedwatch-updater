#!/usr/bin/env python
"""Fetch CME FedWatch-style probabilities and write fedwatch.json.

Sources (all official/free):
  - CME 30-Day Fed Funds futures settlements (product 305)
  - FRED EFFR (effective federal funds rate)

Runs on GitHub Actions (overseas), so the CME host is reachable.
On failure the previous fedwatch.json is kept (no breakage).
"""
import calendar
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

CME_URL = ('https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements'
           '/305/FUT')
FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv'

FOMC_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
]

MONTH_NAMES = {1: 'JAN', 2: 'FEB', 3: 'MAR', 4: 'APR', 5: 'MAY', 6: 'JUN',
               7: 'JUL', 8: 'AUG', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC'}

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')


def recent_business_day(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fred_series(session, series_id):
    end = date.today()
    start = end - timedelta(days=10)
    r = None
    for attempt in range(3):
        try:
            r = session.get(FRED_URL, params={'id': series_id,
                                              'cosd': start.isoformat(),
                                              'coed': end.isoformat()},
                            timeout=60)
            r.raise_for_status()
            break
        except Exception:
            time.sleep(2 + 2 * attempt)
    if r is None:
        raise RuntimeError(f'FRED {series_id} unreachable')
    for line in reversed(r.text.strip().splitlines()[1:]):
        parts = line.split(',')
        if len(parts) == 2 and parts[1] not in ('.', ''):
            return float(parts[1])
    raise ValueError(f'no {series_id} value')


def month_key(y, m):
    return f'{MONTH_NAMES[m]} {y % 100}'


def days_in_month(d):
    return calendar.monthrange(d.year, d.month)[1]


def expected_moves(settle_map, meeting, effr):
    key = month_key(meeting.year, meeting.month)
    if key not in settle_map:
        return None
    implied = 100.0 - settle_map[key]
    py, pm = (meeting.year - 1, 12) if meeting.month == 1 else (meeting.year, meeting.month - 1)
    prev_key = month_key(py, pm)
    pre_rate = (100.0 - settle_map[prev_key]) if prev_key in settle_map else effr
    d, D = meeting.day, days_in_month(meeting)
    n_post = D - d + 1
    if n_post <= 3:
        ny, nm = (meeting.year + 1, 1) if meeting.month == 12 else (meeting.year, meeting.month + 1)
        nxt_key = month_key(ny, nm)
        if nxt_key in settle_map:
            post_rate = 100.0 - settle_map[nxt_key]
            return (post_rate - pre_rate) / 0.25
    n_pre = d - 1
    post_rate = (implied * D - pre_rate * n_pre) / n_post if n_post > 0 else implied
    return (post_rate - pre_rate) / 0.25


def moves_to_probs(expected, effr):
    pre_lower = int(effr * 100 // 25) * 25
    floor_m = int(expected // 1) if expected >= 0 else int(expected) - 1
    p_ceil = max(0.0, min(1.0, expected - floor_m))
    p_floor = 1.0 - p_ceil
    out = {}
    if p_floor > 0.001:
        out[f'{pre_lower + floor_m * 25}-{pre_lower + floor_m * 25 + 25}'] = round(p_floor * 100, 1)
    if p_ceil > 0.001:
        out[f'{pre_lower + (floor_m + 1) * 25}-{pre_lower + (floor_m + 1) * 25 + 25}'] = round(p_ceil * 100, 1)
    return out


def main():
    out = Path('fedwatch.json')
    session = requests.Session()
    session.headers.update({'User-Agent': UA})
    try:
        effr = fred_series(session, 'EFFR')
        trade = recent_business_day(date.today() - timedelta(days=1))
        data = None
        for _ in range(4):
            try:
                r = session.get(CME_URL,
                                params={'tradeDate': trade.strftime('%m/%d/%Y')},
                                timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    if not data.get('empty'):
                        break
            except Exception as e:
                print(f'[warn] CME attempt failed: {type(e).__name__}')
            time.sleep(2)
            trade = recent_business_day(trade - timedelta(days=1))
        if not data or not data.get('settlements'):
            raise RuntimeError('CME settlements unavailable')
        settle_map = {}
        for s in data['settlements']:
            try:
                settle_map[s['month']] = float(s['settle'])
            except (KeyError, ValueError, TypeError):
                continue
        today = date.today()
        meeting = min((m for m in FOMC_2026 if m >= today), default=None)
        if meeting is None:
            raise RuntimeError('no upcoming FOMC in schedule')
        moves = expected_moves(settle_map, meeting, effr)
        probs = moves_to_probs(moves, effr) if moves is not None else {}
        high = bool(probs) and max(probs.values()) >= 80.0
        payload = {
            'meeting': meeting.isoformat(),
            'effr': effr,
            'trade_date': trade.isoformat(),
            'probabilities': probs,
            'consensus_high': high,
            'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': 'cme_settlements+fred',
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print('fedwatch updated:', json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        print(f'[warn] fedwatch fetch failed ({type(e).__name__}: {e}); keeping previous file')
        if not out.exists():
            out.write_text(json.dumps(
                {'status': 'error', 'meeting': None, 'probabilities': {},
                 'consensus_high': False,
                 'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                 'source': 'error'}, ensure_ascii=False, indent=2),
                encoding='utf-8')
        sys.exit(0)


if __name__ == '__main__':
    main()
