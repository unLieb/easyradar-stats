import json
import math
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Placeholder default (Berlin city center) - set SITE_LAT/SITE_LON via the
# environment (see docker-compose.yml) so real receiver coordinates never
# need to live in this file.
SITE_LAT = float(os.environ.get('SITE_LAT', '52.5200'))
SITE_LON = float(os.environ.get('SITE_LON', '13.4050'))
STATION_NAME = os.environ.get('STATION_NAME', '')
DB_PATH = '/data/stats.db'
POLL_URL = 'http://ultrafeeder/data/aircraft.json'
STATS_URL = 'http://ultrafeeder/data/stats.json'
POLL_INTERVAL = 5
MIN_OVERFLIGHT_ALT_FT = 100
# Generously above anything a real aircraft could plausibly do (fastest jets in a
# dive are still well under 1500kt) - a value above this is a corrupted airborne
# velocity decode, not a genuine speed, and would otherwise get recorded as a
# "record" and skew the stats permanently.
MAX_PLAUSIBLE_SPEED_KT = 2000
RANGE_BUCKET_DEG = 5
RANGE_BUCKETS = 360 // RANGE_BUCKET_DEG
RANGE_DIST_BIN_KM = 5
RANGE_PERCENTILE = 0.95
RANGE_MIN_SAMPLES = 5

# --- Achievement thresholds/catalogs -----------------------------------------
# Mirrors the frontend's MIL_TYPES list (index.html) so "military" means the same
# thing in the stats DB as it does on the map/sidebar.
MIL_TYPES = ['F16', 'F15', 'F18', 'F22', 'F35', 'C130', 'C160', 'C17', 'A400', 'KC135', 'KC46', 'KC2', 'E3', 'P8', 'P3',
             'AH64', 'CH47', 'UH1', 'H47', 'H60', 'H64', 'TOR', 'EUFI', 'RFAL', 'MIRA', 'TIGR', 'NH90', 'U2']
BUSINESS_TYPES = ['LJ', 'C25', 'C56', 'C68', 'C700', 'C750', 'GLF', 'GL5', 'GLEX', 'FA7', 'F2TH', 'F900',
                   'CL30', 'CL35', 'E50', 'E55', 'EA50', 'PC24', 'H25', 'BE40', 'ASTR']
# CHX is what German air rescue ("Christoph") helicopters actually transmit as
# their ADS-B flight ident (e.g. CHX31) - CHRISTOPH is the spoken radio
# callsign, not what's on the wire, so it alone never matched a real one.
# Bundespolizei (federal police, spoken callsign "Pirol") is BPO+number
# (e.g. BPO441) - confirmed via FlightAware. Each German state's own police
# helicopters (Landespolizei) have their own distinct ICAO prefix, unrelated
# to Bundespolizei's despite Berlin's spoken callsign also being "Pirol":
# https://knowledgebase.vatsim-germany.org/books/heli-ops/page/polizeifliegerei
# Bundeswehr search & rescue (Heer/Marine) transmits RESQ+number (e.g.
# RESQ63), not "RESCUE" - and Northern Helicopter (offshore SAR) is NHC:
# https://knowledgebase.vatsim-germany.org/books/heli-ops/page/luftrettung
EINSATZ_CALLSIGN_PREFIXES = [
    'CHX', 'CHRISTOPH', 'RESQ', 'RESCUE', 'NHC', 'REGA', 'POLIZEI', 'POLICE', 'BPO',
    'PBW', 'EDL', 'PBB', 'LIB', 'IBIS', 'PMV', 'PPH', 'HUMMEL', 'SRP', 'PHS', 'PIK', 'HBT',
]

# (achievement id, list of type-code prefixes that unlock it) - checked with the
# same digit-boundary-safe prefix match used for MIL_TYPES.
TYPE_ACHIEVEMENTS = [
    ('first_a380', ['A388']),  # exact code, not a prefix: "A38" would be blocked by
                                # the digit-boundary check below (A388 ends in a digit)
    # first_concorde and rare_sr71 deliberately removed from this active list (2026-08):
    # no flightworthy example of either exists anywhere in the world anymore, so they
    # were permanently unearnable dead weight for every new user - see the eligibility
    # rule in the ACHIEVEMENT_XP comment below. Their ids/XP entries stay defined further
    # down (never unlocked, so untouched by the "never remove/edit" rule) so a future
    # non-XP "aviation legends" gallery can reuse the same title/description content.
    ('first_business', BUSINESS_TYPES),
    ('first_seaplane', ['PBY', 'CL41', 'CL21']),
    ('first_firefighter', ['CL41', 'CL21', 'AT8']),
    ('first_osprey', ['V22']),
    ('first_chinook', ['CH47']),
    ('rare_a340', ['A340']),
    ('rare_antonov', ['AN12', 'AN24', 'AN26', 'AN28', 'AN30', 'AN32', 'AN72', 'A124', 'A140', 'A148', 'A158', 'A225']),
    ('rare_beluga', ['A3ST']),
    ('rare_dreamlifter', ['B74S']),
    ('rare_vc25', ['VC25']),
    ('rare_e3sentry', ['E3']),
    ('rare_u2', ['U2']),
    ('rare_b2spirit', ['B2']),
    ('rare_b52', ['B52']),
    ('rare_c17', ['C17']),
    ('rare_f35', ['F35']),
    ('rare_eurofighter', ['EUFI']),
    ('rare_fa18', ['F18']),
    ('rare_p8', ['P8']),
    ('rare_kc46', ['KC46']),
    ('rare_kc135', ['KC135']),
    ('rare_e6mercury', ['E6']),
]
# (achievement id, callsign prefixes)
CALLSIGN_ACHIEVEMENTS = [
    ('first_rescue_heli', ['CHX', 'CHRISTOPH', 'RESQ', 'RESCUE', 'NHC', 'REGA']),
    ('rare_nasa', ['NASA']),
]
# (achievement id, ICAO airline designator)
AIRLINE_FIRST_ACHIEVEMENTS = [
    ('first_lufthansa', 'DLH'),
    ('first_ryanair', 'RYR'),
    ('first_emirates', 'UAE'),
    ('first_singapore', 'SIA'),
    ('first_qantas', 'QFA'),
]
AIRLINE_CODE_TO_ACHIEVEMENT = {code: ach_id for ach_id, code in AIRLINE_FIRST_ACHIEVEMENTS}
AIRLINE_COUNT_THRESHOLDS = [100, 250, 500]
RANGE_THRESHOLDS_KM = [100, 150, 200, 250, 300, 400]
DAILY_COUNT_THRESHOLDS = [100, 500, 1000, 2500, 5000]
MSG_THRESHOLDS = [100000, 1000000, 10000000, 100000000]
ALT_THRESHOLDS_FT = [30000, 40000, 45000]
LOWALT_THRESHOLDS_M = [1000, 500, 250, 100]
COUNTRY_FIRST_ACHIEVEMENTS = {
    'de': 'country_de', 'us': 'country_us', 'gb': 'country_gb', 'fr': 'country_fr',
    'it': 'country_it', 'es': 'country_es', 'nl': 'country_nl', 'pl': 'country_pl',
    'ch': 'country_ch', 'at': 'country_at', 'ru': 'country_ru', 'cn': 'country_cn',
    'jp': 'country_jp', 'ae': 'country_ae', 'tr': 'country_tr',
}
COUNTRY_COUNT_THRESHOLDS = [50]
ANNIVERSARY_DAYS = [1, 7, 30, 100, 365]
NIGHT_OWL_THRESHOLD = 100
TYPE_COUNT_THRESHOLDS = [10, 25, 50, 100]
AIRCRAFT_COUNT_THRESHOLDS = [100, 500, 1000, 2500, 5000, 10000]

