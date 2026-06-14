"""
Bot tout-en-un : relais code Discord (autologin) + lecteur Gmail multi-boites (creation)
+ panneau Discord avec BOUTON pour ajouter (email, app_password) au Google Sheet.

ENDPOINTS HTTP
  GET /                       health
  GET /code?account=<x>       (autologin) renvoie le code poste dans Discord ; sinon {}
  GET /gmailcode?to=<email>   (creation) lit la boite <email> via la LISTE et renvoie
                              le dernier code Instagram ; sinon {}
  GET /set?account=&code=     fallback manuel
  GET /notify?account=<x>     poste une alerte dans le salon

DISCORD
  - Salon "ajout mail" (PANEL_CHANNEL_ID) : un message avec un BOUTON
    "➕ Ajouter un mail" -> ouvre un formulaire (email + app password) ->
    ajoute au Google Sheet -> confirmation privee (visible par toi seul).
  - Aussi en texte :  addmail email@gmail.com abcd efgh ijkl mnop

LISTE email->app_password (pour /gmailcode), par priorite :
  1. GMAIL_ACCOUNTS_JSON : {"email":"app_password", ...}
  2. GMAIL_ACCOUNTS_URL  : URL Apps Script (doGet=CSV, doPost=ajout)
  3. GMAIL_USER + GMAIL_APP_PASSWORD : fallback 1 compte.

VARIABLES D'ENVIRONNEMENT
  DISCORD_TOKEN      token du bot                                   (obligatoire Discord)
  PANEL_CHANNEL_ID   salon ou poster le bouton d'ajout             (defaut 1515650040646074388)
  CHANNEL_ID         salon a ECOUTER pour /code (0 = tous)         (defaut 0)
  NOTIFY_CHANNEL_ID  salon ou POSTER les alertes /notify (0 = auto)(defaut 0)
  ALLOW_LAST 1/0 (defaut 1)    AUTODELETE secondes (defaut 60)
  GMAIL_ACCOUNTS_URL URL Apps Script (lecture CSV + ajout)
  GMAIL_ACCOUNTS_JSON {"email":"app_password",...}  (alternative)
  GMAIL_USER / GMAIL_APP_PASSWORD  fallback 1 compte
  SHEET_SECRET       secret partage avec l'Apps Script (optionnel)
  API_SECRET         protege /code /set /gmailcode (optionnel)
  PORT               fourni par l'hebergeur
"""

import os
import io
import re
import csv
import json
import time
import email
import imaplib
import asyncio
import threading
from datetime import datetime, timedelta
from email.header import decode_header

import requests
import discord
from flask import Flask, request, jsonify

