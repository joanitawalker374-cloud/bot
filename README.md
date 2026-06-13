# Bot relais Discord → code (pour GeeLark autologin)

Ce bot fait le pont entre ton salon Discord **ig analyse** et GeeLark.
Tu postes le code reçu par mail dans Discord, GeeLark va le chercher tout seul.

---

## 1. Créer le bot Discord (5 min)

1. Va sur https://discord.com/developers/applications → **New Application** → donne un nom.
2. Onglet **Bot** → **Add Bot** → **Reset Token** → copie le **TOKEN** (garde-le secret).
3. Toujours dans **Bot**, active **MESSAGE CONTENT INTENT** (interrupteur). **Obligatoire** sinon le bot ne lit pas les messages.
4. Onglet **OAuth2 → URL Generator** : coche `bot`, puis dans les permissions coche
   **Read Messages/View Channels** et **Read Message History**. Copie l'URL générée,
   ouvre-la, et invite le bot sur ton serveur.

### Récupérer l'ID du salon "ig analyse"
- Dans Discord : Paramètres → Avancés → active **Mode développeur**.
- Clic droit sur le salon **ig analyse** → **Copier l'identifiant du salon**. C'est ton `CHANNEL_ID`.

---

## 2. Héberger gratuitement sur Render.com

1. Mets le dossier `code_relay_bot` sur un dépôt GitHub (ou utilise "Deploy from local").
2. Sur https://render.com → **New → Web Service** → connecte le repo.
3. Réglages :
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `python app.py`
   - **Instance Type** : Free
4. **Environment → Add Environment Variable** :
   - `DISCORD_TOKEN` = le token copié à l'étape 1
   - `CHANNEL_ID` = l'ID du salon ig analyse
   - `API_SECRET` = (optionnel) un mot de passe au choix
5. **Create Web Service**. Render te donne une URL publique du type
   `https://ton-bot.onrender.com`.

> Astuce : le plan gratuit Render "s'endort" après 15 min sans trafic et met ~30 s à
> se réveiller. La boucle GeeLark (30 essais × 10 s) laisse largement le temps.

---

## 3. Brancher GeeLark

Dans la tâche **autologin** (fichier `autologin_simple_emailcode.json`), au lancement,
remplis le paramètre :

- `CodeApiUrl` = `https://ton-bot.onrender.com/code`
  - si tu as mis un `API_SECRET`, utilise plutôt
    `https://ton-bot.onrender.com/code?secret=TON_SECRET` (le flow rajoute `&account=...`).

> ⚠️ L'URL doit être **publique** (celle de Render). Pas `127.0.0.1`, GeeLark tourne
> dans le cloud et ne pourrait pas l'atteindre.

---

## 4. Utilisation au quotidien

1. Tu lances la tâche GeeLark sur un compte.
2. Si Instagram demande le code email, tu vois le code (sur ton webmail).
3. Tu le postes dans le salon **ig analyse** :
   - soit `joanitawalker374 482910` (recommandé si plusieurs comptes en parallèle),
   - soit juste `482910`.
   Le bot met un ✅ quand il a capté le code.
4. Dans les ~10 s, GeeLark récupère le code, le saisit et termine le login.

### Test rapide sans Discord
Tu peux vérifier que le relais marche en ouvrant dans ton navigateur :
`https://ton-bot.onrender.com/set?account=test&code=123456`
puis `https://ton-bot.onrender.com/code?account=test` → doit renvoyer `{"code":"123456"}`.

---

## Notes
- Un code n'est servi **qu'une fois** puis effacé, et il expire au bout de 5 min
  (réglable via la variable `CODE_TTL`).
- Si tu postes "juste le code", il est donné au prochain login qui en demande :
  fais-le **un compte à la fois** pour éviter les mélanges. Le format
  `compte code` est plus sûr en parallèle.
