import base64
import json
import logging
import os
import unicodedata
from pathlib import Path

import streamlit as st
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
berlin_tz = ZoneInfo("Europe/Berlin")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bahn_app")

ENV_FILE = Path(__file__).resolve().parent / ".env"
LOGO_FILE = Path(__file__).resolve().parent / "Schullogo.png"


def load_env_file():
    if not ENV_FILE.exists():
        return {}

    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_logo_base64():
    if not LOGO_FILE.exists():
        return None

    with LOGO_FILE.open("rb") as f:
        data = f.read()
    return base64.b64encode(data).decode("utf-8")


ENV_VALUES = load_env_file()
LOGO_BASE64 = load_logo_base64()


def get_db_api_credentials():
    client_id = os.getenv("DB_API_CLIENT_ID") or ENV_VALUES.get("DB_API_CLIENT_ID")
    client_secret = os.getenv("DB_API_CLIENT_SECRET") or ENV_VALUES.get("DB_API_CLIENT_SECRET")
    return client_id, client_secret

try:
    from deutsche_bahn_api.api_authentication import ApiAuthentication
    from deutsche_bahn_api.station_helper import StationHelper
    from deutsche_bahn_api.timetable_helper import TimetableHelper
except ImportError:
    logging.getLogger("bahn_app").warning(
        "deutsche-bahn-api ist nicht installiert oder die Importstruktur hat sich geändert. Fallback-Modus wird verwendet."
    )
    ApiAuthentication = None
    StationHelper = None
    TimetableHelper = None

try:
    from pyhafas import HafasClient
    from pyhafas.profile import DBProfile
    from pyhafas.profile.base.helper.request import BaseErrorCodesMapping
    from pyhafas.types.hafas_response import HafasResponse

    class ResilientDBProfile(DBProfile):
        request_timeout = 10

        def __init__(self, ua=None):
            super().__init__(ua)
            self.activate_retry(retries=5, backoff_factor=0.5)

        def request(self, body):
            data = {
                'svcReqL': [body]
            }
            data.update(self.requestBody)
            data = json.dumps(data)

            res = self.request_session.post(
                self.url_formatter(data),
                data=data,
                headers={
                    'User-Agent': self.userAgent,
                    'Content-Type': 'application/json'
                },
                timeout=self.request_timeout,
            )
            return HafasResponse(res, BaseErrorCodesMapping)

    client = HafasClient(ResilientDBProfile())
except ImportError:
    logger.warning("pyhafas ist nicht installiert. Hafas-Live-Daten werden deaktiviert.")
    client = None

STATION_WOERRSTADT = "8006622"

# 1. Seiteneinstellungen
st.set_page_config(page_title="Bahnhof Wörrstadt Informationsbildschirm", layout="wide")

# 2. CSS-Eingriff
st.markdown("""
    <style>
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
            background-color: #000000 !important;
        }
        .block-container { padding: 0rem !important; margin: 0rem !important; max-width: 100% !important; }
        header, footer { visibility: hidden !important; height: 0px !important; }
    </style>
""", unsafe_allow_html=True)

# Koordinaten
SCHULE_LAT, SCHULE_LON = 49.7891, 8.1672
BAHNHOF_LAT, BAHNHOF_LON = 49.8431, 8.1169

@st.cache_data(ttl=3600)
def get_live_walking_time():
    try:
        url = f"http://router.project-osrm.org/route/v1/foot/{SCHULE_LON},{SCHULE_LAT};{BAHNHOF_LON},{BAHNHOF_LAT}?overview=false"
        response = requests.get(url).json()
        seconds = response['routes'][0]['duration']
        return f"{round(seconds / 60)} Min."
    except:
        return "18 Min."

# 3. Wetterdaten
def describe_weather_code(code):
    descriptions = {
        0: "Klar",
        1: "Überwiegend klar",
        2: "Teilweise bewölkt",
        3: "Bedeckt",
        45: "Nebel",
        48: "Nebel",
        51: "Leichter Sprühregen",
        53: "Sprühregen",
        55: "Starker Sprühregen",
        56: "Leichter gefrierender Sprühregen",
        57: "Gefrierender Sprühregen",
        61: "Leichter Regen",
        63: "Regen",
        65: "Starker Regen",
        66: "Leichter gefrierender Regen",
        67: "Gefrierender Regen",
        71: "Leichter Schneefall",
        73: "Schneefall",
        75: "Starker Schneefall",
        77: "Schneegriesel",
        80: "Leichter Regenschauer",
        81: "Regenschauer",
        82: "Starker Regenschauer",
        85: "Leichter Schneeschauer",
        86: "Starker Schneeschauer",
        95: "Gewitter",
        96: "Gewitter mit leichtem Hagel",
        99: "Gewitter mit Hagel",
    }
    return descriptions.get(code, "Unbekanntes Wetter")