# ------------------------------------------------------------------ config
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
PANEL_CHANNEL_ID = int(os.environ.get("PANEL_CHANNEL_ID", "1515650040646074388") or "0")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or "0")
NOTIFY_CHANNEL_ID = int(os.environ.get("NOTIFY_CHANNEL_ID", "0") or "0")
ALLOW_LAST = os.environ.get("ALLOW_LAST", "1").strip() not in ("0", "false", "False", "")
AUTODELETE = int(os.environ.get("AUTODELETE", "60"))
GMAIL_ACCOUNTS_URL = os.environ.get("GMAIL_ACCOUNTS_URL", "").strip()
GMAIL_ACCOUNTS_JSON = os.environ.get("GMAIL_ACCOUNTS_JSON", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
SHEET_SECRET = os.environ.get("SHEET_SECRET", "").strip()
API_SECRET = os.environ.get("API_SECRET", "").strip()
CODE_TTL = int(os.environ.get("CODE_TTL", "300"))
PORT = int(os.environ.get("PORT", "8080"))

CODE_RE = re.compile(r"\b(\d{4,8})\b")
IG_CODE_RE = re.compile(r"\b(\d{6})\b")
PANEL_MARKER = "panel:addmail"

store = {}
store_lock = threading.Lock()
last_channel = {"id": 0}


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


# ------------------------------------------------------------------ liste Gmail
_creds_cache = {"data": {}, "ts": 0.0}


def load_creds(force=False):
    now = time.time()
    if not force and _creds_cache["data"] and now - _creds_cache["ts"] < 60:
        return _creds_cache["data"]
    data = {}
    if GMAIL_ACCOUNTS_JSON:
        try:
            for k, v in json.loads(GMAIL_ACCOUNTS_JSON).items():
                data[k.strip().lower()] = str(v).replace(" ", "").strip()
        except Exception as e:
            print("[gmail] GMAIL_ACCOUNTS_JSON invalide:", e)
    if GMAIL_ACCOUNTS_URL:
        try:
            r = requests.get(GMAIL_ACCOUNTS_URL, timeout=15)
            r.raise_for_status()
            for row in csv.reader(io.StringIO(r.text)):
                if len(row) >= 2:
                    em = row[0].strip().lower()
                    pw = row[1].replace(" ", "").strip()
                    if "@" in em and pw:
                        data[em] = pw
        except Exception as e:
            print("[gmail] lecture GMAIL_ACCOUNTS_URL echouee:", e)
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        data.setdefault(GMAIL_USER.lower(), GMAIL_APP_PASSWORD)
    _creds_cache["data"] = data
    _creds_cache["ts"] = now
    return data


def get_app_password(email_addr):
    return load_creds().get((email_addr or "").strip().lower())


def sheet_add(email_addr, app_password):
    """Ajoute (email, app_password) au Google Sheet via l'Apps Script (POST)."""
    if not GMAIL_ACCOUNTS_URL:
        return False, "GMAIL_ACCOUNTS_URL non configure"
    try:
        payload = {"email": email_addr, "app_password": app_password}
        if SHEET_SECRET:
            payload["secret"] = SHEET_SECRET
        r = requests.post(GMAIL_ACCOUNTS_URL, json=payload, timeout=20)
        try:
            ok = bool(r.json().get("ok"))
        except Exception:
            ok = r.ok
        load_creds(force=True)
        return ok, ("" if ok else f"reponse {r.status_code}")
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------------ Gmail IMAP
def _hdr(value):
    out = ""
    for part, enc in decode_header(value or ""):
        out += part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part
    return out


def _body_text(msg):
    if msg.is_multipart():
        for ct in ("text/plain", "text/html"):
            for part in msg.walk():
                if part.get_content_type() == ct:
                    try:
                        return part.get_payload(decode=True).decode("utf-8", "ignore")
                    except Exception:
                        pass
        return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "ignore")
    except Exception:
        return msg.get_payload() or ""


def gmail_latest_code(user, app_password, since_min):
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(user, app_password)
        M.select("INBOX")
        since = (datetime.utcnow() - timedelta(minutes=since_min)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split() if data and data[0] else []
        for num in reversed(ids):
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            frm = _hdr(msg.get("From", "")).lower()
            subj = _hdr(msg.get("Subject", ""))
            if "instagram" not in frm and "instagram" not in subj.lower():
                continue
            m = IG_CODE_RE.search(subj) or IG_CODE_RE.search(_body_text(msg))
            if m:
                return m.group(1)
        return None
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ------------------------------------------------------------------ Discord UI
class AddMailModal(discord.ui.Modal, title="Ajouter un mail"):
    email_in = discord.ui.TextInput(
        label="Adresse Gmail",
        placeholder="compte@gmail.com",
        required=True, max_length=80)
    pw_in = discord.ui.TextInput(
        label="Mot de passe d'application (16 caractères)",
        placeholder="abcd efgh ijkl mnop",
        required=True, max_length=40)

    async def on_submit(self, interaction: discord.Interaction):
        em = str(self.email_in).strip()
        pw = str(self.pw_in).strip()
        if "@" not in em or "." not in em:
            await interaction.response.send_message("❌ Email invalide.", ephemeral=True)
            return
        if len(pw.replace(" ", "")) < 12:
            await interaction.response.send_message(
                "❌ Le mot de passe d'application fait 16 caractères (pas ton mot de passe normal).",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, err = await asyncio.get_event_loop().run_in_executor(None, sheet_add, em, pw)
        if ok:
            await interaction.followup.send(f"✅ `{em}` ajouté à la liste.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Échec de l'ajout : {err}", ephemeral=True)


class AddMailView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Ajouter un mail", style=discord.ButtonStyle.primary,
                       custom_id="addmail_button")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddMailModal())