# The individual "rare aircraft" ids (excluding the retired first_concorde/rare_sr71,
# see TYPE_ACHIEVEMENTS above) that count toward the collection-progress milestones
# below. Region-fair by design: it doesn't matter *which* of these a station finds,
# only how many - a Berlin station's Eurofighter/Beluga finds and a Nevada station's
# B-2/F-22 finds both count equally toward the same milestone.
RARE_AIRCRAFT_IDS = [
    'first_a380', 'rare_a340', 'first_osprey', 'first_chinook', 'rare_antonov',
    'rare_beluga', 'rare_dreamlifter', 'rare_vc25', 'rare_e3sentry', 'rare_u2',
    'rare_b2spirit', 'rare_b52', 'rare_c17', 'rare_f35', 'rare_eurofighter',
    'rare_fa18', 'rare_p8', 'rare_kc46', 'rare_kc135', 'rare_e6mercury', 'rare_nasa',
]
RARE_COLLECTION_THRESHOLDS = [1, 5, 10, 20]

# XP value per achievement id, for the Radar-Level system. FROZEN once shipped -
# existing values must never change (would silently shift already-earned levels
# for existing users). New achievements only ever get appended with their own
# value, never by editing an existing one. Rule of thumb applied when balancing:
# no single achievement exceeds ~10% of total possible XP (this is why Concorde/
# SR-71 are 800/1200 rather than the "feels right in isolation" 3000/5000 -
# at 5000 alone they'd have been 1/3 of the entire game's XP).
ACHIEVEMENT_XP = {
    # Flugzeuge
    'first_helicopter': 20, 'first_military': 40, 'first_business': 20,
    'first_seaplane': 60, 'first_rescue_heli': 25, 'first_firefighter': 50,
    # Airlines
    'first_lufthansa': 15, 'first_ryanair': 15, 'first_emirates': 25,
    'first_singapore': 30, 'first_qantas': 40,
    'airlines_100': 100, 'airlines_250': 250, 'airlines_500': 500,
    # Empfang
    'range_100km': 20, 'range_150km': 35, 'range_200km': 55,
    'range_250km': 80, 'range_300km': 110, 'range_400km': 150,
    # Tagesrekorde
    'daily_100': 20, 'daily_500': 40, 'daily_1000': 80, 'daily_2500': 150, 'daily_5000': 300,
    # Nachrichten
    'msg_100000': 20, 'msg_1000000': 60, 'msg_10000000': 150, 'msg_100000000': 400,
    # Flughoehe
    'altitude_fl300': 20, 'altitude_fl400': 35, 'altitude_fl450': 60,
    # Niedrigueberflug
    'lowalt_1000m': 20, 'lowalt_500m': 35, 'lowalt_250m': 55, 'lowalt_100m': 90,
    # Laender
    'country_de': 15, 'country_at': 20, 'country_ch': 20, 'country_nl': 20,
    'country_gb': 25, 'country_fr': 25, 'country_it': 25, 'country_pl': 25,
    'country_es': 30, 'country_tr': 40, 'country_ru': 50, 'country_ae': 60,
    'country_us': 70, 'country_cn': 80, 'country_jp': 90, 'countries_50': 300,
    # Zeit
    'night_watchman': 60, 'early_bird': 50, 'night_owl': 100,
    # Jubilaeen
    'anniversary_1': 50, 'anniversary_7': 100, 'anniversary_30': 250,
    'anniversary_100': 500, 'anniversary_365': 1000,
    # Flugzeugtypen (neu)
    'types_10': 30, 'types_25': 80, 'types_50': 180, 'types_100': 400,
    # Gesamtflugzeuge (neu)
    'aircraft_100': 20, 'aircraft_500': 40, 'aircraft_1000': 80,
    'aircraft_2500': 150, 'aircraft_5000': 300, 'aircraft_10000': 600,
    # Seltene Flugzeuge - first_concorde/rare_sr71 kept here at their original value
    # (never touched, never unlocked - see TYPE_ACHIEVEMENTS) rather than deleted,
    # purely so a future non-XP "legends" gallery can reuse the id/value pairing.
    'first_a380': 200, 'first_concorde': 800, 'rare_a340': 150,
    'first_osprey': 250, 'first_chinook': 100, 'rare_antonov': 200,
    'rare_beluga': 300, 'rare_dreamlifter': 300, 'rare_vc25': 800,
    'rare_e3sentry': 150, 'rare_u2': 400, 'rare_sr71': 1200,
    'rare_b2spirit': 600, 'rare_b52': 200, 'rare_c17': 80, 'rare_f35': 150,
    'rare_eurofighter': 60, 'rare_fa18': 120, 'rare_p8': 150, 'rare_kc46': 150,
    'rare_kc135': 100, 'rare_e6mercury': 300, 'rare_nasa': 250,
    # Seltenheiten-Sammlung (neu, 2026-08): belohnt die Anzahl gefundener seltener
    # Flugzeuge statt einzelner Typen - fair unabhaengig von der Region der Station.
    'rare_collection_1': 50, 'rare_collection_5': 200,
    'rare_collection_10': 450, 'rare_collection_20': 900,
}

