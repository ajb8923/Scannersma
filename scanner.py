"""Scanner SMA 6/70/200 en velas diarias.
Descarga datos del servicio de gráficos de Yahoo (con yfinance y Stooq de respaldo),
calcula señales, guarda data.json para la app y avisa por Telegram."""
import datetime as dt
import html
import io
import json
import os
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import urllib.error

import pandas as pd

# ---------- Configuración ----------
LB200 = 20          # sesiones para medir la pendiente de la SMA 200
LB70 = 10           # sesiones para medir la pendiente de la SMA 70
SEND_EMPTY = True   # enviar un aviso corto los días sin señales
NAMES = {"^NDX": "Nasdaq 100", "^GSPC": "S&P 500", "^DJI": "Dow Jones", "^IBEX": "IBEX 35"}
STOOQ_IDX = {"^NDX": "^ndx", "^GSPC": "^spx", "^DJI": "^dji"}
CLOSES = {"^IBEX": ("Europe/Madrid", 17, 40)}   # cierre por mercado; por defecto, Wall Street
DEFAULT_CLOSE = ("America/New_York", 16, 5)


def market_closed(sym, now):
    """True si la sesión de hoy de ese mercado ya ha cerrado (tiene en cuenta horario de verano)."""
    tz, hh, mm = CLOSES.get(sym, DEFAULT_CLOSE)
    local = now.astimezone(ZoneInfo(tz))
    return (local.hour, local.minute) >= (hh, mm), local.date()


def read_tickers(path="tickers.txt"):
    out, seen, sec = [], set(), "Otros"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                sec = line[1:-1].strip()
                continue
            for s in line.replace(";", ",").replace(" ", ",").split(","):
                s = s.strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    out.append((s, sec))
    return out


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


def from_yahoo_chart(sym):
    """Llamada directa al servicio de gráficos de Yahoo, igual que la app antigua. Incluye la vela del día."""
    last = ValueError("sin respuesta")
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
               "?range=2y&interval=1d&includePrePost=false")
        for attempt in range(1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                j = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
                chart = j.get("chart") or {}
                res = chart.get("result")
                if not res:
                    raise ValueError((chart.get("error") or {}).get("description", "sin datos"))
                r = res[0]
                ts = r.get("timestamp") or []
                q = r["indicators"]["quote"][0]
                tz = ZoneInfo(r.get("meta", {}).get("exchangeTimezoneName") or "America/New_York")
                idx, rows = [], []
                for k, t in enumerate(ts):
                    c = q["close"][k]
                    if c is None:
                        continue
                    idx.append(pd.Timestamp(dt.datetime.fromtimestamp(t, tz).date()))
                    rows.append((q["open"][k], q["high"][k], q["low"][k], c))
                df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex(idx), dtype="float")
                df = df[~df.index.duplicated(keep="last")]
                if len(df) < 2:
                    raise ValueError("sin datos")
                return df
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 404:
                    raise ValueError("ticker no encontrado en Yahoo")
            except Exception as e:
                last = e
    raise last


def from_yfinance(sym):
    import yfinance as yf
    today = dt.date.today()
    df = yf.Ticker(sym).history(start=(today - dt.timedelta(days=740)).isoformat(),
                                end=(today + dt.timedelta(days=2)).isoformat(),
                                interval="1d", auto_adjust=False, actions=False)
    df = df.dropna(subset=["Close"])
    if len(df) < 2:
        raise ValueError("sin datos en yfinance")
    df.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in df.index])
    return df


def from_stooq(sym):
    s = STOOQ_IDX.get(sym) if sym.startswith("^") else sym.lower().replace(".", "-") + ".us"
    if not s:
        raise ValueError("sin fuente alternativa")
    url = "https://stooq.com/q/d/l/?s=" + urllib.parse.quote(s) + "&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    txt = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")
    if not txt.startswith("Date"):
        raise ValueError("sin datos en Stooq")
    df = pd.read_csv(io.StringIO(txt), parse_dates=["Date"]).set_index("Date").tail(510)
    df = df.dropna(subset=["Close"])
    if len(df) < 2:
        raise ValueError("sin datos en Stooq")
    return df


def rnd(v):
    v = float(v)
    return round(v, 2 if abs(v) >= 10 else 4)


def to_series(df):
    df = df[["Open", "High", "Low", "Close"]].copy()
    for col in ("Open", "High", "Low"):
        df[col] = df[col].fillna(df["Close"])
    t = [int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.timezone.utc).timestamp()) for d in df.index]
    return t, [rnd(x) for x in df["Open"]], [rnd(x) for x in df["High"]], [rnd(x) for x in df["Low"]], [rnd(x) for x in df["Close"]]