@st.cache_data(ttl=600)
def get_woerrstadt_weather():
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=49.8431&longitude=8.1169&current=temperature_2m,weather_code&timezone=Europe%2FBerlin"
        response = requests.get(url).json()
        code = response["current"]["weather_code"]
        temp = round(response["current"]["temperature_2m"])

        beschreibung = describe_weather_code(code)
        wetter_typ = "Schlecht" if code not in [0, 1, 2, 3] else "Gut"
        return wetter_typ, f"{beschreibung} ({temp}°C)"
    except:
        return "Normal", "Keine Wetterdaten (--°C)"


def next_fixed_time(minutes):
    now = datetime.now()
    for minute in minutes:
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate >= now:
            return candidate.strftime('%H:%M')
    next_hour = (now + timedelta(hours=1)).replace(minute=minutes[0], second=0, microsecond=0)
    return next_hour.strftime('%H:%M')


def get_next_fixed_schedule(minutes, count=2, now=None):
    now = now or datetime.now()
    candidates = []
    for minute in minutes:
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate < now:
            candidate = candidate + timedelta(hours=1)
        candidates.append(candidate)
    ordered = sorted(candidates)
    return [candidate.strftime('%H:%M') for candidate in ordered[:count]]


def get_mock_zug_daten():
    return (
        get_next_fixed_schedule([7, 28], count=2),
        get_next_fixed_schedule([28, 47], count=2),
        "Fallback-Modus aktiv: feste Wörrstadt-Fahrpläne genutzt"
    )


def parse_departure_time(value, base_time=None):
    if not value or value == "---":
        return None
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except ValueError:
        return None

    base = base_time or datetime.now()
    candidate = base.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    return candidate


def get_next_train_time(current_time, direction):
    try:
        if direction == "mainz":
            schedule_minutes = [28, 47]
        else:
            schedule_minutes = [7, 28]

        if not current_time or current_time == "---":
            return next_fixed_time(schedule_minutes)

        parsed = datetime.strptime(str(current_time), "%H:%M")
        current_hour = parsed.hour
        current_minute = parsed.minute

        for minute in schedule_minutes:
            if minute > current_minute:
                return f"{current_hour:02d}:{minute:02d}"

        next_hour = current_hour + 1 if current_hour < 23 else 0
        return f"{next_hour:02d}:{schedule_minutes[0]:02d}"
    except Exception:
        return next_fixed_time([7, 28, 47])


def get_train_catch_status(train_time, walk_minutes, now=None):
    now = now or datetime.now(berlin_tz)

    if not train_time or train_time == "---":
        return False, "Keine Abfahrt", "#b0b0b0"

    try:
        departure_dt = datetime.strptime(str(train_time), "%H:%M")
        departure = now.replace(hour=departure_dt.hour, minute=departure_dt.minute, second=0, microsecond=0)
        if departure < now:
            departure = departure + timedelta(hours=1)

        arrival_time = now + timedelta(minutes=walk_minutes)
        can_make_it = arrival_time <= departure
        text = "Du schaffst es" if can_make_it else "Du schaffst es nicht"
        color = "#2ecc71" if can_make_it else "#ff4d4d"
        return can_make_it, text, color
    except Exception:
        return False, "Zeit unbekannt", "#b0b0b0"


def normalize_station_text(value):
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    return normalized.lower()