def panel_embed():
    e = discord.Embed(
        title="📩 Ajouter un mail (création Instagram)",
        description=(
            "Ce salon sert à enregistrer les **emails Gmail** utilisés pour la création de comptes.\n\n"
            "**Comment faire :**\n"
            "1. Clique sur le bouton **➕ Ajouter un mail** ci-dessous.\n"
            "2. Entre l'**adresse Gmail** et son **mot de passe d'application** (16 caractères, "
            "généré dans Sécurité Google → Mots de passe des applications — ce n'est PAS ton mot de passe normal).\n"
            "3. Valide : le mail est ajouté automatiquement et le robot pourra lire ses codes.\n\n"
            "🔒 Ce que tu tapes dans le formulaire est **privé** (personne d'autre ne le voit)."),
        color=0x3BA55D)
    e.set_footer(text=PANEL_MARKER)
    return e


# ------------------------------------------------------------------ Discord client
intents = discord.Intents.default()
intents.message_content = True


class Bot(discord.Client):
    async def setup_hook(self):
        self.add_view(AddMailView())  # bouton persistant (marche apres redemarrage)


client = Bot(intents=intents)


async def ensure_panel():
    if not PANEL_CHANNEL_ID:
        return
    ch = client.get_channel(PANEL_CHANNEL_ID)
    if ch is None:
        return
    try:
        async for msg in ch.history(limit=30):
            if msg.author == client.user and msg.embeds and (msg.embeds[0].footer.text == PANEL_MARKER):
                return  # panneau deja present
        await ch.send(embed=panel_embed(), view=AddMailView())
    except Exception as e:
        print("[panel] impossible de poster le panneau:", e)


@client.event
async def on_ready():
    print(f"[discord] connecte: {client.user} | panel={PANEL_CHANNEL_ID}")
    await ensure_panel()


@client.event
async def on_message(message):
    if message.author.bot:
        return

    # commande texte: addmail email app_password
    if message.content.strip().lower().startswith("addmail"):
        parts = message.content.split()
        if len(parts) >= 3:
            em = parts[1].strip()
            pw = " ".join(parts[2:]).strip()
            ok, err = await asyncio.get_event_loop().run_in_executor(None, sheet_add, em, pw)
            try:
                await message.add_reaction("✅" if ok else "❌")
            except Exception:
                pass
            try:
                await message.delete(delay=5)
            except Exception:
                pass
        return

    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return
    last_channel["id"] = message.channel.id
    parts = message.content.strip().split()
    code = None
    account = None
    if len(parts) >= 2 and CODE_RE.fullmatch(parts[-1]):
        account, code = parts[0], parts[-1]
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
        if AUTODELETE:
            try:
                await message.delete(delay=AUTODELETE)
            except Exception:
                pass


def run_discord():
    if not DISCORD_TOKEN:
        print("[discord] DISCORD_TOKEN manquant -> Discord off (Gmail reste dispo)")
        return
    client.run(DISCORD_TOKEN)


def _pick_notify_channel():
    if NOTIFY_CHANNEL_ID:
        ch = client.get_channel(NOTIFY_CHANNEL_ID)
        if ch is not None:
            return ch
    if last_channel["id"]:
        ch = client.get_channel(last_channel["id"])
        if ch is not None:
            return ch
    for g in client.guilds:
        for ch in g.text_channels:
            try:
                if ch.permissions_for(g.me).send_messages:
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


@app.get("/gmailcode")
def gmail_code():
    if API_SECRET and request.args.get("secret", "") != API_SECRET:
        return jsonify({}), 403
    addr = request.args.get("to", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "parametre 'to' manquant"}), 400
    pwd = get_app_password(addr)
    if not pwd:
        return jsonify({"ok": False, "error": f"{addr} absent de la liste"}), 404
    try:
        since_min = int(request.args.get("since", "15"))
    except ValueError:
        since_min = 15
    try:
        code = gmail_latest_code(addr, pwd, since_min)
        return jsonify({"code": code} if code else {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/set")
def set_code():
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
    account = request.args.get("account", "").strip()
    if account:
        msg = (f"🔔 **Code Instagram demandé pour** `{account}`\n"
               f"👉 Réponds ici avec : `{account} TONCODE`")
    else:
        msg = "🔔 **Un code Instagram est demandé** — réponds avec `email code`."
    if client.loop is None or not client.is_ready():
        return jsonify({"ok": False, "error": "bot pas pret"}), 503
    channel = _pick_notify_channel()
    if channel is None:
        return jsonify({"ok": False, "error": "aucun salon ou poster"}), 404
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
