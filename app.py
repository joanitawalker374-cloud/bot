"""
Relais Discord <-> code (pour GeeLark autologin Instagram) - salon partage multi-utilisateurs.

Sens 1 (GeeLark -> Discord) : quand IG demande un code, GeeLark previent le salon.
  GeeLark appelle :  GET /notify?account=<User>
    -> le bot poste "Code demande pour <email> ... reponds avec <email> <code>".
       La personne reconnait SON email et repond.

Sens 2 (Discord -> GeeLark) : la personne poste "<email> <code>" dans le salon.
  GeeLark appelle :  GET /code?account=<User>
    -> {"code":"482910"} si dispo pour CE compte (efface apres) ; sinon {}

Les messages (alerte + code) s'effacent apres AUTODELETE secondes.

IMPORTANT (salon partage) : laisser ALLOW_LAST=0 pour que chaque code soit
servi UNIQUEMENT au compte indique. Sinon les codes de 100 personnes se melangent.

Variables d'environnement :
  DISCORD_TOKEN     = token du bot                              (obligatoire)
  CHANNEL_ID        = salon a ECOUTER (0 = tous)                (defaut 0)
  NOTIFY_CHANNEL_ID = salon ou POSTER les alertes (0 = auto)    (defaut 0)
  ALLOW_LAST        = 1 autorise "juste le code", 0 force "email code"  (defaut 1)
  AUTODELETE        = efface les messages apres X s (0 = jamais)        (defaut 60)
  API_SECRET        = mot de passe optionnel pour /code et /set
  CODE_TTL          = duree de validite d'un code en s                  (defaut 300)
  PORT              = port HTTP (fourni par l'hebergeur)
"""

import os
import re
import time
import asyncio
import threading

import discord
from flask import Flask, request, jsonify

# ------------------------------------------------------------------ config
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or "0")
NOTIFY_CHANNEL_ID = int(os.environ.get("NOTIFY_CHANNEL_ID", "0") or "0")
ALLOW_LAST = os.environ.get("ALLOW_LAST", "1").strip() not in ("0", "false", "False", "")
AUTODELETE = int(os.environ.get("AUTODELETE", "60"))
API_SECRET = os.environ.get("API_SECRET", "").strip()
CODE_TTL = int(os.environ.get("CODE_TTL", "300"))
PORT = int(os.environ.get("PORT", "8080"))

CODE_RE = re.compile(r"\b(\d{4,8})\b")

store = {}                 # { "<account>": (code, timestamp) }  + cle "_last"
store_lock = threading.Lock()
last_channel = {"id": 0}   # dernier salon ou un user a ecrit (auto-cible des alertes)


def _save(account, code):
    with store_lock:
        store[account.lower()] = (code, time.time())


def _take(account):
    now = time.time()
    with store_lock:
        for k in list(store.keys()):
            if now - store[k][1] > CODE_TTL:
                del store[k]
        key = account.lower() if account else None
        if key and key in store:
            return store.pop(key)[0]
        if ALLOW_LAST and "_last" in store:
            return store.pop("_last")[0]
    return None


# ------------------------------------------------------------------ Discord
intents = discord.Intents.default()
intents.message_content = True  # activer "Message Content Intent" dans le portail dev
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[discord] connecte: {client.user} | ecoute={CHANNEL_ID} | notify={NOTIFY_CHANNEL_ID} | allow_last={ALLOW_LAST}")


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    last_channel["id"] = message.channel.id

    content = message.content.strip()
    parts = content.split()

    code = None
    account = None
    if len(parts) >= 2 and CODE_RE.fullmatch(parts[-1]):
        account = parts[0]
        code = parts[-1]
    elif len(parts) == 1:
        m = CODE_RE.fullmatch(parts[0])
        if m:
            code = m.group(1)

    if code:
        if account:
            _save(account, code)
        with store_lock:
            store["_last"] = (code, time.time())
        try:
            await message.add_reaction("✅" if account else "⚠️")
        except Exception:
            pass
        # si pas d'email devant le code en salon partage, prevenir la personne
        if not account and not ALLOW_LAST:
            try:
                warn = await message.channel.send(
                    f"{message.author.mention} mets ton **email** devant le code : `email {code}`",
                )
                if AUTODELETE:
                    await warn.delete(delay=AUTODELETE)
            except Exception:
                pass
        if AUTODELETE:
            try:
                await message.delete(delay=AUTODELETE)
            except Exception:
                pass


def run_discord():
    if not DISCORD_TOKEN:
        print("[discord] DISCORD_TOKEN manquant -> bot non demarre")
        return
    client.run(DISCORD_TOKEN)


def _pick_notify_channel():
    """NOTIFY_CHANNEL_ID, sinon dernier salon utilise, sinon 1er salon ou le bot peut ecrire."""
    if NOTIFY_CHANNEL_ID:
        ch = client.get_channel(NOTIFY_CHANNEL_ID)
        if ch is not None:
            return ch
    if last_channel["id"]:
        ch = client.get_channel(last_channel["id"])
        if ch is not None:
            return ch
    for g in client.guilds:
        me = g.me
        for ch in g.text_channels:
            try:
                if ch.permissions_for(me).send_messages:
                    return ch
            except Exception:
                continue
    return None


# ------------------------------------------------------------------ HTTP API
app = Flask(__name__)


@app.get("/")
def health():
    return "code-relay OK", 200


@app.get("/code")
def get_code():
    if API_SECRET and request.args.get("secret", "") != API_SECRET:
        return jsonify({}), 403
    code = _take(request.args.get("account", ""))
    return jsonify({"code": code} if code else {})


@app.get("/set")
def set_code():
    """Fallback manuel :  /set?account=X&code=482910"""
    if API_SECRET and request.args.get("secret", "") != API_SECRET:
        return jsonify({"ok": False}), 403
    account = request.args.get("account", "_last")
    code = request.args.get("code", "")
    if not CODE_RE.fullmatch(code or ""):
        return jsonify({"ok": False, "error": "code invalide"}), 400
    _save(account, code)
    with store_lock:
        store["_last"] = (code, time.time())
    return jsonify({"ok": True})


@app.get("/notify")
def notify():
    """GeeLark previent qu'un code est demande -> poste un message dans le salon."""
    account = request.args.get("account", "").strip()

    if account:
        msg = (
            f"🔔 **Code Instagram demandé pour** `{account}`\n"
            f"👉 Si c'est ton compte, réponds ici avec : `{account} TONCODE`"
        )
    else:
        msg = "🔔 **Un code Instagram est demandé** — réponds avec `email code`."

    if client.loop is None or not client.is_ready():
        return jsonify({"ok": False, "error": "bot pas pret"}), 503

    channel = _pick_notify_channel()
    if channel is None:
        return jsonify({"ok": False, "error": "aucun salon ou poster (droit d'envoyer des messages manquant, ou poste d'abord un message dans le salon)"}), 404

    try:
        coro = channel.send(msg, delete_after=(AUTODELETE or None))
        fut = asyncio.run_coroutine_threadsafe(coro, client.loop)
        fut.result(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    threading.Thread(target=run_discord, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