LEVEL_MAX = 50
LEVEL_MAX_XP = 12000


def xp_for_level(n):
    if n <= 0:
        return 0
    if n >= LEVEL_MAX:
        return LEVEL_MAX_XP
    return round(LEVEL_MAX_XP * (n / LEVEL_MAX) ** 2)


def compute_level(total_xp):
    level = 0
    for n in range(1, LEVEL_MAX + 1):
        if total_xp >= xp_for_level(n):
            level = n
        else:
            break
    next_level_xp = xp_for_level(level + 1) if level < LEVEL_MAX else None
    return {
        'level': level,
        'maxLevel': LEVEL_MAX,
        'totalXp': total_xp,
        'currentLevelXp': xp_for_level(level),
        'nextLevelXp': next_level_xp,
        'xpToNext': (next_level_xp - total_xp) if next_level_xp is not None else 0,
        'bonusXp': max(0, total_xp - LEVEL_MAX_XP),
    }

# ICAO 24-bit address allocation ranges -> ISO 3166-1 alpha-2 country code.
# Ported from tar1090's flags.js (same source used for the frontend's flag icons).
ICAO_COUNTRY_RANGES = [
    (0x004000, 0x0047FF, 'zw'),
    (0x006000, 0x006FFF, 'mz'),
    (0x008000, 0x00FFFF, 'za'),
    (0x010000, 0x017FFF, 'eg'),
    (0x018000, 0x01FFFF, 'ly'),
    (0x020000, 0x027FFF, 'ma'),
    (0x028000, 0x02FFFF, 'tn'),
    (0x030000, 0x0307FF, 'bw'),
    (0x032000, 0x032FFF, 'bi'),
    (0x034000, 0x034FFF, 'cm'),
    (0x035000, 0x0357FF, 'km'),
    (0x036000, 0x036FFF, 'cg'),
    (0x038000, 0x038FFF, 'ci'),
    (0x03E000, 0x03EFFF, 'ga'),
    (0x040000, 0x040FFF, 'et'),
    (0x042000, 0x042FFF, 'gq'),
    (0x044000, 0x044FFF, 'gh'),
    (0x046000, 0x046FFF, 'gn'),
    (0x048000, 0x0487FF, 'gw'),
    (0x04A000, 0x04A7FF, 'ls'),
    (0x04C000, 0x04CFFF, 'ke'),
    (0x050000, 0x050FFF, 'lr'),
    (0x054000, 0x054FFF, 'mg'),
    (0x058000, 0x058FFF, 'mw'),
    (0x05A000, 0x05A7FF, 'mv'),
    (0x05C000, 0x05CFFF, 'ml'),
    (0x05E000, 0x05E7FF, 'mr'),
    (0x060000, 0x0607FF, 'mu'),
    (0x062000, 0x062FFF, 'ne'),
    (0x064000, 0x064FFF, 'ng'),
    (0x068000, 0x068FFF, 'ug'),
    (0x06A000, 0x06AFFF, 'qa'),
    (0x06C000, 0x06CFFF, 'cf'),
    (0x06E000, 0x06EFFF, 'rw'),
    (0x070000, 0x070FFF, 'sn'),
    (0x074000, 0x0747FF, 'sc'),
    (0x076000, 0x0767FF, 'sl'),
    (0x078000, 0x078FFF, 'so'),
    (0x07A000, 0x07A7FF, 'sz'),
    (0x07C000, 0x07CFFF, 'sd'),
    (0x080000, 0x080FFF, 'tz'),
    (0x084000, 0x084FFF, 'td'),
    (0x088000, 0x088FFF, 'tg'),
    (0x08A000, 0x08AFFF, 'zm'),
    (0x08C000, 0x08CFFF, 'cd'),
    (0x090000, 0x090FFF, 'ao'),
    (0x094000, 0x0947FF, 'bj'),
    (0x096000, 0x0967FF, 'cv'),
    (0x098000, 0x0987FF, 'dj'),
    (0x09A000, 0x09AFFF, 'gm'),
    (0x09C000, 0x09CFFF, 'bf'),
    (0x09E000, 0x09E7FF, 'st'),
    (0x0A0000, 0x0A7FFF, 'dz'),
    (0x0A8000, 0x0A8FFF, 'bs'),
    (0x0AA000, 0x0AA7FF, 'bb'),
    (0x0AB000, 0x0AB7FF, 'bz'),
    (0x0AC000, 0x0ADFFF, 'co'),
    (0x0AE000, 0x0AEFFF, 'cr'),
    (0x0B0000, 0x0B0FFF, 'cu'),
    (0x0B2000, 0x0B2FFF, 'sv'),
    (0x0B4000, 0x0B4FFF, 'gt'),
    (0x0B6000, 0x0B6FFF, 'gy'),
    (0x0B8000, 0x0B8FFF, 'ht'),
    (0x0BA000, 0x0BAFFF, 'hn'),
    (0x0BC000, 0x0BC7FF, 'vc'),
    (0x0BE000, 0x0BEFFF, 'jm'),
    (0x0C0000, 0x0C0FFF, 'ni'),
    (0x0C2000, 0x0C2FFF, 'pa'),
    (0x0C4000, 0x0C4FFF, 'do'),
    (0x0C6000, 0x0C6FFF, 'tt'),
    (0x0C8000, 0x0C8FFF, 'sr'),
    (0x0CA000, 0x0CA7FF, 'ag'),
    (0x0CC000, 0x0CC7FF, 'gd'),
    (0x0D0000, 0x0D7FFF, 'mx'),
    (0x0D8000, 0x0DFFFF, 've'),
    (0x100000, 0x1FFFFF, 'ru'),
    (0x201000, 0x2017FF, 'na'),
    (0x202000, 0x2027FF, 'er'),
    (0x300000, 0x33FFFF, 'it'),
    (0x340000, 0x37FFFF, 'es'),
    (0x380000, 0x3BFFFF, 'fr'),
    (0x3C0000, 0x3FFFFF, 'de'),
    (0x400000, 0x4001BF, 'bm'),
    (0x4001C0, 0x4001FF, 'ky'),
    (0x400300, 0x4003FF, 'tc'),
    (0x424135, 0x4241F2, 'ky'),
    (0x424200, 0x4246FF, 'bm'),
    (0x424700, 0x424899, 'ky'),
    (0x424B00, 0x424BFF, 'im'),
    (0x43BE00, 0x43BEFF, 'bm'),
    (0x43E700, 0x43EAFD, 'im'),
    (0x43EAFE, 0x43EEFF, 'gg'),
    (0x400000, 0x43FFFF, 'gb'),
    (0x440000, 0x447FFF, 'at'),
    (0x448000, 0x44FFFF, 'be'),
    (0x450000, 0x457FFF, 'bg'),
    (0x458000, 0x45FFFF, 'dk'),
    (0x460000, 0x467FFF, 'fi'),
    (0x468000, 0x46FFFF, 'gr'),
    (0x470000, 0x477FFF, 'hu'),
    (0x478000, 0x47FFFF, 'no'),
    (0x480000, 0x487FFF, 'nl'),
    (0x488000, 0x48FFFF, 'pl'),
    (0x490000, 0x497FFF, 'pt'),
    (0x498000, 0x49FFFF, 'cz'),
    (0x4A0000, 0x4A7FFF, 'ro'),
    (0x4A8000, 0x4AFFFF, 'se'),
    (0x4B0000, 0x4B7FFF, 'ch'),
    (0x4B8000, 0x4BFFFF, 'tr'),
    (0x4C0000, 0x4C7FFF, 'rs'),
    (0x4C8000, 0x4C87FF, 'cy'),
    (0x4CA000, 0x4CAFFF, 'ie'),
    (0x4CC000, 0x4CCFFF, 'is'),
    (0x4D0000, 0x4D07FF, 'lu'),
    (0x4D2000, 0x4D27FF, 'mt'),
    (0x4D4000, 0x4D47FF, 'mc'),
    (0x500000, 0x5007FF, 'sm'),
    (0x501000, 0x5017FF, 'al'),
    (0x501800, 0x501FFF, 'hr'),
    (0x502800, 0x502FFF, 'lv'),
    (0x503800, 0x503FFF, 'lt'),
    (0x504800, 0x504FFF, 'md'),
    (0x505800, 0x505FFF, 'sk'),
    (0x506800, 0x506FFF, 'si'),
    (0x507800, 0x507FFF, 'uz'),
    (0x508000, 0x50FFFF, 'ua'),
    (0x510000, 0x5107FF, 'by'),
    (0x511000, 0x5117FF, 'ee'),
    (0x512000, 0x5127FF, 'mk'),
    (0x513000, 0x5137FF, 'ba'),
    (0x514000, 0x5147FF, 'ge'),
    (0x515000, 0x5157FF, 'tj'),
    (0x516000, 0x5167FF, 'me'),
    (0x600000, 0x6007FF, 'am'),
    (0x600800, 0x600FFF, 'az'),
    (0x601000, 0x6017FF, 'kg'),
    (0x601800, 0x601FFF, 'tm'),
    (0x680000, 0x6807FF, 'bt'),
    (0x681000, 0x6817FF, 'fm'),
    (0x682000, 0x6827FF, 'mn'),
    (0x683000, 0x6837FF, 'kz'),
    (0x684000, 0x6847FF, 'pw'),
    (0x700000, 0x700FFF, 'af'),
    (0x702000, 0x702FFF, 'bd'),
    (0x704000, 0x704FFF, 'mm'),
    (0x706000, 0x706FFF, 'kw'),
    (0x708000, 0x708FFF, 'la'),
    (0x70A000, 0x70AFFF, 'np'),
    (0x70C000, 0x70C7FF, 'om'),
    (0x70E000, 0x70EFFF, 'kh'),
    (0x710000, 0x717FFF, 'sa'),
    (0x718000, 0x71FFFF, 'kr'),
    (0x720000, 0x727FFF, 'kp'),
    (0x728000, 0x72FFFF, 'iq'),
    (0x730000, 0x737FFF, 'ir'),
    (0x738000, 0x73FFFF, 'il'),
    (0x740000, 0x747FFF, 'jo'),
    (0x748000, 0x74FFFF, 'lb'),
    (0x750000, 0x757FFF, 'my'),
    (0x758000, 0x75FFFF, 'ph'),
    (0x760000, 0x767FFF, 'pk'),
    (0x768000, 0x76FFFF, 'sg'),
    (0x770000, 0x777FFF, 'lk'),
    (0x778000, 0x77FFFF, 'sy'),
    (0x789000, 0x789FFF, 'hk'),
    (0x780000, 0x7BFFFF, 'cn'),
    (0x7C0000, 0x7FFFFF, 'au'),
    (0x800000, 0x83FFFF, 'in'),
    (0x840000, 0x87FFFF, 'jp'),
    (0x880000, 0x887FFF, 'th'),
    (0x888000, 0x88FFFF, 'vn'),
    (0x890000, 0x890FFF, 'ye'),
    (0x894000, 0x894FFF, 'bh'),
    (0x895000, 0x8957FF, 'bn'),
    (0x896000, 0x896FFF, 'ae'),
    (0x897000, 0x8977FF, 'sb'),
    (0x898000, 0x898FFF, 'pg'),
    (0x899000, 0x8997FF, 'tw'),
    (0x8A0000, 0x8A7FFF, 'id'),
    (0x900000, 0x9007FF, 'mh'),
    (0x901000, 0x9017FF, 'sk'),
    (0x902000, 0x9027FF, 'ws'),
    (0xA00000, 0xAFFFFF, 'us'),
    (0xC00000, 0xC3FFFF, 'ca'),
    (0xC80000, 0xC87FFF, 'nz'),
    (0xC88000, 0xC88FFF, 'fj'),
    (0xC8A000, 0xC8A7FF, 'nr'),
    (0xC8C000, 0xC8C7FF, 'lc'),
    (0xC8D000, 0xC8D7FF, 'to'),
    (0xC8E000, 0xC8E7FF, 'ki'),
    (0xC90000, 0xC907FF, 'vu'),
    (0xC91000, 0xC917FF, 'ad'),
    (0xC92000, 0xC927FF, 'dm'),
    (0xC93000, 0xC937FF, 'kn'),
    (0xC94000, 0xC947FF, 'ss'),
    (0xC95000, 0xC957FF, 'tl'),
    (0xC97000, 0xC977FF, 'tv'),
    (0xE00000, 0xE3FFFF, 'ar'),
    (0xE40000, 0xE7FFFF, 'br'),
    (0xE80000, 0xE80FFF, 'cl'),
    (0xE84000, 0xE84FFF, 'ec'),
    (0xE88000, 0xE88FFF, 'py'),
    (0xE8C000, 0xE8CFFF, 'pe'),
    (0xE90000, 0xE90FFF, 'uy'),
    (0xE94000, 0xE94FFF, 'bo'),
    (0xF00000, 0xF07FFF, None),
    (0xF09000, 0xF097FF, None),
]