def get_deutsche_bahn_api_daten():
    if ApiAuthentication is None or StationHelper is None or TimetableHelper is None:
        logger.info("DB API-Module nicht verfügbar; statischer Fallback wird verwendet")
        return None

    client_id, client_secret = get_db_api_credentials()

    if not client_id or not client_secret:
        logger.info("DB API-Anmeldedaten fehlen; statischer Fallback wird verwendet")
        return None

    try:
        api_auth = ApiAuthentication(client_id, client_secret)
        helper = StationHelper()
        starting_stations = helper.find_stations_by_name("Wörrstadt")
        if not starting_stations:
            logger.warning("Startbahnhof Wörrstadt konnte nicht über die DB API gefunden werden")
            return None

        timetable_helper = TimetableHelper(starting_stations[0], api_auth)
        departures = timetable_helper.get_timetable()

        def collect_times(keyword):
            matches = []
            for train in departures:
                departure = getattr(train, "departure", None)
                if not departure:
                    continue
                route_text = normalize_station_text((getattr(train, "stations", "") or "") + " " + (getattr(train, "passed_stations", "") or ""))
                if keyword in route_text:
                    matches.append(departure)
            return matches[:4]

        alzey = collect_times("alzey")
        mainz = collect_times("mainz")

        if not alzey or not mainz:
            logger.warning("DB API lieferte keine vollständigen Abfahrten für Alzey/Mainz")
            return None

        return alzey, mainz, None
    except requests.exceptions.ConnectionError as err:
        logger.warning("DB API-Netzwerkfehler (Host/Internet), statischer Fallback wird verwendet: %s", err)
        return None
    except requests.exceptions.RequestException as err:
        logger.warning("DB API-Netzwerkfehler, statischer Fallback wird verwendet: %s", err)
        return None
    except Exception:
        logger.exception("Unbekannter Fehler bei der DB API, statischer Fallback wird verwendet")
        return None


def get_db_transport_rest_daten():
    try:
        search_resp = requests.get(
            "https://v6.db.transport.rest/stations",
            params={"query": "Wörrstadt"},
            timeout=10,
        )
        search_resp.raise_for_status()
        stations = search_resp.json()
        station = next(
            (
                s
                for s in stations
                if "woerrstadt" in normalize_station_text(s.get("name"))
            ),
            None,
        )
        if not station or not station.get("id"):
            logger.warning("Community-API: Wörrstadt-Station nicht gefunden")
            return None

        
        departures_resp = requests.get(
            f"https://v6.db.transport.rest/station/{station['id']}/departures",
            params={"duration": 60,"when": datetime.now(berlin_tz).isoformat()},
            timeout=10,
        )
        departures_resp.raise_for_status()
        departures = departures_resp.json()

        def parse_time(iso_str):
            try:
                if iso_str is None:
                    return None
                dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                dt_berlin = dt.astimezone(berlin_tz)
                return dt_berlin.strftime("%H:%M")
            except Exception:
                return None

        def collect_times(keyword):
            matches = []
            for d in departures:
                departure = parse_time(d.get("departure"))
                if departure and keyword in normalize_station_text(d.get("direction", "")):
                    matches.append(departure)
            return matches[:4]

        alzey = collect_times("alzey")
        mainz = collect_times("mainz")

        if not alzey and not mainz:
            logger.warning("Community-API lieferte keine passenden Züge nach Alzey oder Mainz")
            return None

        return alzey or ["---"], mainz or ["---"], "Community-API: v6.db.transport.rest"
    except requests.exceptions.RequestException as err:
        logger.warning("Community-API-Netzwerkfehler, statischer Fallback wird verwendet: %s", err)
        return None
    except Exception:
        logger.exception("Unbekannter Fehler bei der Community-API, statischer Fallback wird verwendet")
        return None


def get_zug_daten():
    if client is not None:
        try:
            deps = client.departures(STATION_WOERRSTADT, datetime.now(), duration=60)
            if not deps:
                return ["---"], ["---"], "Keine Abfahrten gefunden"

            alzey = [d.planned_departure.strftime('%H:%M') for d in deps if d.direction and "Alzey" in d.direction][:4]
            mainz = [d.planned_departure.strftime('%H:%M') for d in deps if d.direction and "Mainz" in d.direction][:4]

            if not alzey and not mainz:
                return ["---"], ["---"], "Keine passenden Züge nach Alzey oder Mainz in den nächsten 60 Minuten"

            return alzey or ["---"], mainz or ["---"], None
        except requests.exceptions.RequestException as err:
            logger.warning("Bahn-API-Netzwerkfehler, versuche Deutsche-Bahn-API-Fallback: %s", err)
        except Exception:
            logger.exception("Unbekannter Fehler bei der Bahn-API, versuche Deutsche-Bahn-API-Fallback")
    else:
        logger.warning("pyhafas-Client ist nicht verfügbar, versuche Deutsche-Bahn-API-Fallback")

    db_api_result = get_deutsche_bahn_api_daten()
    if db_api_result:
        return db_api_result

    transport_rest_result = get_db_transport_rest_daten()
    if transport_rest_result:
        return transport_rest_result

    return get_mock_zug_daten()

