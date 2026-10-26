# 🛡️ BYTE CORPORATION — UID Whitelist Bot (24/7 Cloud Edition)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/IKVOID/byte-whitelist-bot)

---

## ⚡ 1-Click 24/7 Free Cloud Hosting (Render.com)

Aap is bot ko **Render.com** par 100% Free aur 24/7 online chala sakte hain:

1. Click karein **[Deploy to Render](https://render.com/deploy?repo=https://github.com/IKVOID/byte-whitelist-bot)** button par.
2. Render aapke repository ke `render.yaml` ko automatically load kar lega.
3. Sirf ye 2 values fill karein:
   - `DISCORD_TOKEN`: Aapka bot token
   - `API_KEY`: Aapka backend API key
4. Click **Apply / Deploy**!

Render automatic Python 3 environment setup karega aur bot live ho jayega.

---

## 🕒 Zero-Sleep Architecture (24/7 Hamesha Online)

- **`render_entry.py`**: Lightweight `aiohttp` web server chalata hai jo Render ke HTTP port ko active rakhta hai.
- **Auto-Restart Supervisor**: Agar bot gateway se disconnect ho jaye toh 5 seconds ke andar automatically reconnect ho jata hai.
- **GitHub Actions Ping**: Har 10 minute me GitHub Actions cron workflow Render service ko ping karta hai taaki Free Tier dyno kabhi na soye.

---

## 🔒 Role Permission System

Sirf wahi log panel aur commands use kar sakein jinko specific role diya gaya ho:

```text
*setrole @RoleName
```
*(ya slash command: `/setrole @RoleName`)*

- **Jiske pass role hoga:** Panel buttons aur commands access kar payega.
- **Jiske pass role nahi hoga:** Error aayega `Permission Denied`.

---

## ⚡ Commands (Prefix: `*`)

- `*panel` — Control panel send karne ke liye
- `*setrole @Role` — Whitelist permission role set karne ke liye
- `*setlogchannel #channel` — Audit log channel set karne ke liye
- `*adduid <UID> [1/7/30]` — Quick whitelist command
- `*removeuid <UID>` — Quick delete command
