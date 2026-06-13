"""
Relais Discord -> code (pour GeeLark autologin Instagram).

Principe :
  - Le bot ecoute UN salon Discord (ton salon "ig analyse").
  - Quand tu y postes un code, il le memorise.
        * "joanitawalker374 482910"  -> code range pour ce compte
        * "482910"                   -> code "dernier recu" (sert au prochain login)
  - GeeLark interroge l'URL :  GET /code?account=<User>
        -> renvoie {"code":"482910"} si dispo (et l'efface, usage unique)
        -> renvoie {} si rien (GeeLark continue d'attendre)

Variables d'environnement (a configurer sur l'hebergeur) :
  DISCORD_TOKEN  = le token de ton bot Discord  (obligatoire)
  CHANNEL_ID     = l'ID du salon "ig analyse"   (obligatoire)
  API_SECRET     = un mot de passe optionnel ; si defini, GeeLark doit
                   appeler /code?account=X&secret=CE_MOT_DE_PASSE
  CODE_TTL       = duree de validite d'un code en secondes (defaut 300)
  PORT           = port HTTP (Render le fournit automatiquement)
"""

import os
import re
import time
import threading

import discord
from flask import Flask, request, jsonify

# ------------------------------------------------------------------ config
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or "0")
API_SECRET = os.environ.get("API_SECRET", "").strip()
CODE_TTL = int(os.environ.get("CODE_TTL", "300"))
PORT = int(os.environ.get("PORT", "8080"))

# motif d'un code : 4 a 8 chiffres
CODE_RE = re.compile(r"\b(\d{4,8})\b")

# stockage en memoire : { "<account_minuscule>": (code, timestamp) }
# la cle speciale "_last" garde le dernier code recu sans compte precise
store = {}
store_lock = threading.Lock()


def _save(account, code):
    with store_lock:
        store[account.lower()] = (code, time.time())


def _take(account):
    """Recupere et efface le code pour ce compte (ou le dernier recu)."""
    now = time.time()
    with store_lock:
        # purge des codes expires
        for k in list(store.keys()):
            if now - store[k][1] > CODE_TTL:
                del store[k]
        key = account.lower() if account else None
        if key and key in store:
            code = store.pop(key)[0]
            return code
        if "_last" in store:
            code = store.pop("_last")[0]
            return code
    return None


# ------------------------------------------------------------------ Discord
intents = discord.Intents.default()
intents.message_content = True  # IMPORTANT : activer "Message Content Intent" dans le portail dev
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[discord] connecte en tant que {client.user} | salon cible = {CHANNEL_ID}")


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip()
    parts = content.split()

    # cas "compte code"
    if len(parts) >= 2 and CODE_RE.fullmatch(parts[-1]):
        account = parts[0]
        code = parts[-1]
        _save(account, code)
        with store_lock:
            store["_last"] = (code, time.time())
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        return

    # cas "juste le code"
    m = CODE_RE.search(content)
    if m and len(parts) == 1:
        code = m.group(1)
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
    account = request.args.get("account", "")
    code = _take(account)
    if code:
        return jsonify({"code": code})
    return jsonify({})  # rien encore -> GeeLark reessaiera


@app.get("/set")
def set_code():
    """Fallback manuel sans Discord :  /set?account=X&code=482910"""
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


if __name__ == "__main__":
    threading.Thread(target=run_discord, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