# Wetter und Zeit aufrufen (dein alter Code)
wetter_status, wetter_final_text = get_woerrstadt_weather()
basis_zeit = int(get_live_walking_time().split()[0])
gehzeit_text = f"{basis_zeit + 3} Min." if wetter_status == "Schlecht" else f"{basis_zeit} Min."
gehzeit_minutes = int(gehzeit_text.split()[0])

# Züge aufrufen (der neue Teil)
with st.spinner("Running..."):
    alzey_schedule, mainz_schedule, zug_warnung = get_zug_daten()

alzey_zeit = alzey_schedule[0] if alzey_schedule else "---"
mainz_zeit = mainz_schedule[0] if mainz_schedule else "---"
mainz_can_make_it, mainz_status_text, mainz_time_color = get_train_catch_status(mainz_zeit, gehzeit_minutes)
alzey_can_make_it, alzey_status_text, alzey_time_color = get_train_catch_status(alzey_zeit, gehzeit_minutes)
can_catch_any_train = mainz_can_make_it or alzey_can_make_it
catch_status_text = "Du schaffst es zu einem der nächsten Züge" if can_catch_any_train else "Du schaffst es zu keinem der nächsten Züge"
catch_status_color = "#2ecc71" if can_catch_any_train else "#ff4d4d"
gehzeit_color = catch_status_color
fallback_badge = "Fallback Modus aktiv" if zug_warnung else ""