def matches_type_prefix(t, p):
    # A plain prefix match lets a short code like "C17" (C-17 Globemaster) wrongly
    # match unrelated types that extend it with more digits, e.g. "C172" (Cessna
    # 172). Variant suffixes are letters (e.g. "UH1H"), never digits, so only count
    # it as a match when the next character isn't one.
    if not t.startswith(p):
        return False
    if len(t) == len(p):
        return True
    return not t[len(p)].isdigit()



# German Navy (Marinefliegergeschwader 5, Nordholz) is GNY+number. Worth a
# callsign check specifically because its Lynx and Sea King (S61) aren't in
# MIL_TYPES at all, and its EC135s can't go in MIL_TYPES to begin with - that
# type is predominantly civilian (rescue/police), so tagging it military by
# type alone would misclassify every civilian one:
# https://knowledgebase.vatsim-germany.org/books/heli-ops/page/militarfliegerei
MIL_CALLSIGN_PREFIXES = ['GNY']


def is_military(a):
    db_flags = a.get('dbFlags')
    if db_flags and (db_flags & 1):
        return True
    callsign = (a.get('flight') or '').strip().upper()
    if any(callsign.startswith(p) for p in MIL_CALLSIGN_PREFIXES):
        return True
    t = (a.get('t') or '').upper()
    return any(matches_type_prefix(t, p) for p in MIL_TYPES)


