import datetime
import time
from zoneinfo import ZoneInfo

from paas import settings_store
from paas.db import connect

_cache: dict = {"at": 0.0, "tz_name": None, "tz": None}


def get_timezone() -> ZoneInfo:
    now = time.monotonic()
    tz_name = None
    try:
        conn = connect()
        tz_name = settings_store.get_setting(conn, "timezone")
        conn.close()
    except Exception:
        pass
    if not tz_name:
        tz_name = "Asia/Shanghai"
    if _cache["tz"] is None or _cache["tz_name"] != tz_name or now - _cache["at"] > 30:
        _cache["tz"] = ZoneInfo(tz_name)
        _cache["tz_name"] = tz_name
        _cache["at"] = now
    return _cache["tz"]


def now() -> datetime.datetime:
    return datetime.datetime.now(get_timezone())


def today() -> datetime.date:
    return now().date()


def iso_today() -> str:
    return today().isoformat()


def parse_iso_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value)