# 4. HTML/CSS/JS mit den neuen Cockpit-Fenstern
html_code = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <style>
        html, body {
            background-color: #000000;
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
            color: #ffffff;
            font-family: 'Arial', sans-serif;
        }

        .canvas {
            width: 1920px;
            height: 1080px;
            background-color: #000000;
            position: relative;
        }

        .logo-wrapper {
            position: absolute;
            top: 30px;
            left: 30px;
            z-index: 45;
            width: 140px;
            height: auto;
            pointer-events: none;
        }

        .dashboard-logo {
            width: 100%;
            height: auto;
            display: block;
            opacity: 0.92;
            background: transparent;
            border-radius: 14px;
            filter: drop-shadow(0 0 18px rgba(255,255,255,0.18));
        }

        #clock {
            position: absolute;
            top: 60px;
            right: 80px;
            font-size: 3.5rem;
            font-weight: bold;
            letter-spacing: 2px;
        }

        .simulation-container {
            position: absolute;
            top: 40%;
            left: 0;
            width: 100%;
            height: 300px;
        }

        .track {
            position: absolute;
            left: 0;
            bottom: 60px;
            width: 100%;
            height: 12px;
            background: linear-gradient(to bottom, #666, #333);
            border-bottom: 6px dashed #444;
        }

        .station {
            position: absolute;
            left: 50%;
            bottom: 40px;
            transform: translateX(-50%);
            display: flex;
            flex-direction: column-reverse;
            align-items: center;
            z-index: 20;
        }

        .station-icon {
            font-size: 140px;
            filter: drop-shadow(0 0 20px rgba(255,255,255,0.7));
        }

        .station-text {
            font-size: 28px;
            font-weight: bold;
            color: #ffffff;
            margin-bottom: 15px;
            text-transform: uppercase;
            letter-spacing: 3px;
        }

        /* --- ZUGVERBÄNDE --- */
        .train-set {
            position: absolute;
            bottom: 68px;
            display: flex;
            gap: 6px;
            align-items: flex-end;
            z-index: 10;
        }

        .train-set .train-info {
            position: absolute;
            top: -72px;
            left: 50%;
            transform: translateX(-50%);
            padding: 10px 14px;
            background: rgba(0, 0, 0, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.6);
            border-radius: 12px;
            font-size: 0.95rem;
            line-height: 1.2;
            letter-spacing: 1px;
            color: #ffffff;
            text-align: center;
            white-space: nowrap;
            box-shadow: 0 6px 15px rgba(0, 0, 0, 0.45);
        }

        .train-set-left .train-info,
        .train-set-right .train-info {
            left: 50%;
            right: auto;
            transform: translateX(-50%);
        }

        .train-set-left .train-info {
            background: rgba(5, 65, 120, 0.92);
            border-color: rgba(80, 190, 255, 0.65);
        }

        .train-set-right .train-info {
            background: rgba(70, 100, 40, 0.92);
            border-color: rgba(220, 255, 145, 0.65);
        }

        .train-time {
            font-weight: bold;
        }

        .train-set .train-info .train-direction {
            display: block;
            margin-top: 4px;
            font-size: 0.75rem;
            color: #cfcfcf;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .train-set-left {
            left: -1000px;
            flex-direction: row-reverse; 
            animation: driveThroughRight 18s infinite linear;
        }

        .train-set-right {
            right: -1000px;
            flex-direction: row; 
            animation: driveThroughLeft 18s infinite linear;
        }

        .train-set-trailing-left,
        .train-set-trailing-right {
            position: absolute;
            bottom: 68px;
            display: flex;
            gap: 6px;
            align-items: flex-end;
            z-index: 5;
            opacity: 0.55;
            transform: scale(0.9);
        }

        .train-set-trailing-left .train-info,
        .train-set-trailing-right .train-info {
            position: absolute;
            top: -72px;
            left: 50%;
            transform: translateX(-50%);
            padding: 8px 12px;
            background: rgba(90, 90, 90, 0.9);
            border: 1px solid rgba(255, 255, 255, 0.25);
            border-radius: 10px;
            font-size: 0.9rem;
            line-height: 1.2;
            letter-spacing: 1px;
            color: #f5f5f5;
            text-align: center;
            white-space: nowrap;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
        }

        .train-set-trailing-left {
            left: -1000px;
            flex-direction: row-reverse;
            animation: driveThroughRight 18s infinite linear;
            animation-delay: 2s;
        }

        .train-set-trailing-right {
            right: -1000px;
            flex-direction: row;
            animation: driveThroughLeft 18s infinite linear;
            animation-delay: 2s;
        }

        .train-set-trailing-left .locomotive,
        .train-set-trailing-right .locomotive {
            background-color: #6a6a6a;
            box-shadow: 0 8px 15px rgba(90, 90, 90, 0.35);
        }

        .train-set-trailing-left .wagon,
        .train-set-trailing-right .wagon {
            background-color: #7d7d7d;
            border-bottom-color: #4a4a4a;
        }

        .train-set-trailing-left .loco-front,
        .train-set-trailing-right .loco-front {
            border-radius: 15px 45px 5px 5px;
            border-left: 6px solid #fff;
            justify-content: flex-start;
            padding-left: 30px;
        }

        .train-set-trailing-left .loco-back,
        .train-set-trailing-right .loco-back {
            border-radius: 45px 15px 5px 5px;
            border-right: 6px solid #fff;
            justify-content: flex-end;
            padding-right: 30px;
        }

        .train-set-trailing-right .loco-front {
            border-radius: 45px 15px 5px 5px;
            border-right: 6px solid #fff;
            justify-content: flex-end;
            padding-right: 30px;
        }

        .train-set-trailing-right .loco-back {
            border-radius: 15px 45px 5px 5px;
            border-left: 6px solid #fff;
            justify-content: flex-start;
            padding-left: 30px;
        }

        .train-set-trailing-left .loco-front::after,
        .train-set-trailing-right .loco-front::after,
        .train-set-trailing-left .loco-back::after,
        .train-set-trailing-right .loco-back::after {
            content: '';
            position: absolute;
            top: 8px;
            width: 35px;
            height: 18px;
            background-color: rgba(255, 255, 255, 0.5);
            border-radius: 2px 12px 2px 2px;
        }

        .train-set-trailing-left .loco-front::after {
            right: 8px;
        }

        .train-set-trailing-left .loco-back::after {
            left: 8px;
            border-radius: 12px 2px 2px 2px;
        }

        .train-set-trailing-right .loco-front::after {
            left: 8px;
            border-radius: 12px 2px 2px 2px;
        }

        .train-set-trailing-right .loco-back::after {
            right: 8px;
            border-radius: 2px 12px 2px 2px;
        }

        /* Die Basis-Lokomotive */
        .locomotive {
            width: 200px;
            height: 55px;
            background-color: #0f2b5c; 
            box-shadow: 0 8px 15px rgba(15, 43, 92, 0.5);
            display: flex;
            align-items: center;
            position: relative;
            font-weight: bold;
            font-size: 22px;
            color: #ffffff;
            box-sizing: border-box;
        }

        /* --- DESIGN & FAHRERFENSTER FÜR LINKEN ZUG --- */
        .train-set-left .loco-front {
            border-radius: 15px 45px 5px 5px;
            border-left: 6px solid #fff;
            justify-content: flex-start;
            padding-left: 30px;
        }
        /* Cockpit-Fenster vorne rechts schräg */
        .train-set-left .loco-front::after {
            content: '';
            position: absolute;
            right: 8px;
            top: 8px;
            width: 35px;
            height: 18px;
            background-color: rgba(255, 255, 255, 0.6);
            border-radius: 2px 12px 2px 2px;
        }

        .train-set-left .loco-back {
            border-radius: 45px 15px 5px 5px;
            border-right: 6px solid #fff;
            justify-content: flex-end;
            padding-right: 30px;
        }
        /* Cockpit-Fenster hinten links schräg */
        .train-set-left .loco-back::after {
            content: '';
            position: absolute;
            left: 8px;
            top: 8px;
            width: 35px;
            height: 18px;
            background-color: rgba(255, 255, 255, 0.6);
            border-radius: 12px 2px 2px 2px;
        }

        /* --- DESIGN & FAHRERFENSTER FÜR RECHTEN ZUG --- */
        .train-set-right .loco-front {
            border-radius: 45px 15px 5px 5px;
            border-right: 6px solid #fff;
            justify-content: flex-end;
            padding-right: 30px;
        }
        /* Cockpit-Fenster vorne links schräg */
        .train-set-right .loco-front::after {
            content: '';
            position: absolute;
            left: 8px;
            top: 8px;
            width: 35px;
            height: 18px;
            background-color: rgba(255, 255, 255, 0.6);
            border-radius: 12px 2px 2px 2px;
        }

        .train-set-right .loco-back {
            border-radius: 15px 45px 5px 5px;
            border-left: 6px solid #fff;
            justify-content: flex-start;
            padding-left: 30px;
        }
        /* Cockpit-Fenster hinten rechts schräg */
        .train-set-right .loco-back::after {
            content: '';
            position: absolute;
            right: 8px;
            top: 8px;
            width: 35px;
            height: 18px;
            background-color: rgba(255, 255, 255, 0.6);
            border-radius: 2px 12px 2px 2px;
        }

        /* Die Passagier-Wagons */
        .wagon {
            width: 180px;
            height: 50px;
            background-color: #112d61; 
            border-radius: 5px;
            border-bottom: 4px solid #000;
            display: flex;
            justify-content: space-around;
            align-items: center;
            padding: 0 10px;
            box-sizing: border-box;
        }

        .window {
            width: 35px;
            height: 16px;
            background-color: rgba(255, 255, 255, 0.35);
            border-radius: 3px;
        }

        /* --- INFOKASTEN --- */
        .passenger-container {
            position: absolute;
            bottom: 60px;
            left: 50%;
            transform: translateX(-50%);
            display: flex;
            flex-direction: column;
            align-items: center;
            z-index: 30;
        }

        .passenger {
            font-size: 140px;
            margin-bottom: -10px;
            animation: walkAnimation 1.2s infinite ease-in-out;
        }

        .info-box {
            background-color: rgba(15, 15, 15, 0.85);
            border: 2px solid #ffffff;
            border-radius: 8px;
            padding: 12px 25px;
            text-align: center;
            font-size: 20px;
            line-height: 1.5;
            letter-spacing: 1px;
            box-shadow: 0 4px 15px rgba(255, 255, 255, 0.1);
        }

        .info-box span {
            color: #ffcc00;
            font-weight: bold;
        }

        .fallback-badge {
            position: absolute;
            bottom: 12px;
            right: 12px;
            padding: 5px 10px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 10px;
            color: #d4d4d4;
            font-size: 0.7rem;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            box-shadow: 0 3px 10px rgba(0, 0, 0, 0.25);
            backdrop-filter: blur(5px);
        }

        @keyframes driveThroughRight {
            0% { left: -1000px; }
            35% { left: calc(50% - 744px); } 
            60% { left: calc(50% - 744px); } 
            100% { left: 2200px; }
        }

        @keyframes driveThroughLeft {
            0% { right: -1000px; }
            35% { right: calc(50% - 744px); } 
            60% { right: calc(50% - 744px); } 
            100% { right: 2200px; }
        }

        @keyframes walkAnimation {
            0% { transform: translateY(0) rotate(0deg); }
            25% { transform: translateY(-15px) rotate(-3deg); }
            50% { transform: translateY(0) rotate(0deg); }
            75% { transform: translateY(-15px) rotate(3deg); }
            100% { transform: translateY(0) rotate(0deg); }
        }
    </style>
</head>
<body>

    <div class="canvas">
        <div class="logo-wrapper">
            <img class="dashboard-logo" src="data:image/png;base64,{logo_base64}" alt="Dashboard Logo">
        </div>
        <div id="clock">00:00:00</div>

        <div class="simulation-container">
            <div class="track"></div>
            
            <div class="train-set train-set-left">
                <div class="train-info">
                    <strong class="train-time" style="color: {mainz_time_color};">{mainz}</strong>
                    <span class="train-direction">Richtung Mainz →</span>
                </div>
                <div class="locomotive loco-front">vlexx</div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="locomotive loco-back">vlexx</div>
            </div>

            <div class="station">
                <div class="station-icon">🏢</div>
                <div class="station-text">Bahnhof Wörrstadt</div>
            </div>

            <div class="train-set train-set-right">
                <div class="train-info">
                    <strong class="train-time" style="color: {alzey_time_color};">{alzey}</strong>
                    <span class="train-direction">← Richtung Alzey</span>
                </div>
                <div class="locomotive loco-front">vlexx</div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="locomotive loco-back">vlexx</div>
            </div>

            <div class="train-set train-set-trailing-left">
                <div class="train-info">
                    <strong>{next_train}</strong>
                    <span class="train-direction">Nächster Zug</span>
                </div>
                <div class="locomotive loco-front">vlexx</div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="locomotive loco-back">vlexx</div>
            </div>

            <div class="train-set train-set-trailing-right">
                <div class="train-info">
                    <strong>{next_train_right}</strong>
                    <span class="train-direction">Nächster Zug</span>
                </div>
                <div class="locomotive loco-front">vlexx</div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="wagon"><div class="window"></div><div class="window"></div><div class="window"></div></div>
                <div class="locomotive loco-back">vlexx</div>
            </div>

        </div>

        <div class="passenger-container">
            <div class="passenger">🚶‍♂️</div>
            <div class="info-box">
                Weg zum Bahnhof: <span style="color: {gehzeit_color};">{gehzeit}</span><br>
                Wetter: <span>{wetter}</span>
            </div>
        </div>
        {fallback_badge_html}
    </div>

    <script>
        function updateClock() {
            const now = new Date();
            let hours = now.getHours().toString().padStart(2, '0');
            let minutes = now.getMinutes().toString().padStart(2, '0');
            let seconds = now.getSeconds().toString().padStart(2, '0');
            document.getElementById('clock').textContent = hours + ':' + minutes + ':' + seconds;
        }

        setInterval(updateClock, 1000);
        updateClock();
    </script>

</body>
</html>
"""
fallback_badge_html = ""
if fallback_badge:
    fallback_badge_html = f"<div class=\"fallback-badge\">{fallback_badge}</div>"


next_train_time = get_next_train_time(mainz_zeit, "mainz")
next_train_time_right = get_next_train_time(alzey_zeit, "alzey")

logo_data = LOGO_BASE64 or ""
final_html = html_code.replace("{gehzeit}", gehzeit_text) \
                      .replace("{gehzeit_color}", gehzeit_color) \
                      .replace("{wetter}", wetter_final_text) \
                      .replace("{alzey}", alzey_zeit) \
                      .replace("{mainz}", mainz_zeit) \
                      .replace("{mainz_time_color}", mainz_time_color) \
                      .replace("{alzey_time_color}", alzey_time_color) \
                      .replace("{catch_status_text}", catch_status_text) \
                      .replace("{catch_status_color}", catch_status_color) \
                      .replace("{next_train}", next_train_time) \
                      .replace("{next_train_right}", next_train_time_right) \
                      .replace("{fallback_badge_html}", fallback_badge_html) \
                      .replace("{logo_base64}", logo_data)

st.components.v1.html(final_html, height=1080)