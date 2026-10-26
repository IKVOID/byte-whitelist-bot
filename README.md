# 🛡️ BYTE CORPORATION — UID Whitelist Bot

---

## 🔒 Role Permission System (Sirf Role Wale Use Kar Sakein)

Aap bot ko restrict kar sakte hain taaki sirf wahi log whitelist kar sakein jinko aapne specific role diya ho:

### Role Set Kaise Karein:
Server Administrator ko sirf ek baar Discord me type karna hoga:
```text
*setrole @RoleName
```
*(ya slash command: `/setrole @RoleName`)*

- **Jiske pass wo role hoga:** Wo panel ke buttons aur whitelist commands use kar payega.
- **Jiske pass wo role NAHI hoga:** Jab bhi wo button dabayega, bot use error dega:
  `❌ Permission Denied: Aapke pass @RoleName role hona zaroori hai.`

---

## 🎨 Panel Style

- Full Name: **`BYTE CORPORATION`** (Har jagah set ho gaya hai).
- `Encrypted Storage` ko footer aur description se **hata diya gaya hai**.
- Panel content vertical box (`>>> `) me packed hai.
- Server ka Blue Dragon Logo top-right thumbnail me display hoga.

---

## ⚡ Commands (Prefix: `*`)

- `*panel` — Control panel send karne ke liye
- `*setrole @Role` — Whitelist permission role set karne ke liye
- `*setlogchannel #channel` — Audit log channel set karne ke liye
- `*adduid <UID> [1/7/30]` — Quick whitelist command
- `*removeuid <UID>` — Quick delete command
