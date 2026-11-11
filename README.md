# FinSight

A personal-finance web app built with Flask, Jinja2, SQLite and Chart.js.

**Live site:** https://myfinsight.pythonanywhere.com
It runs on a free hosting plan, so it may be offline for short periods. Please use fake data only.

## Features
- Dashboard with income, spending by category, monthly trend and budget progress
- Bills (one payment row per month), money to receive, and goals ("dreams")
- Loan tracker with EMI history and a rough prepayment estimate
- Document vault protected by a PIN, with files encrypted on disk
- Light and dark mode

## Security notes
- Money is stored in whole paise (integers), never floats
- Every query is filtered by user and parameterised
- Hand-written CSRF protection on all forms
- Login delay after repeated wrong passwords
- Vault files are checked by content, stored under random names and encrypted (Fernet)

## Run it locally (Windows PowerShell)

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

The app needs two environment variables and will not start without them.
Generate them once:

```
python -c "import secrets; print(secrets.token_hex(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set them, then start the app:

```
$env:FINSIGHT_SECRET_KEY="paste-the-first-value"
$env:FINSIGHT_VAULT_KEY="paste-the-second-value"
python app.py
```

Open http://127.0.0.1:5000. The database is created on first start.
Optional: set `$env:FINSIGHT_DEBUG="1"` for debug mode.

Keep the vault key safe and never change it, or existing vault files cannot be opened.

## Known limits
- The login delay resets when the server restarts
- Sessions are signed cookies, so changing a password does not log out other browsers
- The loan prepayment simulator is a rough estimate
- Only vault files are encrypted, not the database
- There is no "forgot password" (a safe version needs email or OTP)

## Use fake data only
This is a learning project. Please do not enter real financial details or real documents.
The database is not encrypted and the site owner can read stored data, so use made-up names, amounts and files only.