def is_einsatz(a):
    callsign = (a.get('flight') or '').strip().upper()
    return any(callsign.startswith(p) for p in EINSATZ_CALLSIGN_PREFIXES)


def country_code_for_hex(hex_):
    if not hex_ or hex_.startswith('~'):
        return None
    try:
        val = int(hex_, 16)
    except ValueError:
        return None
    for start, end, cc in ICAO_COUNTRY_RANGES:
        if start <= val <= end:
            return cc
    return None


def airline_code_from_callsign(callsign):
    if not callsign or len(callsign) < 4:
        return None
    code = callsign[:3]
    if not code.isalpha() or code != code.upper():
        return None
    if not callsign[3].isdigit():
        return None
    return code


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


# Standard "Sunrise/Sunset Algorithm" (Almanac for Computers, 1990) - no external
# dependency/API needed, accurate to a few minutes, plenty for a fun achievement.
def sun_event_utc_hour(lat, lon, date, is_sunrise):
    zenith = math.radians(90.833)
    day_of_year = date.timetuple().tm_yday
    lng_hour = lon / 15
    t = day_of_year + (((6 if is_sunrise else 18) - lng_hour) / 24)

    M = (0.9856 * t) - 3.289
    L = M + (1.916 * math.sin(math.radians(M))) + (0.020 * math.sin(math.radians(2 * M))) + 282.634
    L = L % 360

    RA = math.degrees(math.atan(0.91764 * math.tan(math.radians(L))))
    RA = RA % 360
    l_quadrant = math.floor(L / 90) * 90
    ra_quadrant = math.floor(RA / 90) * 90
    RA = (RA + (l_quadrant - ra_quadrant)) / 15

    sin_dec = 0.39782 * math.sin(math.radians(L))
    cos_dec = math.cos(math.asin(sin_dec))
    lat_rad = math.radians(lat)

    cos_h = (math.cos(zenith) - (sin_dec * math.sin(lat_rad))) / (cos_dec * math.cos(lat_rad))
    if cos_h > 1 or cos_h < -1:
        return None  # sun never rises/sets that day at this latitude

    h = (360 - math.degrees(math.acos(cos_h))) if is_sunrise else math.degrees(math.acos(cos_h))
    h = h / 15

    T = h + RA - (0.06571 * t) - 6.622
    return (T - lng_hour) % 24


def is_before_sunrise_now():
    now_utc = datetime.now(timezone.utc)
    sunrise = sun_event_utc_hour(SITE_LAT, SITE_LON, now_utc.date(), True)
    if sunrise is None:
        return False
    return (now_utc.hour + now_utc.minute / 60) < sunrise


