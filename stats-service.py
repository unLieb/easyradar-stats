import json
import math
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SITE_LAT, SITE_LON = 52.52319, 13.35822
DB_PATH = '/data/stats.db'
POLL_URL = 'http://ultrafeeder/data/aircraft.json'
STATS_URL = 'http://ultrafeeder/data/stats.json'
POLL_INTERVAL = 5
MIN_OVERFLIGHT_ALT_FT = 100
RANGE_BUCKET_DEG = 5
RANGE_BUCKETS = 360 // RANGE_BUCKET_DEG
RANGE_DIST_BIN_KM = 5
RANGE_PERCENTILE = 0.95
RANGE_MIN_SAMPLES = 5
# Mirrors the frontend's MIL_TYPES list (index.html) so "military" means the same
# thing in the stats DB as it does on the map/sidebar.
MIL_TYPES = ['F16', 'F15', 'F18', 'F22', 'F35', 'C130', 'C160', 'C17', 'A400', 'KC135', 'KC46', 'KC2', 'E3', 'P8', 'P3',
             'AH64', 'CH47', 'UH1', 'H47', 'H60', 'H64', 'TOR', 'EUFI', 'RFAL', 'MIRA', 'TIGR', 'NH90', 'U2']
DAILY_1000_THRESHOLD = 1000
RANGE_400KM_THRESHOLD_KM = 400


def matches_mil_type(t, p):
    # A plain prefix match lets a short military code like "C17" (C-17 Globemaster)
    # wrongly match unrelated civilian types that extend it with more digits, e.g.
    # "C172" (Cessna 172). Military variant suffixes are letters (e.g. "UH1H"),
    # never digits, so only count it as a match when the next character isn't one.
    if not t.startswith(p):
        return False
    if len(t) == len(p):
        return True
    return not t[len(p)].isdigit()


def is_military(a):
    db_flags = a.get('dbFlags')
    if db_flags and (db_flags & 1):
        return True
    t = (a.get('t') or '').upper()
    return any(matches_mil_type(t, p) for p in MIL_TYPES)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dLon = math.radians(lon2 - lon1)
    y = math.sin(dLon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dLon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def quadrant(brg):
    if brg >= 315 or brg < 45:
        return 'N'
    if brg < 135:
        return 'E'
    if brg < 225:
        return 'S'
    return 'W'


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS sightings (
        hex TEXT NOT NULL,
        seen_date TEXT NOT NULL,
        type TEXT,
        callsign TEXT,
        max_alt_ft REAL,
        max_dist_km REAL,
        first_seen_ts INTEGER,
        PRIMARY KEY (hex, seen_date)
    )''')
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(sightings)')}
    if 'max_speed_kt' not in existing_cols:
        conn.execute('ALTER TABLE sightings ADD COLUMN max_speed_kt REAL')
    if 'min_alt_ft' not in existing_cols:
        conn.execute('ALTER TABLE sightings ADD COLUMN min_alt_ft REAL')
    if 'is_military' not in existing_cols:
        conn.execute('ALTER TABLE sightings ADD COLUMN is_military INTEGER DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS direction_stats (
        seen_date TEXT NOT NULL,
        quadrant TEXT NOT NULL,
        max_dist_km REAL,
        PRIMARY KEY (seen_date, quadrant)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_messages (
        seen_date TEXT PRIMARY KEY,
        start_total INTEGER,
        latest_total INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS range_stats (
        seen_date TEXT NOT NULL,
        bucket INTEGER NOT NULL,
        max_dist_km REAL,
        PRIMARY KEY (seen_date, bucket)
    )''')
    # Distance histogram per bearing bucket (5km bins), replacing the plain running
    # max above for the map contour: a single far-off outlier detection shouldn't
    # spike the whole contour outward. Storing counts per bin keeps this bounded in
    # size (unlike storing every raw sample) while still letting us compute a
    # percentile at query time.
    conn.execute('''CREATE TABLE IF NOT EXISTS range_hist (
        seen_date TEXT NOT NULL,
        bucket INTEGER NOT NULL,
        dist_bin INTEGER NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (seen_date, bucket, dist_bin)
    )''')
    # One-time badge unlocks (each id can only ever be inserted once).
    conn.execute('''CREATE TABLE IF NOT EXISTS achievements (
        id TEXT PRIMARY KEY,
        unlocked_at INTEGER,
        hex TEXT,
        callsign TEXT
    )''')
    # Current standing personal-best per category; overwritten (not appended) each
    # time it's beaten, so the frontend can show "current record + when it was set".
    conn.execute('''CREATE TABLE IF NOT EXISTS records (
        category TEXT PRIMARY KEY,
        value REAL,
        hex TEXT,
        callsign TEXT,
        broken_at INTEGER
    )''')
    return conn


