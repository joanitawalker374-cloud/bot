"""
Relais Discord <-> code (pour GeeLark autologin Instagram).

Sens 1 (Discord -> GeeLark) : tu postes le code dans le salon, GeeLark le recupere.
    * "joanitawalker374 482910"  -> code range pour ce compte
    * "482910"                   -> code "dernier recu" (un compte a la fois)
  GeeLark appelle :  GET /code?account=<User>
    -> {"code":"482910"} si dispo (efface apres, usage unique) ; sinon {}

Sens 2 (GeeLark -> Discord) : quand IG demande un code, GeeLark previent le salon.
  GeeLark appelle :  GET /notify?account=<User>
    -> le bot poste "Code Instagram demande pour <User>, poste le code ici."

Variables d'environnement :
  DISCORD_TOKEN       = token du bot                              (obligatoire)
  CHANNEL_ID          = salon a ECOUTER (0 = tous les salons)     (defaut 0)
  NOTIFY_CHANNEL_ID   = salon ou POSTER les alertes /notify       (obligatoire pour /notify)
  API_SECRET          = mot de passe optionnel pour /code et /set
  CODE_TTL            = duree de validite d'un code en s          (defaut 300)
  PORT                = port HTTP (fourni par l'hebergeur)
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
API_SECRET = os.environ.get("API_SECRET", "").strip()
CODE_TTL = int(os.environ.get("CODE_TTL", "300"))
PORT = int(os.environ.get("PORT", "8080"))

CODE_RE = re.compile(r"\b(\d{4,8})\b")

store = {}            # { "<account>": (code, timestamp) }  + cle "_last"
store_lock = threading.Lock()


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
        if "_last" in store:
            return store.pop("_last")[0]
    return None


# ------------------------------------------------------------------ Discord
intents = discord.Intents.default()
intents.message_content = True  # activer "Message Content Intent" dans le portail dev
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[discord] connecte: {client.user} | ecoute={CHANNEL_ID} | notify={NOTIFY_CHANNEL_ID}")


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

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
            await message.add_reaction("✅")
        except Exception:
            pass


def run_discord():
    if not DISCORD_TOKEN:
        print("[discord] DISCORD_TOKEN manquant -> bot non demarre")
        return
    client.run(DISCORD_TOKEN)


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
    msg = "🔔 **Instagram demande un code**"
    if account:
        msg += f" pour `{account}`"
    msg += " — poste le code ici (juste les chiffres)."

    if not NOTIFY_CHANNEL_ID:
        return jsonify({"ok": False, "error": "NOTIFY_CHANNEL_ID non configure"}), 400
    if client.loop is None or not client.is_ready():
        return jsonify({"ok": False, "error": "bot pas pret"}), 503

    channel = client.get_channel(NOTIFY_CHANNEL_ID)
    if channel is None:
        return jsonify({"ok": False, "error": "salon introuvable / bot absent du salon"}), 404
    try:
        fut = asyncio.run_coroutine_threadsafe(channel.send(msg), client.loop)
        fut.result(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    threading.Thread(target=run_discord, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