def is_night_now():
    now_utc = datetime.now(timezone.utc)
    sunrise = sun_event_utc_hour(SITE_LAT, SITE_LON, now_utc.date(), True)
    sunset = sun_event_utc_hour(SITE_LAT, SITE_LON, now_utc.date(), False)
    if sunrise is None or sunset is None:
        return False
    hour = now_utc.hour + now_utc.minute / 60
    return hour < sunrise or hour > sunset


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
    if 'is_einsatz' not in existing_cols:
        conn.execute('ALTER TABLE sightings ADD COLUMN is_einsatz INTEGER DEFAULT 0')
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
    conn.execute('''CREATE TABLE IF NOT EXISTS countries_seen (
        country_code TEXT PRIMARY KEY,
        hex TEXT,
        callsign TEXT,
        first_seen_at INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS airlines_seen (
        code TEXT PRIMARY KEY,
        hex TEXT,
        callsign TEXT,
        first_seen_at INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS types_seen (
        type TEXT PRIMARY KEY,
        hex TEXT,
        callsign TEXT,
        first_seen_at INTEGER
    )''')
    # readsb's own message total resets on every ultrafeeder restart; this survives
    # that by accumulating deltas instead of trusting the raw counter directly.
    conn.execute('''CREATE TABLE IF NOT EXISTS cumulative_messages (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_total INTEGER,
        accumulated INTEGER
    )''')
    # Small generic key/value store (first-ever-start timestamp for anniversaries,
    # running night-detection counter for the "Nachteule" achievement).
    conn.execute('''CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    return conn


def unlock_achievement(conn, achievement_id, hex_, callsign, now_ts):
    conn.execute(
        'INSERT OR IGNORE INTO achievements (id, unlocked_at, hex, callsign) VALUES (?,?,?,?)',
        (achievement_id, now_ts, hex_, callsign)
    )
    # The very first poll that triggers a "first sighting" achievement can catch an
    # aircraft before its callsign has decoded yet (common right as it enters range) -
    # INSERT OR IGNORE then locks that gap in permanently, since only the first insert
    # for a given id ever lands. Backfill it from a later poll of the same aircraft
    # instead of leaving it blank forever once the callsign does show up.
    if callsign and hex_:
        conn.execute(
            'UPDATE achievements SET callsign=? WHERE id=? AND hex=? AND callsign IS NULL',
            (callsign, achievement_id, hex_)
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


def get_meta_int(conn, key, default=0):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return int(row[0]) if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )


def check_achievements(conn, hex_, callsign, t_upper, cat, mil, dist, alt, overflight_alt, is_new_today, now_ts):
    for ach_id, prefixes in TYPE_ACHIEVEMENTS:
        if any(matches_type_prefix(t_upper, p) for p in prefixes):
            unlock_achievement(conn, ach_id, hex_, callsign, now_ts)
    if cat == 'A7':
        unlock_achievement(conn, 'first_helicopter', hex_, callsign, now_ts)
    if mil:
        unlock_achievement(conn, 'first_military', hex_, callsign, now_ts)

    cs_upper = (callsign or '').upper()
    for ach_id, prefixes in CALLSIGN_ACHIEVEMENTS:
        if any(cs_upper.startswith(p) for p in prefixes):
            unlock_achievement(conn, ach_id, hex_, callsign, now_ts)

    # Seltenheiten-Sammlung: wie viele der RARE_AIRCRAFT_IDS sind bereits freigeschaltet -
    # unabhaengig davon, welche genau (regional-fair, siehe Kommentar bei RARE_AIRCRAFT_IDS).
    placeholders = ','.join('?' * len(RARE_AIRCRAFT_IDS))
    rare_count = conn.execute(
        f'SELECT COUNT(*) FROM achievements WHERE id IN ({placeholders})', RARE_AIRCRAFT_IDS
    ).fetchone()[0]
    for threshold in RARE_COLLECTION_THRESHOLDS:
        if rare_count >= threshold:
            unlock_achievement(conn, f'rare_collection_{threshold}', None, None, now_ts)

    cc = country_code_for_hex(hex_)
    if cc:
        row = conn.execute('SELECT 1 FROM countries_seen WHERE country_code=?', (cc,)).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO countries_seen (country_code, hex, callsign, first_seen_at) VALUES (?,?,?,?)',
                (cc, hex_, callsign, now_ts)
            )
            ach_id = COUNTRY_FIRST_ACHIEVEMENTS.get(cc)
            if ach_id:
                unlock_achievement(conn, ach_id, hex_, callsign, now_ts)
            country_count = conn.execute('SELECT COUNT(*) FROM countries_seen').fetchone()[0]
            for threshold in COUNTRY_COUNT_THRESHOLDS:
                if country_count >= threshold:
                    unlock_achievement(conn, f'countries_{threshold}', None, None, now_ts)

    if t_upper:
        row = conn.execute('SELECT 1 FROM types_seen WHERE type=?', (t_upper,)).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO types_seen (type, hex, callsign, first_seen_at) VALUES (?,?,?,?)',
                (t_upper, hex_, callsign, now_ts)
            )
            type_count = conn.execute('SELECT COUNT(*) FROM types_seen').fetchone()[0]
            for threshold in TYPE_COUNT_THRESHOLDS:
                if type_count >= threshold:
                    unlock_achievement(conn, f'types_{threshold}', None, None, now_ts)

    code = airline_code_from_callsign(callsign)
    if code:
        row = conn.execute('SELECT 1 FROM airlines_seen WHERE code=?', (code,)).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO airlines_seen (code, hex, callsign, first_seen_at) VALUES (?,?,?,?)',
                (code, hex_, callsign, now_ts)
            )
            ach_id = AIRLINE_CODE_TO_ACHIEVEMENT.get(code)
            if ach_id:
                unlock_achievement(conn, ach_id, hex_, callsign, now_ts)
            airline_count = conn.execute('SELECT COUNT(*) FROM airlines_seen').fetchone()[0]
            for threshold in AIRLINE_COUNT_THRESHOLDS:
                if airline_count >= threshold:
                    unlock_achievement(conn, f'airlines_{threshold}', None, None, now_ts)

    if dist is not None:
        for km in RANGE_THRESHOLDS_KM:
            if dist >= km:
                unlock_achievement(conn, f'range_{km}km', hex_, callsign, now_ts)
    if alt is not None:
        for ft in ALT_THRESHOLDS_FT:
            if alt >= ft:
                unlock_achievement(conn, f'altitude_fl{ft // 100}', hex_, callsign, now_ts)
    if overflight_alt is not None:
        for m in LOWALT_THRESHOLDS_M:
            if overflight_alt <= (m / 0.3048):
                unlock_achievement(conn, f'lowalt_{m}m', hex_, callsign, now_ts)

    now_local = datetime.now()
    if 2 <= now_local.hour < 4:
        unlock_achievement(conn, 'night_watchman', hex_, callsign, now_ts)
    if is_before_sunrise_now():
        unlock_achievement(conn, 'early_bird', hex_, callsign, now_ts)
    if is_new_today and is_night_now():
        night_count = get_meta_int(conn, 'night_count') + 1
        set_meta(conn, 'night_count', night_count)
        if night_count >= NIGHT_OWL_THRESHOLD:
            unlock_achievement(conn, 'night_owl', None, None, now_ts)


def poll_once():
    with urllib.request.urlopen(POLL_URL, timeout=4) as r:
        data = json.load(r)
    today = datetime.now().strftime('%Y-%m-%d')
    now_ts = int(time.time())
    conn = get_db()

    start_ts = get_meta_int(conn, 'first_started_at', 0)
    if start_ts == 0:
        start_ts = now_ts
        set_meta(conn, 'first_started_at', start_ts)
    days_elapsed = (now_ts - start_ts) // 86400
    for d in ANNIVERSARY_DAYS:
        if days_elapsed >= d:
            unlock_achievement(conn, f'anniversary_{d}', None, None, now_ts)

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
        speed = speed if isinstance(speed, (int, float)) and 0 <= speed <= MAX_PLAUSIBLE_SPEED_KT else None
        type_ = a.get('t') or (a.get('desc') or '').strip() or None
        callsign = (a.get('flight') or '').strip() or None
        mil = 1 if is_military(a) else 0
        einsatz = 1 if is_einsatz(a) else 0
        cat = a.get('category') or ''
        t_upper = (a.get('t') or '').upper()

        row = conn.execute(
            'SELECT max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft, is_military, is_einsatz FROM sightings WHERE hex=? AND seen_date=?',
            (hex_, today)
        ).fetchone()
        is_new_today = row is None

        check_achievements(conn, hex_, callsign, t_upper, cat, mil, dist, alt, overflight_alt, is_new_today, now_ts)
        maybe_break_record(conn, 'max_alt', alt, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'max_dist', dist, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'max_speed', speed, hex_, callsign, now_ts, higher_is_better=True)
        maybe_break_record(conn, 'min_alt', overflight_alt, hex_, callsign, now_ts, higher_is_better=False)

        if is_new_today:
            conn.execute(
                'INSERT INTO sightings (hex, seen_date, type, callsign, max_alt_ft, max_dist_km, max_speed_kt, min_alt_ft, is_military, is_einsatz, first_seen_ts) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (hex_, today, type_, callsign, alt, dist, speed, overflight_alt, mil, einsatz, now_ts)
            )
        else:
            new_alt = max((v for v in (row[0], alt) if v is not None), default=None)
            new_dist = max((v for v in (row[1], dist) if v is not None), default=None)
            new_speed = max((v for v in (row[2], speed) if v is not None), default=None)
            new_min_alt = min((v for v in (row[3], overflight_alt) if v is not None), default=None)
            new_mil = 1 if (row[4] or mil) else 0
            new_einsatz = 1 if (row[5] or einsatz) else 0
            conn.execute(
                'UPDATE sightings SET max_alt_ft=?, max_dist_km=?, max_speed_kt=?, min_alt_ft=?, is_military=?, is_einsatz=?, '
                'type=COALESCE(?, type), callsign=COALESCE(?, callsign) WHERE hex=? AND seen_date=?',
                (new_alt, new_dist, new_speed, new_min_alt, new_mil, new_einsatz, type_, callsign, hex_, today)
            )

    today_count = conn.execute(
        'SELECT COUNT(DISTINCT hex) FROM sightings WHERE seen_date=?', (today,)
    ).fetchone()[0]
    for n in DAILY_COUNT_THRESHOLDS:
        if today_count >= n:
            unlock_achievement(conn, f'daily_{n}', None, None, now_ts)

    total_aircraft_count = conn.execute('SELECT COUNT(DISTINCT hex) FROM sightings').fetchone()[0]
    for n in AIRCRAFT_COUNT_THRESHOLDS:
        if total_aircraft_count >= n:
            unlock_achievement(conn, f'aircraft_{n}', None, None, now_ts)

    conn.commit()
    conn.close()


def poll_stats_once():
    with urllib.request.urlopen(STATS_URL, timeout=4) as r:
        data = json.load(r)
    total_msgs = data.get('total', {}).get('messages')
    if total_msgs is None:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    now_ts = int(time.time())
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

    cum_row = conn.execute('SELECT last_total, accumulated FROM cumulative_messages WHERE id=1').fetchone()
    if cum_row is None:
        conn.execute('INSERT INTO cumulative_messages (id, last_total, accumulated) VALUES (1, ?, 0)', (total_msgs,))
        accumulated = 0
    else:
        last_total, accumulated = cum_row
        accumulated += (total_msgs - last_total) if total_msgs >= last_total else total_msgs
        conn.execute('UPDATE cumulative_messages SET last_total=?, accumulated=? WHERE id=1', (total_msgs, accumulated))
    for threshold in MSG_THRESHOLDS:
        if accumulated >= threshold:
            unlock_achievement(conn, f'msg_{threshold}', None, None, now_ts)

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
    elif range_ == 'yesterday':
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        where = 'seen_date = ?'
        params = (yesterday,)
    else:
        where = '1=1'
        params = ()

    unique_count = conn.execute(f'SELECT COUNT(DISTINCT hex) FROM sightings WHERE {where}', params).fetchone()[0]
    military_count = conn.execute(
        f'SELECT COUNT(DISTINCT hex) FROM sightings WHERE {where} AND is_military=1', params
    ).fetchone()[0]
    einsatz_count = conn.execute(
        f'SELECT COUNT(DISTINCT hex) FROM sightings WHERE {where} AND is_einsatz=1', params
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

    is_single_day = range_ in ('today', 'yesterday')

    messages_today = None
    if is_single_day:
        mrow = conn.execute(
            'SELECT start_total, latest_total FROM daily_messages WHERE seen_date=?', params
        ).fetchone()
        if mrow:
            messages_today = mrow[1] - mrow[0]

    dir_where = 'seen_date = ?' if is_single_day else '1=1'
    dir_rows = conn.execute(
        f'SELECT quadrant, MAX(max_dist_km) FROM direction_stats WHERE {dir_where} GROUP BY quadrant', params
    ).fetchall()
    directions = {'N': None, 'E': None, 'S': None, 'W': None}
    for q, d in dir_rows:
        directions[q] = d

    range_where = 'seen_date = ?' if is_single_day else '1=1'
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
        'einsatzCount': einsatz_count,
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


# "Ø pro Tag" - averages per calendar day, computed only from fully-completed
# days (seen_date < today). The day still in progress is deliberately excluded
# so the average isn't dragged down by a today that hasn't finished yet - see
# CHANGELOG for the reasoning. Direction/range-outline breakdowns aren't
# meaningfully "average-able" the same way (they're percentile-based spatial
# summaries, not simple counts), so those are left empty here; the frontend
# already renders an empty state for both when there's no data.
def get_avgday_summary():
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')

    day_count, since_date = conn.execute(
        'SELECT COUNT(DISTINCT seen_date), MIN(seen_date) FROM sightings WHERE seen_date < ?', (today,)
    ).fetchone()
    if not day_count:
        conn.close()
        return {'dayCount': 0, 'sinceDate': None}

    def avg_per_day(select_expr, extra_where=''):
        row = conn.execute(
            f'SELECT AVG(c) FROM (SELECT seen_date, {select_expr} c FROM sightings '
            f'WHERE seen_date < ? {extra_where} GROUP BY seen_date)', (today,)
        ).fetchone()
        return row[0] if row else None

    def top_avg_per_day(group_expr, extra_where=''):
        return conn.execute(
            f'SELECT g, AVG(c) avgc FROM (SELECT seen_date, {group_expr} g, COUNT(*) c FROM sightings '
            f'WHERE seen_date < ? {extra_where} GROUP BY seen_date, g) GROUP BY g '
            f'ORDER BY avgc DESC LIMIT 5', (today,)
        ).fetchall()

    unique_count = avg_per_day('COUNT(DISTINCT hex)')
    military_count = avg_per_day('COUNT(DISTINCT hex)', 'AND is_military=1')
    einsatz_count = avg_per_day('COUNT(DISTINCT hex)', 'AND is_einsatz=1')
    top_types = top_avg_per_day('type', 'AND type IS NOT NULL')
    top_calls = top_avg_per_day('callsign', 'AND callsign IS NOT NULL')
    top_airlines = top_avg_per_day("SUBSTR(callsign,1,3)", "AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*'")

    max_alt = avg_per_day('MAX(max_alt_ft)', 'AND max_alt_ft IS NOT NULL')
    max_dist = avg_per_day('MAX(max_dist_km)', 'AND max_dist_km IS NOT NULL')
    max_speed = avg_per_day('MAX(max_speed_kt)', 'AND max_speed_kt IS NOT NULL')
    min_alt = avg_per_day('MIN(min_alt_ft)', 'AND min_alt_ft IS NOT NULL')

    avg_row = conn.execute(
        'SELECT AVG(max_dist_km), AVG(max_alt_ft) FROM sightings WHERE seen_date < ?', (today,)
    ).fetchone()
    messages_avg = conn.execute(
        'SELECT AVG(latest_total - start_total) FROM daily_messages WHERE seen_date < ?', (today,)
    ).fetchone()[0]

    conn.close()
    return {
        'dayCount': day_count,
        'sinceDate': since_date,
        'uniqueCount': round(unique_count) if unique_count is not None else 0,
        'militaryCount': round(military_count) if military_count is not None else 0,
        'einsatzCount': round(einsatz_count) if einsatz_count is not None else 0,
        'topTypes': [[g, round(c)] for g, c in top_types],
        'topCallsigns': [[g, round(c)] for g, c in top_calls],
        'topAirlineCodes': [[g, round(c)] for g, c in top_airlines],
        'maxAlt': {'value': max_alt, 'callsign': None} if max_alt is not None else None,
        'maxDist': {'value': max_dist, 'callsign': None} if max_dist is not None else None,
        'maxSpeed': {'value': max_speed, 'callsign': None} if max_speed is not None else None,
        'minAlt': {'value': min_alt, 'callsign': None} if min_alt is not None else None,
        'avgDist': avg_row[0] if avg_row else None,
        'avgAlt': avg_row[1] if avg_row else None,
        'messagesToday': round(messages_avg) if messages_avg is not None else None,
        'directions': {'N': None, 'E': None, 'S': None, 'W': None},
        'rangeOutline': {},
        'rangeBucketDeg': RANGE_BUCKET_DEG,
    }


def get_achievements():
    conn = get_db()
    unlocked = conn.execute('SELECT id, unlocked_at, hex, callsign FROM achievements').fetchall()
    records = conn.execute('SELECT category, value, hex, callsign, broken_at FROM records').fetchall()
    country_count = conn.execute('SELECT COUNT(*) FROM countries_seen').fetchone()[0]
    airline_count = conn.execute('SELECT COUNT(*) FROM airlines_seen').fetchone()[0]
    type_count = conn.execute('SELECT COUNT(*) FROM types_seen').fetchone()[0]
    aircraft_count = conn.execute('SELECT COUNT(DISTINCT hex) FROM sightings').fetchone()[0]
    cum_row = conn.execute('SELECT accumulated FROM cumulative_messages WHERE id=1').fetchone()
    conn.close()
    total_xp = sum(ACHIEVEMENT_XP.get(i, 0) for i, ts, hex_, cs in unlocked)
    return {
        'unlocked': [
            {'id': i, 'unlockedAt': ts, 'hex': hex_, 'callsign': cs} for i, ts, hex_, cs in unlocked
        ],
        'records': {
            cat: {'value': val, 'hex': hex_, 'callsign': cs, 'brokenAt': ts}
            for cat, val, hex_, cs, ts in records
        },
        'countryCount': country_count,
        'airlineCount': airline_count,
        'typeCount': type_count,
        'aircraftCount': aircraft_count,
        'messagesAccumulated': cum_row[0] if cum_row else 0,
        'levelInfo': compute_level(total_xp),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/summary':
            qs = parse_qs(parsed.query)
            range_ = qs.get('range', ['today'])[0]
            if range_ not in ('today', 'yesterday', 'alltime', 'avgday'):
                range_ = 'today'
            try:
                self._send_json(get_avgday_summary() if range_ == 'avgday' else get_summary(range_))
            except Exception as e:
                print('summary error:', e, flush=True)
                self.send_response(500)
                self.end_headers()
        elif parsed.path == '/api/achievements':
            try:
                self._send_json(get_achievements())
            except Exception as e:
                print('achievements error:', e, flush=True)
                self.send_response(500)
                self.end_headers()
        elif parsed.path == '/api/station':
            self._send_json({'name': STATION_NAME})
        else:
            self.send_response(404)
            self.end_headers()


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', 8090), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()