def unlock_achievement(conn, achievement_id, hex_, callsign, now_ts):
    conn.execute(
        'INSERT OR IGNORE INTO achievements (id, unlocked_at, hex, callsign) VALUES (?,?,?,?)',
        (achievement_id, now_ts, hex_, callsign)
    )


def maybe_break_record(conn, category, value, hex_, callsign, now_ts, higher_is_better):
    if value is None:
        return
    row = conn.execute('SELECT value FROM records WHERE category=?', (category,)).fetchone()
    is_new_record = row is None or (value > row[0] if higher_is_better else value < row[0])
    if is_new_record:
        conn.execute(
            'INSERT INTO records (category, value, hex, callsign, broken_at) VALUES (?,?,?,?,?) '
            'ON CONFLICT(category) DO UPDATE SET value=excluded.value, hex=excluded.hex, '
            'callsign=excluded.callsign, broken_at=excluded.broken_at',
            (category, value, hex_, callsign, now_ts)
        )


def poll_once():
    with urllib.request.urlopen(POLL_URL, timeout=4) as r:
        data = json.load(r)
    today = datetime.now().strftime('%Y-%m-%d')
    now_ts = int(time.time())
    conn = get_db()
    for a in data.get('aircraft', []):
        hex_ = a.get('hex')
        if not hex_:
            continue
        lat, lon = a.get('lat'), a.get('lon')
        dist = haversine(SITE_LAT, SITE_LON, lat, lon) if lat is not None and lon is not None else None

        if dist is not None:
            brg = bearing(SITE_LAT, SITE_LON, lat, lon)
            q = quadrant(brg)
            drow = conn.execute(
                'SELECT max_dist_km FROM direction_stats WHERE seen_date=? AND quadrant=?', (today, q)
            ).fetchone()
            if drow is None:
                conn.execute(
                    'INSERT INTO direction_stats (seen_date, quadrant, max_dist_km) VALUES (?,?,?)',
                    (today, q, dist)
                )
            elif drow[0] is None or dist > drow[0]:
                conn.execute(
                    'UPDATE direction_stats SET max_dist_km=? WHERE seen_date=? AND quadrant=?',
                    (dist, today, q)
                )

            bucket = int(brg // RANGE_BUCKET_DEG) % RANGE_BUCKETS
            dist_bin = int(dist // RANGE_DIST_BIN_KM)
            conn.execute(
                'INSERT INTO range_hist (seen_date, bucket, dist_bin, count) VALUES (?,?,?,1) '
                'ON CONFLICT(seen_date, bucket, dist_bin) DO UPDATE SET count = count + 1',
                (today, bucket, dist_bin)
            )

        alt = a.get('alt_baro')
        alt = alt if isinstance(alt, (int, float)) else None
        overflight_alt = alt if (alt is not None and alt >= MIN_OVERFLIGHT_ALT_FT) else None
        speed = a.get('gs')
        speed = speed if isinstance(speed, (int, float)) else None
        type_ = a.get('t') or (a.get('desc') or '').strip() or None
        callsign = (a.get('flight') or '').strip() or None
        mil = 1 if is_military(a) else 0
        cat = a.get('category') or ''
        t_upper = (a.get('t') or '').upper()

        if t_upper.startswith('A38'):
            unlock_achievement(conn, 'first_a380', hex_, callsign, now_ts)
        if t_upper == 'CONC':
            unlock_achievement(conn, 'first_concorde', hex_, callsign, now_ts)
        if cat == 'A7':
            unlock_achievement(conn, 'first_helicopter', hex_, callsign, now_ts)
        if mil:
            unlock_achievement(conn, 'first_military', hex_, callsign, now_ts)
        if dist is not None and dist >= RANGE_400KM_THRESHOLD_KM:
            unlock_achievement(conn, 'range_400km', hex_, callsign, now_ts)

        maybe_break_record(conn, 'max_alt', alt, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'max_dist', dist, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'max_speed', speed, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'min_alt', overflight_alt, hex_, callsign, now_ts, higher_is_better=False)

        row = conn.execute(
            'SELECT max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft, is_military FROM sightings WHERE hex=? AND seen_date=?',
            (hex_, today)
        ).fetchone()

        if row is None:
            conn.execute(
                'INSERT INTO sightings (hex, seen_date, type, callsign, max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft, is_military, first_seen_ts) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (hex_, today, type_, callsign, alt, dist, speed, overflight_alt, mil, now_ts)
            )
        else:
            new_alt = max((v for v in (row[0], alt) if v is not None), default=None)
            new_dist = max((v for v in (row[1], dist) if v is not None), default=None)
            new_speed = max((v for v in (row[2], speed) if v is not None), default=None)
            new_min_alt = min((v for v in (row[3], overflight_alt) if v is not None), default=None)
            new_mil = 1 if (row[4] or mil) else 0
            conn.execute(
                'UPDATE sightings SET max_alt_ft=?, max_dist_km=?, max_speed_kt=?, min_alt_ft=?, is_military=?, '
                'type=COALESCE(?, type), callsign=COALESCE(?, callsign) WHERE hex=? AND seen_date=?',
                (new_alt, new_dist, new_speed, new_min_alt, new_mil, type_, callsign, hex_, today)
            )

    today_count = conn.execute(
        'SELECT COUNT(DISTINCT hex) FROM sightings WHERE seen_date=?', (today,)
    ).fetchone()[0]
    if today_count >= DAILY_1000_THRESHOLD:
        unlock_achievement(conn, 'daily_1000', None, None, now_ts)

    conn.commit()
    conn.close()


def poll_stats_once():
    with urllib.request.urlopen(STATS_URL, timeout=4) as r:
        data = json.load(r)
    total_msgs = data.get('total', {}).get('messages')
    if total_msgs is None:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute('SELECT start_total FROM daily_messages WHERE seen_date=?', (today,)).fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO daily_messages (seen_date, start_total, latest_total) VALUES (?,?,?)',
            (today, total_msgs, total_msgs)
        )
    elif total_msgs < row[0]:
        # readsb counter reset (container restart) - rebase today's baseline
        conn.execute(
            'UPDATE daily_messages SET start_total=?, latest_total=? WHERE seen_date=?',
            (total_msgs, total_msgs, today)
        )
    else:
        conn.execute('UPDATE daily_messages SET latest_total=? WHERE seen_date=?', (total_msgs, today))
    conn.commit()
    conn.close()


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            print('poll error:', e, flush=True)
        try:
            poll_stats_once()
        except Exception as e:
            print('stats poll error:', e, flush=True)
        time.sleep(POLL_INTERVAL)


def get_summary(range_):
    conn = get_db()
    if range_ == 'today':
        today = datetime.now().strftime('%Y-%m-%d')
        where = 'seen_date = ?'
        params = (today,)
    else:
        where = '1=1'
        params = ()

    unique_count = conn.execute(f'SELECT COUNT(DISTINCT hex) FROM sightings WHERE {where}', params).fetchone()[0]
    military_count = conn.execute(
        f'SELECT COUNT(DISTINCT hex) FROM sightings WHERE {where} AND is_military=1', params
    ).fetchone()[0]

    top_types = conn.execute(
        f'SELECT type, COUNT(*) c FROM sightings WHERE {where} AND type IS NOT NULL '
        f'GROUP BY type ORDER BY c DESC LIMIT 5', params
    ).fetchall()
    top_calls = conn.execute(
        f'SELECT callsign, COUNT(*) c FROM sightings WHERE {where} AND callsign IS NOT NULL '
        f'GROUP BY callsign ORDER BY c DESC LIMIT 5', params
    ).fetchall()
    # Airline ICAO designator = first 3 letters of the callsign (e.g. DLH441 -> DLH).
    # The GLOB filter excludes bare registrations used as callsign by GA traffic,
    # which don't follow the "3 letters + digits" airline flight-number pattern.
    top_airlines = conn.execute(
        f"SELECT SUBSTR(callsign,1,3) code, COUNT(*) c FROM sightings WHERE {where} "
        f"AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*' GROUP BY code ORDER BY c DESC LIMIT 5", params
    ).fetchall()
    max_alt_row = conn.execute(
        f'SELECT hex, callsign, max_alt_ft FROM sightings WHERE {where} AND max_alt_ft IS NOT NULL '
        f'ORDER BY max_alt_ft DESC LIMIT 1', params
    ).fetchone()
    max_dist_row = conn.execute(
        f'SELECT hex, callsign, max_dist_km FROM sightings WHERE {where} AND max_dist_km IS NOT NULL '
        f'ORDER BY max_dist_km DESC LIMIT 1', params
    ).fetchone()
    max_speed_row = conn.execute(
        f'SELECT hex, callsign, max_speed_kt FROM sightings WHERE {where} AND max_speed_kt IS NOT NULL '
        f'ORDER BY max_speed_kt DESC LIMIT 1', params
    ).fetchone()
    min_alt_row = conn.execute(
        f'SELECT hex, callsign, min_alt_ft FROM sightings WHERE {where} AND min_alt_ft IS NOT NULL '
        f'ORDER BY min_alt_ft ASC LIMIT 1', params
    ).fetchone()
    avg_row = conn.execute(
        f'SELECT AVG(max_dist_km), AVG(max_alt_ft) FROM sightings WHERE {where}', params
    ).fetchone()

    messages_today = None
    if range_ == 'today':
        today = datetime.now().strftime('%Y-%m-%d')
        mrow = conn.execute(
            'SELECT start_total, latest_total FROM daily_messages WHERE seen_date=?', (today,)
        ).fetchone()
        if mrow:
            messages_today = mrow[1] - mrow[0]

    dir_where = 'seen_date = ?' if range_ == 'today' else '1=1'
    dir_rows = conn.execute(
        f'SELECT quadrant, MAX(max_dist_km) FROM direction_stats WHERE {dir_where} GROUP BY quadrant', params
    ).fetchall()
    directions = {'N': None, 'E': None, 'S': None, 'W': None}
    for q, d in dir_rows:
        directions[q] = d

    range_where = 'seen_date = ?' if range_ == 'today' else '1=1'
    hist_rows = conn.execute(
        f'SELECT bucket, dist_bin, SUM(count) FROM range_hist WHERE {range_where} '
        f'GROUP BY bucket, dist_bin ORDER BY bucket, dist_bin', params
    ).fetchall()
    hist_by_bucket = {}
    for bucket, dist_bin, cnt in hist_rows:
        hist_by_bucket.setdefault(bucket, []).append((dist_bin, cnt))
    range_outline = {}
    for bucket, bins in hist_by_bucket.items():
        total = sum(c for _, c in bins)
        if total < RANGE_MIN_SAMPLES:
            continue
        target = RANGE_PERCENTILE * total
        cum = 0
        for dist_bin, count in bins:
            cum += count
            if cum >= target:
                range_outline[str(bucket)] = (dist_bin + 0.5) * RANGE_DIST_BIN_KM
                break

    conn.close()
    return {
        'uniqueCount': unique_count,
        'topTypes': [[t, c] for t, c in top_types],
        'topCallsigns': [[cs, c] for cs, c in top_calls],
        'topAirlineCodes': [[code, c] for code, c in top_airlines],
        'militaryCount': military_count,
        'maxAlt': {'value': max_alt_row[2], 'callsign': max_alt_row[1] or max_alt_row[0]} if max_alt_row else None,
        'maxDist': {'value': max_dist_row[2], 'callsign': max_dist_row[1] or max_dist_row[0]} if max_dist_row else None,
        'maxSpeed': {'value': max_speed_row[2], 'callsign': max_speed_row[1] or max_speed_row[0]} if max_speed_row else None,
        'minAlt': {'value': min_alt_row[2], 'callsign': min_alt_row[1] or min_alt_row[0]} if min_alt_row else None,
        'avgDist': avg_row[0] if avg_row else None,
        'avgAlt': avg_row[1] if avg_row else None,
        'messagesToday': messages_today,
        'directions': directions,
        'rangeOutline': range_outline,
        'rangeBucketDeg': RANGE_BUCKET_DEG,
    }


def get_achievements():
    conn = get_db()
    unlocked = conn.execute('SELECT id, unlocked_at, hex, callsign FROM achievements').fetchall()
    records = conn.execute('SELECT category, value, hex, callsign, broken_at FROM records').fetchall()
    conn.close()
    return {
        'unlocked': [
            {'id': i, 'unlockedAt': ts, 'hex': hex_, 'callsign': cs} for i, ts, hex_, cs in unlocked
        ],
        'records': {
            cat: {'value': val, 'hex': hex_, 'callsign': cs, 'brokenAt': ts}
            for cat, val, hex_, cs, ts in records
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/summary':
            qs = parse_qs(parsed.query)
            range_ = qs.get('range', ['today'])[0]
            if range_ not in ('today', 'alltime'):
                range_ = 'today'
            try:
                summary = get_summary(range_)
                body = json.dumps(summary).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                print('summary error:', e, flush=True)
                self.send_response(500)
                self.end_headers()
        elif parsed.path == '/api/achievements':
            try:
                body = json.dumps(get_achievements()).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                print('achievements error:', e, flush=True)
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', 8090), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()
