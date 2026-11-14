"""Lecture d'un agenda au format iCal (.ics), par ex. l'« adresse secrète au format iCal »
de Google Agenda, et calcul des événements d'une période (répétitions comprises).

Sans dépendance : gère les répétitions courantes (quotidiennes, hebdomadaires avec jours,
mensuelles, annuelles, INTERVAL, COUNT, UNTIL), les exceptions (EXDATE), les occurrences
modifiées (RECURRENCE-ID), les événements annulés et les fuseaux horaires.
"""
import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_STEPS = 20000


def local_tz():
    return datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------- lecture

def unfold(text):
    """Les lignes longues du format iCal sont coupées et continuent par une espace."""
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def parse_line(line):
    """'DTSTART;TZID=Europe/Paris:20261116T093000' -> ('DTSTART', {'TZID': ...}, '2026...')"""
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    p = {}
    for item in params:
        k, _, v = item.partition("=")
        p[k.upper()] = v.strip('"')
    return name.upper(), p, value


def unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def parse_dt(value, params, tz):
    """Date ou date-heure iCal -> date (journée entière) ou datetime avec fuseau."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", value)
    if not m:
        raise ValueError(f"date iCal invalide : {value!r}")
    y, mo, d, h, mi, s, z = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    if z:
        return dt.replace(tzinfo=timezone.utc)
    zone = None
    if params.get("TZID") and ZoneInfo:
        try:
            zone = ZoneInfo(params["TZID"])
        except Exception:
            zone = None
    return dt.replace(tzinfo=zone or tz)


def parse_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(0)
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                   minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def parse_events(text, tz=None):
    """Liste des VEVENT sous forme de dictionnaires."""
    tz = tz or local_tz()
    events, cur = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": [], "rrule": None}
        elif line == "END:VEVENT":
            if cur is not None and "start" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, params, value = parse_line(line)
            try:
                if name == "DTSTART":
                    cur["start"] = parse_dt(value, params, tz)
                elif name == "DTEND":
                    cur["end"] = parse_dt(value, params, tz)
                elif name == "DURATION":
                    cur["duration"] = parse_duration(value)
                elif name == "SUMMARY":
                    cur["title"] = unescape(value)
                elif name == "LOCATION":
                    cur["location"] = unescape(value)
                elif name == "UID":
                    cur["uid"] = value
                elif name == "STATUS":
                    cur["status"] = value.upper()
                elif name == "RRULE":
                    cur["rrule"] = dict(kv.split("=", 1) for kv in value.split(";") if "=" in kv)
                elif name == "EXDATE":
                    cur["exdates"] += [parse_dt(v, params, tz) for v in value.split(",") if v]
                elif name == "RECURRENCE-ID":
                    cur["recurrence_id"] = parse_dt(value, params, tz)
            except ValueError:
                pass  # ligne illisible : on l'ignore plutôt que de tout perdre
    return events


# --------------------------------------------------------------------------- répétitions

def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return y, m


def _nth_weekday(y, m, wd, n):
    """n-ième jour wd (0=lundi) du mois ; n<0 compte depuis la fin."""
    if n > 0:
        first = date(y, m, 1)
        d = first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
        return d if d.month == m else None
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    d = last - timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == m else None


def _combine(day, start):
    """Même heure que le premier événement, à la date day."""
    if isinstance(start, datetime):
        return start.replace(year=day.year, month=day.month, day=day.day)
    return day


def occurrences(ev, range_start, range_end, skip_before=None):
    """Débuts des occurrences de ev qui touchent la période [range_start, range_end).

    skip_before (date) : les occurrences antérieures sont comptées (pour COUNT) mais pas
    calculées en détail, ce qui accélère beaucoup les vieilles répétitions."""
    start = ev["start"]
    rule = ev.get("rrule")
    if not rule:
        yield start
        return
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = None
    if rule.get("UNTIL"):
        until = parse_dt(rule["UNTIL"], {}, start.tzinfo if isinstance(start, datetime) else None)
        if isinstance(until, date) and not isinstance(until, datetime) and isinstance(start, datetime):
            until = datetime.combine(until, datetime.max.time()).replace(tzinfo=start.tzinfo)
        elif isinstance(until, datetime) and not isinstance(start, datetime):
            until = until.date()
    byday = [d for d in rule.get("BYDAY", "").split(",") if d]
    bymonthday = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x]
    d0 = start.date() if isinstance(start, datetime) else start

    def candidates():
        if freq == "DAILY":
            for i in range(MAX_STEPS):
                yield d0 + timedelta(days=i * interval)
        elif freq == "WEEKLY":
            days = sorted({DAYS.index(d[-2:]) for d in byday}) or [d0.weekday()]
            week = d0 - timedelta(days=d0.weekday())
            for i in range(MAX_STEPS):
                for wd in days:
                    yield week + timedelta(days=7 * i * interval + wd)
        elif freq == "MONTHLY":
            for i in range(MAX_STEPS):
                y, m = _add_months(d0, i * interval)
                found = []
                if byday:
                    for item in byday:
                        mm = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item)
                        if mm and mm.group(1):
                            d = _nth_weekday(y, m, DAYS.index(mm.group(2)), int(mm.group(1)))
                            if d:
                                found.append(d)
                for md in bymonthday or ([] if byday else [d0.day]):
                    try:
                        found.append(date(y, m, md) if md > 0 else
                                     date(y + (m == 12), m % 12 + 1, 1) + timedelta(days=md))
                    except ValueError:
                        pass  # 31 dans un mois de 30 jours : pas d'occurrence
                yield from sorted(found)
        elif freq == "YEARLY":
            for i in range(MAX_STEPS):
                try:
                    yield date(d0.year + i * interval, d0.month, d0.day)
                except ValueError:
                    pass  # 29 février
        else:
            yield d0

    n = 0
    for day in candidates():
        if day < d0:
            continue
        if skip_before is not None and day < skip_before:
            n += 1
            if count is not None and n > count:
                return
            if until is not None and _key(day) > _key(until):
                return
            continue
        occ = _combine(day, start)
        if until is not None and occ > until:
            return
        n += 1
        if count is not None and n > count:
            return
        if _key(occ) >= _key(range_end):
            return
        yield occ


def _key(x):
    """Comparer dates et date-heures entre elles."""
    if isinstance(x, datetime):
        return x.astimezone(timezone.utc).replace(tzinfo=None)
    return datetime(x.year, x.month, x.day)


def events_between(text, range_start, range_end, tz=None):
    """Événements (répétitions comprises) qui touchent [range_start, range_end),
    triés, avec des heures dans le fuseau local."""
    tz = tz or local_tz()
    events = parse_events(text, tz)
    overridden = {}
    for ev in events:
        if "recurrence_id" in ev and ev.get("uid"):
            overridden.setdefault(ev["uid"], set()).add(_key(ev["recurrence_id"]))
    out = []
    for ev in events:
        if ev.get("status") == "CANCELLED":
            continue
        start = ev["start"]
        if "end" in ev:
            length = ev["end"] - start
        elif "duration" in ev:
            length = ev["duration"]
        else:
            length = timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)
        skip = {_key(x) for x in ev["exdates"]}
        if "recurrence_id" not in ev:
            skip |= overridden.get(ev.get("uid"), set())
        first = range_start.date() if isinstance(range_start, datetime) else range_start
        skip_before = first - timedelta(days=max(0, length.days) + 2)
        for occ in occurrences(ev, range_start, range_end, skip_before):
            if _key(occ) in skip:
                continue
            end = occ + length
            if _key(end) <= _key(range_start) and _key(occ) < _key(range_start):
                continue
            all_day = not isinstance(occ, datetime)
            out.append({
                "title": ev.get("title", "(sans titre)"),
                "location": ev.get("location", ""),
                "all_day": all_day,
                "start": occ.isoformat() if all_day else occ.astimezone(tz).isoformat(),
                "end": end.isoformat() if all_day else end.astimezone(tz).isoformat(),
            })
    out.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    return out


def week_bounds(today=None, weeks=1):
    """Lundi 00:00 de la semaine en cours et le lundi suivant (ou + weeks semaines)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    tz = local_tz()
    start = datetime.combine(monday, datetime.min.time()).replace(tzinfo=tz)
    return start, start + timedelta(days=7 * weeks)
