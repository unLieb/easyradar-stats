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
    return conn


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
            q = quadrant(bearing(SITE_LAT, SITE_LON, lat, lon))
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

        alt = a.get('alt_baro')
        alt = alt if isinstance(alt, (int, float)) else None
        overflight_alt = alt if (alt is not None and alt >= MIN_OVERFLIGHT_ALT_FT) else None
        speed = a.get('gs')
        speed = speed if isinstance(speed, (int, float)) else None
        type_ = a.get('t') or (a.get('desc') or '').strip() or None
        callsign = (a.get('flight') or '').strip() or None

        row = conn.execute(
            'SELECT max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft FROM sightings WHERE hex=? AND seen_date=?',
            (hex_, today)
        ).fetchone()

        if row is None:
            conn.execute(
                'INSERT INTO sightings (hex, seen_date, type, callsign, max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft, first_seen_ts) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (hex_, today, type_, callsign, alt, dist, speed, overflight_alt, now_ts)
            )
        else:
            new_alt = max((v for v in (row[0], alt) if v is not None), default=None)
            new_dist = max((v for v in (row[1], dist) if v is not None), default=None)
            new_speed = max((v for v in (row[2], speed) if v is not None), default=None)
            new_min_alt = min((v for v in (row[3], overflight_alt) if v is not None), default=None)
            conn.execute(
                'UPDATE sightings SET max_alt_ft=?, max_dist_km=?, max_speed_kt=?, min_alt_ft=?, '
                'type=COALESCE(?, type), callsign=COALESCE(?, callsign) WHERE hex=? AND seen_date=?',
                (new_alt, new_dist, new_speed, new_min_alt, type_, callsign, hex_, today)
            )
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

    top_types = conn.execute(
        f'SELECT type, COUNT(*) c FROM sightings WHERE {where} AND type IS NOT NULL '
        f'GROUP BY type ORDER BY c DESC LIMIT 5', params
    ).fetchall()
    top_calls = conn.execute(
        f'SELECT callsign, COUNT(*) c FROM sightings WHERE {where} AND callsign IS NOT NULL '
        f'GROUP BY callsign ORDER BY c DESC LIMIT 5', params
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

    conn.close()
    return {
        'uniqueCount': unique_count,
        'topTypes': [[t, c] for t, c in top_types],
        'topCallsigns': [[cs, c] for cs, c in top_calls],
        'maxAlt': {'value': max_alt_row[2], 'callsign': max_alt_row[1] or max_alt_row[0]} if max_alt_row else None,
        'maxDist': {'value': max_dist_row[2], 'callsign': max_dist_row[1] or max_dist_row[0]} if max_dist_row else None,
        'maxSpeed': {'value': max_speed_row[2], 'callsign': max_speed_row[1] or max_speed_row[0]} if max_speed_row else None,
        'minAlt': {'value': min_alt_row[2], 'callsign': min_alt_row[1] or min_alt_row[0]} if min_alt_row else None,
        'avgDist': avg_row[0] if avg_row else None,
        'avgAlt': avg_row[1] if avg_row else None,
        'messagesToday': messages_today,
        'directions': directions,
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
        else:
            self.send_response(404)
            self.end_headers()


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', 8090), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()
