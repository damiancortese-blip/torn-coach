#!/usr/bin/env python3
"""
torn_coach.py - asistente de SOLO LECTURA para Torn.

Mira tu cuenta con la API oficial cada ~1 minuto y te avisa por Telegram
cuando conviene actuar (entrenar, tomar Xanax, usar el refill, ir a rehab).
NO hace clics ni acciones en el juego: la accion final la haces vos.
(La API de Torn es de solo lectura y las reglas de scripting prohiben automatizar acciones.)

Solo usa la libreria estandar de Python 3.8+. Sin dependencias.

SETUP RAPIDO
1) En Torn: Settings > API Key > crea una key de acceso "Limited" (alcanza para
   bars, cooldowns, battlestats, personalstats y refills). Nunca la compartas.
2) En Telegram: habla con @BotFather, crea un bot y copia el token.
   Escribile algo a tu bot y abri https://api.telegram.org/bot<TOKEN>/getUpdates
   para ver tu chat id.
   (Telegram es lo mas confiable. Podes usar uno, otro o ambos.)
2b) WhatsApp (opcional, via CallMeBot, servicio gratuito de uso personal):
   agrega el numero del bot que figura en https://www.callmebot.com/blog/free-api-whatsapp-messages/
   a tus contactos, mandale "I allow callmebot to send me messages" por WhatsApp y
   espera que te responda con tu APIKEY.
3) Variables de entorno:
     export TORN_API_KEY=xxxxxxxxxxxxxxxx
     export TELEGRAM_BOT_TOKEN=123456:ABC...
     export TELEGRAM_CHAT_ID=123456789
     export CALLMEBOT_PHONE=+54911XXXXXXXX     # tu numero con codigo de pais
     export CALLMEBOT_APIKEY=123456
4) Probar sin key:   python3 torn_coach.py --demo
   Una sola pasada:  python3 torn_coach.py --once
   Dejarlo andando:  nohup python3 torn_coach.py >> coach.log 2>&1 &

OPCIONALES (variables de entorno)
  POLL_SECONDS=60        cada cuanto mira (minimo 30)
  TRAIN_STAT=strength    que stat te recuerda entrenar
  TARGET_STAT_VALUE=0    meta para el ETA (0 = el stat mas alto que tengas)
  DIGEST_HOUR_UTC=12     hora del resumen diario (12 UTC = 09:00 Buenos Aires)
  REFILL_HOUR_UTC=21     hora del recordatorio del refill de puntos (21 UTC = 18:00 BA)
  TOLERATION=0           perk de facción de tolerancia: 0, 0.25, 0.5 (reduce adiccion)
  REHAB_AP_ALERT=105     adiccion estimada a partir de la cual avisa del rehab
  ALERT_BOOSTER=1        avisar cuando termina el cooldown de boosters
  STATE_FILE=~/.torn_coach_state.json
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("TORN_API_BASE", "https://api.torn.com")
CALLMEBOT_BASE = os.environ.get("CALLMEBOT_BASE", "https://api.callmebot.com")
# Si la key es invalida, parar de inmediato (Torn banea IPs que insisten con keys malas).
FATAL_API_CODES = {2, 10, 13, 16, 18}

XANAX_ENERGY = 250
XANAX_AP = 35          # puntos de adiccion por Xanax (sin tolerancia)
AP_DECAY_PER_DAY = 20  # decaimiento natural diario
ENERGY_CAP = 1000


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__("Torn API error %s: %s" % (code, msg))
        self.code = code


# ----------------------------------------------------------------- utilidades
def fmt(n):
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return "%.2f%s" % (n / div, suf)
    return "%d" % n


def fmt_secs(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m = r // 60
    return "%dh%02dm" % (h, m)


def load_config():
    env = os.environ
    return {
        "key": env.get("TORN_API_KEY", ""),
        "tg_token": env.get("TELEGRAM_BOT_TOKEN", ""),
        "tg_chat": env.get("TELEGRAM_CHAT_ID", ""),
        "cmb_phone": env.get("CALLMEBOT_PHONE", ""),
        "cmb_key": env.get("CALLMEBOT_APIKEY", ""),
        "poll": max(30, int(env.get("POLL_SECONDS", "60"))),
        "train_stat": env.get("TRAIN_STAT", "strength").lower(),
        "target": float(env.get("TARGET_STAT_VALUE", "0")),
        "digest_hour": int(env.get("DIGEST_HOUR_UTC", "12")),
        "refill_hour": int(env.get("REFILL_HOUR_UTC", "21")),
        "toleration": float(env.get("TOLERATION", "0")),
        "rehab_ap": float(env.get("REHAB_AP_ALERT", "105")),
        "alert_booster": env.get("ALERT_BOOSTER", "1") not in ("0", "false", ""),
        "ap_set": float(env.get("AP_SET") or "-1"),
        "state_file": os.path.expanduser(env.get("STATE_FILE", "~/.torn_coach_state.json")),
    }


def api(selections, key, extra=None):
    q = {"selections": selections, "key": key}
    if extra:
        q.update(extra)
    url = "%s/user/?%s" % (API_BASE, urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers={"User-Agent": "torn-coach/1.0 (read-only)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise ApiError(err.get("code"), err.get("error"))
    return data


def notify(cfg, text):
    """Manda el aviso a todos los canales configurados (Telegram y/o WhatsApp)."""
    sent = False
    if cfg["tg_token"] and cfg["tg_chat"]:
        url = "https://api.telegram.org/bot%s/sendMessage" % cfg["tg_token"]
        body = urllib.parse.urlencode({"chat_id": cfg["tg_chat"], "text": text}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20).read()
            sent = True
        except Exception as exc:  # que un canal caiga no tumba el bot
            print("No pude mandar a Telegram:", exc, file=sys.stderr)
    if cfg["cmb_phone"] and cfg["cmb_key"]:
        q = urllib.parse.urlencode({"phone": cfg["cmb_phone"], "apikey": cfg["cmb_key"], "text": text})
        try:
            urllib.request.urlopen(urllib.request.Request(
                "%s/whatsapp.php?%s" % (CALLMEBOT_BASE, q), headers={"User-Agent": "torn-coach/1.0"}), timeout=30).read()
            sent = True
        except Exception as exc:
            print("No pude mandar a WhatsApp:", exc, file=sys.stderr)
    if not sent:
        print("[aviso]", text.replace("\n", " | "))


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


# ----------------------------------------------------------------- logica
def gym_notes(s):
    """Gyms especializados que ya podrias desbloquear (requisitos de la wiki)."""
    notes = []
    st, sp, de, dx = s["strength"], s["speed"], s["defense"], s["dexterity"]
    ordered = sorted(((v, k) for k, v in (("strength", st), ("speed", sp), ("defense", de), ("dexterity", dx))), reverse=True)
    top_v, top_k = ordered[0]
    second_v = ordered[1][0]
    single = {"strength": "Gym 3000 (Str 8.0)", "defense": "Mr. Isoyamas (Def 8.0)",
              "speed": "Total Rebound (Spd 8.0)", "dexterity": "Elites (Dex 8.0)"}
    if second_v > 0 and top_v >= 1.25 * second_v:
        notes.append("Cumplis requisito de %s (cuesta $100M, 50E por train)" % single[top_k])
    if st + sp >= 1.25 * (dx + de):
        notes.append("Cumplis requisito de Frontline Fitness (Str/Spd 7.5)")
    if de + dx >= 1.25 * (st + sp):
        notes.append("Cumplis requisito de Balboas (Def/Dex 7.5)")
    return notes


def rate_per_day(history, stat):
    """Ganancia absoluta por dia del stat usando el historial guardado."""
    pts = [(h["t"], h[stat]) for h in history if stat in h]
    if len(pts) < 2:
        return None
    t0, v0 = pts[0]
    t1, v1 = pts[-1]
    if t1 - t0 < 6 * 3600:
        return None
    return (v1 - v0) / ((t1 - t0) / 86400.0)


def update_ap(st, now):
    last = st.get("ap_ts", now)
    st["ap"] = max(0.0, st.get("ap", 0.0) - AP_DECAY_PER_DAY * (now - last) / 86400.0)
    st["ap_ts"] = now


def build_digest(cfg, st, stats, now):
    lines = ["Resumen diario Torn"]
    for k, label in (("strength", "Fuerza"), ("speed", "Velocidad"), ("defense", "Defensa"), ("dexterity", "Destreza")):
        lines.append("%s: %s" % (label, fmt(stats[k])))
    lines.append("Total: %s" % fmt(sum(stats[k] for k in ("strength", "speed", "defense", "dexterity"))))
    hist = st.get("history", [])
    day_ago = [h for h in hist if now - h["t"] >= 20 * 3600]
    if day_ago:
        ref = min(day_ago, key=lambda h: abs((now - h["t"]) - 86400))
        d = {k: stats[k] - ref.get(k, stats[k]) for k in ("strength", "speed", "defense", "dexterity")}
        lines.append("Cambio en las ultimas %dh: Fza +%s, Vel +%s, Def +%s, Dex +%s" % (
            (now - ref["t"]) / 3600, fmt(d["strength"]), fmt(d["speed"]), fmt(d["defense"]), fmt(d["dexterity"])))
    ts = cfg["train_stat"] if cfg["train_stat"] in stats else "strength"
    target = cfg["target"] or max(stats[k] for k in ("strength", "speed", "defense", "dexterity"))
    r = rate_per_day(hist, ts)
    if r and r > 0 and stats[ts] < target:
        days = (target - stats[ts]) / r
        lines.append("Ritmo en %s: +%s/dia. A este ritmo llegas a %s en ~%d dias." % (ts, fmt(r), fmt(target), days))
    ps = st.get("ps", {})
    if ps:
        used = ps.get("xantaken", 0) + ps.get("exttaken", 0)
        lines.append("Xanax+Extasis de por vida: %d (Sports Science Lab pide 150 o menos)" % used)
        lines.append("Adiccion estimada: ~%d AP (estimacion)" % st.get("ap", 0))
    for n in gym_notes(stats):
        lines.append(n)
    return "\n".join(lines)


def process(cfg, st, bars, cds, stats, now, ps=None, refills=None, travel=None):
    """Devuelve la lista de avisos. Recibe datos ya leidos, asi se puede probar sin red."""
    alerts = []
    flags = st.setdefault("flags", {})
    e = bars["energy"]
    cur, mx = int(e["current"]), int(e["maximum"])
    drug = int(cds.get("drug", 0))
    booster = int(cds.get("booster", 0))
    ts = cfg["train_stat"]

    # 1) energia llena
    if cur >= mx:
        if not flags.get("energy_full"):
            alerts.append("Energia llena (%d/%d). Entrena %s ahora: gastala antes de tomar Xanax." % (cur, mx, ts))
            flags["energy_full"] = True
    else:
        flags["energy_full"] = False

    # 2) cooldown de drogas
    if drug == 0:
        if not flags.get("drug_ready"):
            msg = "Cooldown de drogas en 0: ya podes tomar Xanax (+%dE)." % XANAX_ENERGY
            if cur + XANAX_ENERGY > ENERGY_CAP:
                msg += " Ojo: tenes %dE y el tope es %d; entrena antes o desperdicias energia." % (cur, ENERGY_CAP)
            else:
                msg += " Tip: entrena primero la energia que tenes (%dE) para perder menos si hay sobredosis." % cur
            alerts.append(msg)
            flags["drug_ready"] = True
    else:
        flags["drug_ready"] = False

    # 3) cooldown de boosters
    if cfg["alert_booster"]:
        if booster == 0:
            if not flags.get("booster_ready"):
                alerts.append("Cooldown de boosters en 0: podes usar latas/FHC.")
                flags["booster_ready"] = True
        else:
            flags["booster_ready"] = False

    # 4) snapshot de stats (cada >=3h)
    hist = st.setdefault("history", [])
    if not hist or now - hist[-1]["t"] >= 3 * 3600:
        hist.append({"t": now, "strength": stats["strength"], "speed": stats["speed"],
                     "defense": stats["defense"], "dexterity": stats["dexterity"]})
        del hist[:-500]

    # 5) personalstats: Xanax, sobredosis, rehab
    if cfg["ap_set"] >= 0 and st.get("ap_set_applied") != cfg["ap_set"]:
        st["ap"] = cfg["ap_set"]
        st["ap_ts"] = now
        st["ap_set_applied"] = cfg["ap_set"]
        flags["rehab_alerted"] = False
        alerts.append("Adiccion ajustada a ~%d AP segun tu dato." % st["ap"])
    if ps is not None:
        update_ap(st, now)
        prev = st.get("ps")
        if prev is not None:
            dx = ps.get("xantaken", 0) - prev.get("xantaken", 0)
            if dx > 0:
                st["ap"] = st.get("ap", 0.0) + dx * XANAX_AP * (1.0 - cfg["toleration"])
                alerts.append("Tomaste %d Xanax. Mientras dure el efecto tus stats de pelea estan -35%%: no pelees ni te dejes atacar. Adiccion estimada ~%d AP." % (dx, st["ap"]))
            if ps.get("overdosed", 0) > prev.get("overdosed", 0):
                alerts.append("SOBREDOSIS detectada. Revisa tu energia y el hospital.")
            if ps.get("rehabs", 0) > prev.get("rehabs", 0):
                st["ap"] = 0.0
                flags["rehab_alerted"] = False
                alerts.append("Rehab registrado: reinicio la estimacion de adiccion a 0.")
        st["ps"] = {k: ps.get(k, 0) for k in ("xantaken", "exttaken", "overdosed", "rehabs")}
        if st.get("ap", 0) >= cfg["rehab_ap"]:
            if not flags.get("rehab_alerted"):
                alerts.append("Adiccion estimada ~%d AP (es una estimacion, confirma en el icono de drogas del juego). Evalua ir a rehab a Suiza; cada rehab nuevo sale mas caro, no hagas varios clics seguidos." % st["ap"])
                flags["rehab_alerted"] = True
        elif st.get("ap", 0) < cfg["rehab_ap"] * 0.5:
            flags["rehab_alerted"] = False

    # 5b) viajes: avisa al aterrizar
    if travel is not None:
        left = int(travel.get("time_left", 0) or 0)
        dest = travel.get("destination", "")
        if left > 0:
            flags["flying_to"] = dest
        elif flags.get("flying_to"):
            alerts.append("Aterrizaste en %s." % flags["flying_to"])
            flags["flying_to"] = ""

    # 6) refill de puntos (una vez por dia, se resetea 00:00 TCT = UTC)
    utc = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    today = utc.strftime("%Y-%m-%d")
    if refills is not None and utc.hour >= cfg["refill_hour"]:
        if not refills.get("energy_refill_used") and st.get("refill_reminded") != today:
            alerts.append("Todavia no usaste el refill de energia de hoy (se resetea a las 00:00 TCT). Usalo cuando tengas la barra baja.")
            st["refill_reminded"] = today

    # 7) resumen diario
    if utc.hour >= cfg["digest_hour"] and st.get("digest_day") != today:
        alerts.append(build_digest(cfg, st, stats, now))
        st["digest_day"] = today
    return alerts


def poll(cfg, st, now=None):
    now = now or time.time()
    d = api("bars,cooldowns,battlestats,travel", cfg["key"])
    bars = {"energy": d["energy"]}
    cds = d.get("cooldowns", {})
    stats = {k: float(d[k]) for k in ("strength", "speed", "defense", "dexterity")}
    ps = refills = None
    if now - st.get("ps_check", 0) >= 240:
        p = api("personalstats", cfg["key"], {"stat": "xantaken,exttaken,overdosed,rehabs"})
        ps = p.get("personalstats", {})
        st["ps_check"] = now
    if now - st.get("refill_check", 0) >= 840:
        refills = api("refills", cfg["key"]).get("refills", {})
        st["refill_check"] = now
    travel = d.get("travel")
    return process(cfg, st, bars, cds, stats, now, ps, refills, travel), stats


def run(cfg, once=False):
    if not cfg["key"]:
        sys.exit("Falta TORN_API_KEY (o proba con --demo).")
    st = load_state(cfg["state_file"])
    backoff = 0
    while True:
        try:
            alerts, _ = poll(cfg, st)
            for a in alerts:
                notify(cfg, a)
            save_state(cfg["state_file"], st)
            backoff = 0
        except ApiError as exc:
            print(exc, file=sys.stderr)
            if exc.code in FATAL_API_CODES:
                notify(cfg, "El bot se detuvo: la API key no es valida (%s)." % exc)
                sys.exit(1)
            backoff = min(600, (backoff or 30) * 2)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print("Error de red/datos:", exc, file=sys.stderr)
            backoff = min(600, (backoff or 30) * 2)
        if once:
            return
        time.sleep(cfg["poll"] + backoff)


def demo(cfg):
    """Corre la logica con datos inventados para ver como se ven los avisos."""
    cfg = dict(cfg, state_file=os.devnull)
    st = {}
    now = dt.datetime(2026, 11, 20, 13, 0, tzinfo=dt.timezone.utc).timestamp()
    stats = {"strength": 363165494.0, "speed": 1220653323.0, "defense": 487249475.0, "dexterity": 1001062985.0}
    st["history"] = [{"t": now - 5 * 86400, "strength": 358000000.0, "speed": 1220000000.0, "defense": 487249475.0, "dexterity": 1001062985.0}]
    st["ps"] = {"xantaken": 10, "exttaken": 2, "overdosed": 0, "rehabs": 0}
    ps = {"xantaken": 11, "exttaken": 2, "overdosed": 0, "rehabs": 0}
    for a in process(cfg, st, {"energy": {"current": 150, "maximum": 150}}, {"drug": 0, "booster": 0},
                     stats, now, ps, {"energy_refill_used": False}):
        print(a, "\n")


def main():
    ap = argparse.ArgumentParser(description="Asistente de solo lectura para Torn")
    ap.add_argument("--once", action="store_true", help="una sola pasada")
    ap.add_argument("--demo", action="store_true", help="avisos de ejemplo sin key")
    args = ap.parse_args()
    cfg = load_config()
    if args.demo:
        demo(cfg)
    else:
        run(cfg, once=args.once)


if __name__ == "__main__":
    main()