def sma(a, p):
    out, s = [None] * len(a), 0.0
    for i, v in enumerate(a):
        s += v
        if i >= p:
            s -= a[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def analyze(c):
    n = len(c)
    s6, s70, s200 = sma(c, 6), sma(c, 70), sma(c, 200)
    sig = [[] for _ in range(n)]
    trades, pos = [], 0
    fL = fS = False
    for i in range(1, n):
        if s70[i] is None or s70[i - 1] is None:
            continue
        ok = s200[i] is not None and i - LB200 >= 0 and s200[i - LB200] is not None and i - LB70 >= 0 and s70[i - LB70] is not None
        fL = ok and s200[i] > s200[i - LB200] and s70[i] > s70[i - LB70] and s70[i] > s200[i]
        fS = ok and s200[i] < s200[i - LB200] and s70[i] < s70[i - LB70] and s70[i] < s200[i]
        up = s6[i - 1] <= s70[i - 1] and s6[i] > s70[i]
        dn = s6[i - 1] >= s70[i - 1] and s6[i] < s70[i]
        if pos == 1 and dn:
            pos = 0; trades[-1]["out"] = i; sig[i].append(("XL", trades[-1]))
        if pos == -1 and up:
            pos = 0; trades[-1]["out"] = i; sig[i].append(("XS", trades[-1]))
        if pos == 0 and up and fL:
            pos = 1; trades.append({"dir": 1, "in": i}); sig[i].append(("EL", trades[-1]))
        if pos == 0 and dn and fS:
            pos = -1; trades.append({"dir": -1, "in": i}); sig[i].append(("ES", trades[-1]))
    return {"s6": s6, "s70": s70, "s200": s200, "sig": sig, "trades": trades, "pos": pos}


def fnum(v):
    s = f"{v:,.2f}" if abs(v) >= 1 else f"{v:,.4f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fpct(v):
    return (f"{v * 100:+.2f}%").replace(".", ",")


def fdate(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%d/%m/%y")


def signal_block(it, a):
    c, t, L = it["c"], it["t"], len(it["c"]) - 1
    s6, s70, s200 = a["s6"][L], a["s70"][L], a["s200"][L]
    name = html.escape(it["name"])
    lines = []
    for kind, tr in a["sig"][L]:
        if kind in ("EL", "ES"):
            ico, txt = ("🟢", "ENTRA LARGO") if kind == "EL" else ("🔴", "ENTRA CORTO")
            closed = [x for x in a["trades"] if "out" in x]
            wins = sum(1 for x in closed if x["dir"] * (c[x["out"]] / c[x["in"]] - 1) > 0)
            lines.append(f"{ico} <b>{name}</b> ({html.escape(it['sec'])}) · <b>{txt}</b>")
            lines.append(f"   Precio de entrada: <b>{fnum(c[L])}</b>")
            lines.append(f"   SMA 6: {fnum(s6)} · SMA 70: {fnum(s70)} · SMA 200: {fnum(s200)}")
            lines.append(f"   Pendiente SMA 200: {fpct(s200 / a['s200'][L - LB200] - 1)} ({LB200} ses.) · SMA 70: {fpct(s70 / a['s70'][L - LB70] - 1)} ({LB70} ses.)")
            lines.append(f"   Distancia a SMA 70: {fpct(c[L] / s70 - 1)} · a SMA 200: {fpct(c[L] / s200 - 1)}")
            lines.append(f"   Salida: cuando la SMA 6 corte la SMA 70 a la {'baja' if kind == 'EL' else 'alza'}")
            if closed:
                lines.append(f"   Histórico 2 años: {len(closed)} operaciones, {round(wins / len(closed) * 100)}% ganadoras")
        else:
            d = tr["dir"]
            res = d * (c[L] / c[tr["in"]] - 1)
            txt = "CIERRA LARGO" if kind == "XL" else "CIERRA CORTO"
            lines.append(f"🟡 <b>{name}</b> ({html.escape(it['sec'])}) · <b>{txt}</b>")
            lines.append(f"   Entrada {fdate(t[tr['in']])} a {fnum(c[tr['in']])} → salida a <b>{fnum(c[L])}</b>")
            lines.append(f"   Resultado: <b>{fpct(res)}</b> en {L - tr['in']} sesiones")
    return "\n".join(lines)


def tg_send(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram no configurado: faltan los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        data = urllib.parse.urlencode({"chat_id": chat, "text": ch, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
            if not j.get("ok"):
                raise RuntimeError(j.get("description", "error de Telegram"))
    return True


def main():
    now = dt.datetime.now(dt.timezone.utc)
    force = os.environ.get("FORCE_SEND", "").lower() == "true"
    prev = {}
    if os.path.exists("data.json"):
        try:
            prev = json.load(open("data.json", encoding="utf-8"))
        except Exception:
            prev = {}

    items, errors = [], []
    stooq_fails = 0
    yahoo_fails = 0
    for sym, sec in read_tickers():
        base = {"s": sym, "sec": sec, "name": NAMES.get(sym, sym)}
        df, src, errs = None, None, []
        for name, fn in (("yahoo", from_yahoo_chart), ("yfinance", from_yfinance), ("stooq", from_stooq)):
            if (name == "stooq" and stooq_fails >= 3) or (name == "yahoo" and yahoo_fails >= 3):
                continue
            try:
                df, src = fn(sym), name
                if name == "stooq":
                    stooq_fails = 0
                if name == "yahoo":
                    yahoo_fails = 0
                break
            except Exception as e:
                errs.append(f"{name}: {e}")
                if name == "stooq":
                    stooq_fails += 1
                if name == "yahoo":
                    yahoo_fails += 1
                    if yahoo_fails == 3:
                        print(f"Yahoo directo no responde ({e}); se usa yfinance para el resto")
        if df is None:
            print(f"{sym}: error ({'; '.join(errs)})")
            errors.append(sym)
            items.append({**base, "err": "Sin datos en Yahoo ni en Stooq. Revisa el ticker."})
            continue
        closed, local_today = market_closed(sym, now)
        last = df.index[-1].date()
        if closed and local_today.weekday() < 5 and last < local_today:
            # Falta la vela de hoy: probar las otras fuentes
            for name, fn in (("yfinance", from_yfinance), ("stooq", from_stooq)):
                if name == src or (name == "stooq" and stooq_fails >= 3):
                    continue
                try:
                    df2 = fn(sym)
                    if df2.index[-1].date() >= local_today:
                        df, src, last = df2, name, df2.index[-1].date()
                        break
                except Exception:
                    if name == "stooq":
                        stooq_fails += 1
        t, o, h, l, c = to_series(df)
        prov = last == local_today and not closed
        grp = CLOSES.get(sym, DEFAULT_CLOSE)[0]
        missing = closed and local_today.weekday() < 5 and last < local_today
        items.append({**base, "src": src, "prov": prov, "grp": grp, "missing": missing, "t": t, "o": o, "h": h, "l": l, "c": c})
        print(f"{sym}: {len(c)} velas, última {last}{' (en curso)' if prov else ''} ({src})")
        time.sleep(0.3)

    ok = [it for it in items if "c" in it]
    last_by_grp = {}
    for it in ok:
        if not it["prov"]:
            last_by_grp[it["grp"]] = max(last_by_grp.get(it["grp"], 0), it["t"][-1])
    main_bar = last_by_grp.get(DEFAULT_CLOSE[0]) or max(last_by_grp.values(), default=None)
    last_key = fdate(main_bar) if main_bar else None
    stale = [it["s"] for it in ok if it.get("missing") or (not it["prov"] and it["t"][-1] < last_by_grp.get(it["grp"], 0))]

    blocks, n_long, n_short = [], 0, 0
    for it in ok:
        a = analyze(it["c"])
        if a["pos"] == 1: n_long += 1
        if a["pos"] == -1: n_short += 1
        if it["prov"] or it["t"][-1] != last_by_grp.get(it["grp"]):
            continue
        if a["sig"][-1]:
            blocks.append(signal_block(it, a))

    data = {
        "app": "Scanner SMA",
        "updated": now.isoformat(timespec="seconds"),
        "params": {"lb200": LB200, "lb70": LB70},
        "lastBar": last_key,
        "sent": prev.get("sent"),
        "tickers": items,
    }

    if last_key and (force or prev.get("sent") != last_key):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        link = ""
        if "/" in repo:
            owner, name = repo.split("/", 1)
            link = f'\n\n<a href="https://{owner.lower()}.github.io/{name}/">Abrir Scanner SMA</a>'
        foot = f"\n\nPosiciones abiertas: {n_long} en largo, {n_short} en corto"
        if errors:
            foot += f"\n⚠️ Sin datos: {', '.join(errors)}"
        if stale:
            foot += f"\n⚠️ Sin la vela de hoy ({len(stale)}): {', '.join(stale[:25])}{'…' if len(stale) > 25 else ''}"
        msg = None
        if blocks:
            msg = f"📊 <b>Scanner SMA</b> · {len(blocks)} señal{'es' if len(blocks) != 1 else ''} del {last_key}\n\n" + "\n\n".join(blocks) + foot + link
        elif SEND_EMPTY:
            msg = f"📊 <b>Scanner SMA</b> · {last_key}\nSin señales hoy." + foot + link
        if msg:
            try:
                if tg_send(msg):
                    data["sent"] = last_key
                    print("Telegram enviado")
            except Exception as e:
                print(f"Error enviando Telegram: {e}")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Listo: {len(ok)} con datos, {len(errors)} con error, {len(stale)} sin la vela de hoy, {len(blocks)} señales. Última vela: {last_key}")


if __name__ == "__main__":
    import builtins, functools
    builtins.print = functools.partial(builtins.print, flush=True)
    main()
