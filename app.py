import calendar
import io
import os
import re
import secrets
import time
import uuid

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
from datetime import date, datetime, timedelta

from database import connect
from init_db import init_db
from money import to_paise, to_rupees, format_inr, pct, div_round

app = Flask(__name__)
app.secret_key = os.environ.get("FINSIGHT_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("Set the FINSIGHT_SECRET_KEY environment variable before starting FinSight.")

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FINSIGHT_HTTPS") == "1"
# Vault files are encrypted before they are saved. This key is the ONLY way to read them.
# BACK IT UP (for example in a password manager). If it is lost, the files are gone for good.
# Create it once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# It must stay the same every time the app starts.
vault_key = os.environ.get("FINSIGHT_VAULT_KEY")
if not vault_key:
    raise RuntimeError("Set the FINSIGHT_VAULT_KEY environment variable before starting FinSight.")
vault_cipher = Fernet(vault_key)

# Create any table that is missing (for example the new monthly_budgets table).
# It only adds, it never deletes or changes your data.
init_db(verbose=False)


def get_db():
    return connect()


# ---------------------------------------------------------------------------
# Month helpers. A month is written as text like '2026-10'.
# ---------------------------------------------------------------------------

def month_bounds(month_str):
    """'2026-10' -> ('2026-10-01', '2026-11-01').

    Used as: date >= start AND date < end. A date range is exact and lets the
    database use its date index, unlike matching text with LIKE '2026-10%'.
    """
    year, month = int(month_str[:4]), int(month_str[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def current_month():
    return date.today().strftime("%Y-%m")


def parse_month(value):
    """Return a safe 'YYYY-MM' text. Anything odd becomes the current month."""
    try:
        if len(value) == 7 and value[4] == "-":
            year, month = int(value[:4]), int(value[5:7])
            if 2000 <= year <= 2100 and 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
    except (TypeError, ValueError):
        pass
    return current_month()


def shift_month(month_str, delta):
    """'2026-10', -1 -> '2026-09'. Works across years."""
    index = int(month_str[:4]) * 12 + (int(month_str[5:7]) - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(month_str):
    """'2026-09' -> 'September 2026'."""
    return date(int(month_str[:4]), int(month_str[5:7]), 1).strftime("%B %Y")


def short_month_label(month_str):
    """'2026-09' -> "Sep '26" (used on the trend chart)."""
    return date(int(month_str[:4]), int(month_str[5:7]), 1).strftime("%b '%y")


def days_in_month(month_str):
    return calendar.monthrange(int(month_str[:4]), int(month_str[5:7]))[1]


def pick_month(value):
    """The month a page should show: the one asked for, but never in the future."""
    return min(parse_month(value), current_month())


def available_months(conn, user_id, include=()):
    """Months the user can open: every month with an entry, plus this month."""
    rows = conn.execute(
        """SELECT substr(date, 1, 7) AS m FROM expenses WHERE user_id=?
           UNION
           SELECT substr(date, 1, 7) AS m FROM income WHERE user_id=?""",
        (user_id, user_id)
    ).fetchall()
    this_month = current_month()
    values = {r["m"] for r in rows
              if r["m"] and parse_month(r["m"]) == r["m"] and r["m"] <= this_month}
    values.add(this_month)
    values.update(include)
    return values


def previous_period(month):
    """What to compare a month with.

    Returns (previous month, its first day, the day after its last day, and the
    day after the 'same point' in that month). For the month in progress, the
    same point means the same number of days, so a half month is not compared
    with a whole one.
    """
    prev_month = shift_month(month, -1)
    prev_start, prev_end = month_bounds(prev_month)
    if month == current_month():
        days = min(date.today().day, days_in_month(prev_month))
        point_end = (date.fromisoformat(prev_start) + timedelta(days=days)).isoformat()
    else:
        point_end = prev_end
    return prev_month, prev_start, prev_end, point_end


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def total_between(conn, table, user_id, start, end):
    """Sum of a table's amounts for one user in [start, end). table is income or expenses."""
    return conn.execute(
        f"SELECT COALESCE(SUM(amount),0) AS total FROM {table} "
        "WHERE user_id=? AND date>=? AND date<?",
        (user_id, start, end)
    ).fetchone()["total"]


def category_totals(conn, user_id, start, end):
    """{category: total paise}, biggest first."""
    rows = conn.execute(
        """SELECT category, SUM(amount) AS total FROM expenses
           WHERE user_id=? AND date>=? AND date<?
           GROUP BY category ORDER BY total DESC""",
        (user_id, start, end)
    ).fetchall()
    return {r["category"]: r["total"] for r in rows}


def share_color(share):
    """Colour by how big a slice of the spending a category is (not by its rank)."""
    if share >= 40:
        return "red"
    if share >= 20:
        return "amber"
    return "green"


def compare_phrase(spent, before, same_point):
    """'Rs 1,200 less than last month (12% lower)', or None if there is nothing to compare."""
    if before <= 0:
        return None
    when = "the same point last month" if same_point else "last month"
    diff = spent - before
    if diff == 0:
        return f"the same as {when}"
    change = pct(abs(diff), before)
    if diff > 0:
        return f"₹{format_inr(diff)} more than {when} ({change}% higher)"
    return f"₹{format_inr(-diff)} less than {when} ({change}% lower)"


def comparison_rows(current, before):
    """Rows for the category comparison table. current and before are {category: paise}."""
    names = list(current) + [n for n in before if n not in current]
    rows = []
    for name in names:
        cur, prev = current.get(name, 0), before.get(name, 0)
        rows.append({"name": name, "cur": cur, "prev": prev, "diff": cur - prev})
    rows.sort(key=lambda r: max(r["cur"], r["prev"]), reverse=True)
    return rows


def get_budget(conn, user_id, month):
    """The budget that applies to a month.

    kind 'set'     = the user saved a budget for exactly this month
    kind 'carried' = none saved for this month, so the latest earlier one is used
    kind 'none'    = no budget at all yet
    """
    row = conn.execute(
        "SELECT monthly_limit FROM monthly_budgets WHERE user_id=? AND month=?",
        (user_id, month)
    ).fetchone()
    if row:
        return {"limit": row["monthly_limit"], "kind": "set", "from_label": month_label(month)}

    row = conn.execute(
        """SELECT month, monthly_limit FROM monthly_budgets
           WHERE user_id=? AND month<? ORDER BY month DESC LIMIT 1""",
        (user_id, month)
    ).fetchone()
    if row:
        return {"limit": row["monthly_limit"], "kind": "carried", "from_label": month_label(row["month"])}

    # The single budget from before budgets became per-month.
    row = conn.execute(
        "SELECT monthly_limit FROM budgets WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    if row:
        return {"limit": row["monthly_limit"], "kind": "carried", "from_label": "your earlier budget"}

    return {"limit": 0, "kind": "none", "from_label": None}


def goal_money_in_month(conn, user_id, start, end):
    """Total put into goals during a month."""
    return conn.execute(
        """SELECT COALESCE(SUM(c.amount), 0) AS total
           FROM contributions c JOIN goals g ON g.id = c.goal_id
           WHERE g.user_id=? AND c.date>=? AND c.date<?""",
        (user_id, start, end)
    ).fetchone()["total"]


def goal_pct(current, target):
    """Whole-number percent done, rounded DOWN, so 99.6% never shows as 100%."""
    if target <= 0:
        return 0
    return min(100, max(0, current * 100 // target))


# ---------------------------------------------------------------------------
# Dates typed by the user
# ---------------------------------------------------------------------------

def check_date(value):
    """Check a date typed by the user.

    Returns (date, None) for a normal date, (date, warning) for an odd one
    (in the future, or more than a year ago), and (None, None) if it is not a date.
    """
    try:
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            return None, None
        when = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    today = date.today()
    if when > today:
        return when, "This date is in the future, so it will not show in any month until then."
    if when < today - timedelta(days=365):
        return when, "This date is more than a year ago."
    return when, None


def date_gate(value, form):
    """Decide what to do with a date. Returns (error, warning).

    A warning is shown once; saving again with the same date goes through.
    """
    when, warning = check_date(value)
    if when is None:
        return "Please enter a valid date.", None
    if warning and form.get("confirmed_date") == value:
        return None, None
    return None, warning


# ---------------------------------------------------------------------------
# Bills: one row per bill per month in bill_payments, so bills reset each month
# ---------------------------------------------------------------------------

def ensure_month_bills(conn, user_id, month):
    """Make sure every bill has a row for this month (created as unpaid)."""
    bills = conn.execute(
        "SELECT id, amount, due_date, status FROM bills WHERE user_id=?", (user_id,)
    ).fetchall()
    for b in bills:
        first_month = (b["due_date"] or "")[:7]
        if parse_month(first_month) != first_month or first_month > month:
            continue                                   # odd date, or the bill starts later
        exists = conn.execute(
            "SELECT 1 FROM bill_payments WHERE bill_id=? AND month=?", (b["id"], month)
        ).fetchone()
        if exists:
            continue
        try:
            day = int(b["due_date"][8:10])
        except ValueError:
            day = 1
        due = date(int(month[:4]), int(month[5:7]), min(day, days_in_month(month))).isoformat()
        # A bill saved as paid before this change keeps that status in its own month only.
        is_paid = 1 if (first_month == month and b["status"] == "paid") else 0
        conn.execute(
            "INSERT INTO bill_payments (bill_id, month, amount, due_date, is_paid) VALUES (?,?,?,?,?)",
            (b["id"], month, b["amount"], due, is_paid)
        )
    conn.commit()


def bill_status(is_paid, due_date_text):
    """(text, colour) shown on a bill card."""
    if is_paid:
        return "Paid", "green"
    try:
        days_left = (date.fromisoformat(due_date_text) - date.today()).days
    except (TypeError, ValueError):
        return "Unpaid", "amber"
    if days_left < 0:
        return "Overdue", "red"
    if days_left == 0:
        return "Due today", "red"
    if days_left == 1:
        return "Due tomorrow", "red"
    return f"{days_left} days left", "amber"


# ---------------------------------------------------------------------------
# Template filters and shared page data
# ---------------------------------------------------------------------------

# All money in the database is whole paise; this filter shows it as rupees.
app.jinja_env.filters['inr'] = format_inr


def format_day(value):
    """'2026-09-15' -> '15 Sep 2026'."""
    try:
        return date.fromisoformat(value).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return value or "-"


app.jinja_env.filters['dmy'] = format_day


@app.context_processor
def inject_header_info():
    """Header text: the logged-in name and the current month."""
    return {
        "current_username": session.get("username"),
        "current_month_label": date.today().strftime("%B %Y"),
    }
    

def csrf_token():
    """One random token per browser session. Every form sends it back."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


# Lets any template write {{ csrf_token() }}.
app.jinja_env.globals["csrf_token"] = csrf_token


@app.after_request
def security_headers(response):
    # Only our own pages may put our pages in a frame. The vault viewer needs this.
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


@app.before_request
def check_csrf_token():
    """Every POST must bring back the token that was put in its form."""
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        saved = session.get("csrf_token", "")
        if not saved or not secrets.compare_digest(sent.encode(), saved.encode()):
            abort(400)

# ---------------------------------------------------------------------------
# Error pages: a missing page or a crash shows a normal FinSight page
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def bad_request(error):
    return render_template(
        "error.html", active_page="dashboard",
        title="Request could not be verified",
        text="This form is out of date or came from another site. Go back, refresh the page and try again."
    ), 400


@app.errorhandler(404)
def page_not_found(error):
    return render_template(
        "error.html", active_page="dashboard",
        title="Page not found",
        text="That page does not exist, or it may have moved."
    ), 404


@app.errorhandler(500)
def server_error(error):
    return render_template(
        "error.html", active_page="dashboard",
        title="Something went wrong",
        text="Something broke on our side. Your data is safe. Please try again."
    ), 500


@app.route("/")
def home():
    return render_template(
        "home.html",
        active_page="home"
    )


# Wrong passwords per username: {"name": (wrong tries, time the next try is allowed)}.
# Kept in memory on purpose: it also covers usernames that do not exist, so the
# delay never reveals which usernames are real. It resets when the server restarts.
login_failures = {}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    wait = 0

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        key = username.lower()

        failed, retry_at = login_failures.get(key, (0, 0))
        if time.time() > retry_at + 15 * 60:      # 15 quiet minutes: start counting again
            failed = 0
        wait = int(retry_at - time.time()) + 1 if retry_at > time.time() else 0

        if wait > 0:
            error = f"Too many wrong tries. Please wait {wait} second{'s' if wait != 1 else ''}."
        else:
            conn = get_db()
            user = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,)
            ).fetchone()
            conn.close()

            if user and check_password_hash(user["password_hash"], password):
                login_failures.pop(key, None)
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("dashboard"))

            # The first 4 wrong tries are free. From the 5th: 5s, 10s, 15s ... up to 30s.
            failed += 1
            delay = min((failed - 4) * 5, 30) if failed >= 5 else 0
            login_failures[key] = (failed, time.time() + delay)
            error = "Invalid username or password."
            if delay:
                wait = delay
                error += f" Please wait {delay} second{'s' if delay != 1 else ''} before trying again."

    return render_template(
        "login.html",
        active_page="login",
        error=error,
        wait=wait
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]

    # Which month is on screen: /dashboard?month=2026-09 (default: this month).
    this_month = current_month()
    month = pick_month(request.args.get("month"))
    is_current_month = (month == this_month)
    month_start, month_end = month_bounds(month)
    prev_month, prev_start, prev_end, prev_point_end = previous_period(month)

    # Month list and arrows.
    month_values = available_months(conn, user_id, include={month})
    months = [{"value": m, "label": month_label(m)} for m in sorted(month_values, reverse=True)]
    prev_arrow = shift_month(month, -1) if month > min(month_values) else None
    next_arrow = shift_month(month, 1) if month < this_month else None

    # Snapshot numbers.
    income = total_between(conn, "income", user_id, month_start, month_end)
    expenses = total_between(conn, "expenses", user_id, month_start, month_end)
    saved = income - expenses
    goal_added = goal_money_in_month(conn, user_id, month_start, month_end)

    budget = get_budget(conn, user_id, month)
    budget_limit = budget["limit"]
    budget_used_pct = pct(expenses, budget_limit)

    # Categories (colour = share of spending).
    cat_now = category_totals(conn, user_id, month_start, month_end)
    categories = []
    for name, total in cat_now.items():
        share = pct(total, expenses)
        categories.append({
            "name": name,
            "amount": total,                   # paise, shown with |inr
            "chart_amount": to_rupees(total),  # rupees, for the chart
            "share": share,
            "color": share_color(share),
        })

    # Category comparison: last month (whole) against this month.
    cat_prev = category_totals(conn, user_id, prev_start, prev_end)
    compare_rows = comparison_rows(cat_now, cat_prev)
    prev_total = sum(cat_prev.values())

    # Insight line: top category plus a real comparison with last month.
    if categories:
        insight = f"{categories[0]['name']} is your top spend {'this month' if is_current_month else 'in ' + month_label(month)}"
        before_point = total_between(conn, "expenses", user_id, prev_start, prev_point_end)
        phrase = compare_phrase(expenses, before_point, same_point=is_current_month)
        if phrase:
            insight += f" · you {'have spent' if is_current_month else 'spent'} {phrase}"
    else:
        insight = f"No spending logged {'yet this month' if is_current_month else 'in ' + month_label(month)}"

    # Banner for last month's report (first week of a month).
    report_banner = None
    if is_current_month and date.today().day <= 7:
        had_data = (total_between(conn, "income", user_id, prev_start, prev_end)
                    + total_between(conn, "expenses", user_id, prev_start, prev_end))
        if had_data > 0:
            report_banner = {"month": prev_month, "label": month_label(prev_month)}

    # Goals: weighted progress = total saved / total target (each goal capped at its target).
    goal_rows = conn.execute("SELECT * FROM goals WHERE user_id=?", (user_id,)).fetchall()
    total_target = sum(g["target_amount"] for g in goal_rows)
    total_saved = sum(min(g["current_amount"], g["target_amount"]) for g in goal_rows)
    goal_progress_pct = goal_pct(total_saved, total_target)
    goals = [
        {"name": g["name"], "pct": goal_pct(g["current_amount"], g["target_amount"])}
        for g in goal_rows if not g["is_achieved"]
    ]
    goals_achieved = len(goal_rows) - len(goals)

    # Money to receive.
    receivable_rows = conn.execute(
        """SELECT id, person_name, amount, status, lent_date, received_date, interest, notes
           FROM lending WHERE user_id=? ORDER BY status ASC, id DESC""",
        (user_id,)
    ).fetchall()
    receivables = [
        {
            "id": r["id"],
            "person": r["person_name"],
            "amount": r["amount"],
            "status": r["status"],
            "lent_date": r["lent_date"],
            "received_date": r["received_date"],
            "interest_text": f"{r['interest']:g}%" if r["interest"] else "None",
            "notes": r["notes"],
        }
        for r in receivable_rows
    ]
    pending_total = sum(r["amount"] for r in receivables if r["status"] == "pending")

    # Bills for this month (each bill has one row per month).
    if is_current_month:
        ensure_month_bills(conn, user_id, month)
    bill_rows = conn.execute(
        """SELECT bp.id AS payment_id, b.id AS bill_id, b.name, bp.amount, bp.due_date, bp.is_paid,
                  pp.amount AS last_amount
           FROM bill_payments bp
           JOIN bills b ON b.id = bp.bill_id
           LEFT JOIN bill_payments pp ON pp.bill_id = bp.bill_id AND pp.month = ?
           WHERE b.user_id=? AND bp.month=?
           ORDER BY bp.is_paid ASC, bp.due_date ASC""",
        (prev_month, user_id, month)
    ).fetchall()
    bills = []
    for b in bill_rows:
        text, color = bill_status(b["is_paid"], b["due_date"])
        bills.append({
            "payment_id": b["payment_id"],
            "bill_id": b["bill_id"],
            "name": b["name"],
            "amount": b["amount"],
            "due_date": b["due_date"],
            "last_amount": b["last_amount"],
            "is_paid": bool(b["is_paid"]),
            "status_text": text,
            "status_color": color,
        })

    # Trend: the 6 months ending at the month on screen. Empty months show as 0.
    trend_months = [shift_month(month, i) for i in range(-5, 1)]
    trend_rows = conn.execute(
        """SELECT strftime('%Y-%m', date) AS m, SUM(amount) AS total
           FROM expenses
           WHERE user_id=? AND date>=? AND date<?
           GROUP BY m""",
        (user_id, month_bounds(trend_months[0])[0], month_end)
    ).fetchall()
    by_month = {r["m"]: r["total"] for r in trend_rows}
    trend_labels = [short_month_label(m) for m in trend_months]
    trend_values = [to_rupees(by_month.get(m, 0)) for m in trend_months]
    conn.close()

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        month=month,
        month_name=month_label(month),
        is_current_month=is_current_month,
        months=months,
        prev_arrow=prev_arrow,
        next_arrow=next_arrow,
        prev_name=month_label(prev_month),
        report_banner=report_banner,
        insight=insight,
        income=income,
        expenses=expenses,
        saved=saved,
        goal_added=goal_added,
        budget_limit=budget_limit,
        budget_kind=budget["kind"],
        budget_from=budget["from_label"],
        budget_used_pct=budget_used_pct,
        categories=categories,
        compare_rows=compare_rows,
        prev_total=prev_total,
        goals=goals,
        goals_achieved=goals_achieved,
        goals_total=len(goal_rows),
        goal_progress_pct=goal_progress_pct,
        receivables=receivables,
        pending_total=pending_total,
        bills=bills,
        trend_labels=trend_labels,
        trend_values=trend_values
    )


# ---------------------------------------------------------------------------
# Monthly report: one page per month
# ---------------------------------------------------------------------------

@app.route("/report/<month_str>")
def report(month_str):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]
    this_month = current_month()
    month = pick_month(month_str)
    is_current_month = (month == this_month)
    month_start, month_end = month_bounds(month)
    prev_month, prev_start, prev_end, prev_point_end = previous_period(month)

    income = total_between(conn, "income", user_id, month_start, month_end)
    expenses = total_between(conn, "expenses", user_id, month_start, month_end)
    saved = income - expenses
    goal_added = goal_money_in_month(conn, user_id, month_start, month_end)
    budget = get_budget(conn, user_id, month)
    budget_limit = budget["limit"]
    budget_used_pct = pct(expenses, budget_limit)

    cat_now = category_totals(conn, user_id, month_start, month_end)
    cat_prev = category_totals(conn, user_id, prev_start, prev_end)
    rows = comparison_rows(cat_now, cat_prev)
    for r in rows:
        r["share"] = pct(r["cur"], expenses)

    bill_counts = conn.execute(
        """SELECT COUNT(*) AS total, COALESCE(SUM(bp.is_paid), 0) AS paid
           FROM bill_payments bp JOIN bills b ON b.id = bp.bill_id
           WHERE b.user_id=? AND bp.month=?""",
        (user_id, month)
    ).fetchone()

    before_point = total_between(conn, "expenses", user_id, prev_start, prev_point_end)
    month_values = available_months(conn, user_id, include={month})
    prev_link = shift_month(month, -1) if month > min(month_values) else None
    next_link = shift_month(month, 1) if month < this_month else None
    conn.close()

    # A plain-words summary, built from fixed rules (no guessing).
    lines = [f"You earned ₹{format_inr(income)} and spent ₹{format_inr(expenses)}."]
    if saved > 0:
        rate = pct(saved, income)
        lines.append(f"That leaves ₹{format_inr(saved)} saved ({rate}% of your income).")
    elif saved < 0:
        lines.append(f"You spent ₹{format_inr(-saved)} more than you earned.")
    elif income > 0:
        lines.append("You spent exactly what you earned.")

    if budget_limit > 0:
        left = budget_limit - expenses
        if left >= 0:
            lines.append(f"You used {budget_used_pct}% of your ₹{format_inr(budget_limit)} budget (₹{format_inr(left)} left).")
        else:
            lines.append(f"You went over your ₹{format_inr(budget_limit)} budget by ₹{format_inr(-left)}.")
    else:
        lines.append("No budget was set for this month.")

    top = max(rows, key=lambda r: r["cur"]) if rows else None
    if top and top["cur"]:
        lines.append(f"{top['name']} was your biggest spend at ₹{format_inr(top['cur'])} ({top['share']}% of all spending).")

    phrase = compare_phrase(expenses, before_point, same_point=is_current_month)
    if phrase:
        lines.append(f"You {'have spent' if is_current_month else 'spent'} {phrase}.")

    if goal_added:
        lines.append(f"₹{format_inr(goal_added)} went into your goals.")
    if bill_counts["total"]:
        lines.append(f"{bill_counts['paid']} of {bill_counts['total']} bills are paid.")

    return render_template(
        "report.html",
        active_page="dashboard",
        month=month,
        month_name=month_label(month),
        prev_name=month_label(prev_month),
        is_current_month=is_current_month,
        prev_link=prev_link,
        next_link=next_link,
        income=income,
        expenses=expenses,
        saved=saved,
        budget_limit=budget_limit,
        budget_used_pct=budget_used_pct,
        rows=rows,
        lines=lines
    )


# ---------------------------------------------------------------------------
# Monthly budget
# ---------------------------------------------------------------------------

@app.route("/budget", methods=["GET", "POST"])
def set_budget():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    month = parse_month(request.values.get("month"))
    error = None
    conn = get_db()

    if request.method == "POST":
        amount_raw = request.form.get("amount", "")
        try:
            limit = to_paise(amount_raw)
            if limit <= 0:
                raise ValueError
        except ValueError:
            error = "Budget must be a positive number."

        if not error:
            # One budget per month: add it, or replace it if the month already has one.
            conn.execute(
                """INSERT INTO monthly_budgets (user_id, month, monthly_limit)
                   VALUES (?,?,?)
                   ON CONFLICT(user_id, month)
                   DO UPDATE SET monthly_limit=excluded.monthly_limit""",
                (user_id, month, limit)
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dashboard", month=min(month, current_month())))

    budget = get_budget(conn, user_id, month)
    conn.close()

    if request.method == "POST":
        prefill = request.form.get("amount", "")
    else:
        prefill = to_rupees(budget["limit"]) if budget["limit"] else ""

    return render_template(
        "set_budget.html",
        active_page="dashboard",
        error=error,
        month=month,
        month_name=month_label(month),
        budget_limit=budget["limit"],
        budget_kind=budget["kind"],
        budget_from=budget["from_label"],
        prefill=prefill
    )


# ---------------------------------------------------------------------------
# Entry forms: transaction, money lent, bill
# ---------------------------------------------------------------------------

@app.route("/add-transaction", methods=["GET", "POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    warning = None
    today_str = date.today().isoformat()
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        t_type = request.form.get("type", "expense")
        category = request.form.get("category", "").strip()
        amount_raw = request.form.get("amount", "")
        description = request.form.get("description", "").strip()
        txn_date = request.form.get("date", "")
        amount = None

        if not category or not amount_raw or not txn_date:
            error = "Category, amount, and date are required."
        else:
            try:
                amount = to_paise(amount_raw)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                error = "Amount must be a positive number."

        if not error:
            error, warning = date_gate(txn_date, request.form)

        if not error and not warning:
            conn = get_db()
            if t_type == "income":
                conn.execute(
                    "INSERT INTO income (user_id, source, amount, date) VALUES (?,?,?,?)",
                    (session["user_id"], category, amount, txn_date)
                )
            else:
                conn.execute(
                    "INSERT INTO expenses (user_id, category, amount, description, date) VALUES (?,?,?,?,?)",
                    (session["user_id"], category, amount, description, txn_date)
                )
            conn.commit()
            conn.close()

            if request.form.get("save_action") == "add_another":
                return redirect(url_for("add_transaction", saved="1"))

            # Open the month the entry belongs to, so it is easy to see.
            return redirect(url_for("dashboard", month=min(txn_date[:7], current_month())))

    return render_template(
        "add_transaction.html",
        active_page="dashboard",
        error=error,
        warning=warning,
        form=form,
        today=today_str
    )


# ---------------------------------------------------------------------------
# Transactions list: see every entry and delete a wrong one
# ---------------------------------------------------------------------------

def transaction_entries(conn, user_id, start, end=None):
    """Income and expense rows from [start, end) as one list, newest first.

    end=None means no upper limit (used for entries dated in the future).
    """
    limit_sql = "AND date<?" if end else ""
    params = (user_id, start, end) if end else (user_id, start)

    expenses = conn.execute(
        f"""SELECT id, category AS title, description AS note, amount, date
            FROM expenses WHERE user_id=? AND date>=? {limit_sql}""",
        params
    ).fetchall()
    incomes = conn.execute(
        f"""SELECT id, source AS title, '' AS note, amount, date
            FROM income WHERE user_id=? AND date>=? {limit_sql}""",
        params
    ).fetchall()

    entries = [{"kind": "expense", "id": r["id"], "title": r["title"], "note": r["note"] or "",
                "amount": r["amount"], "date": r["date"]} for r in expenses]
    entries += [{"kind": "income", "id": r["id"], "title": r["title"], "note": "",
                 "amount": r["amount"], "date": r["date"]} for r in incomes]
    entries.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return entries


@app.route("/transactions")
def transactions():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]
    month = pick_month(request.args.get("month"))
    month_start, month_end = month_bounds(month)

    month_values = available_months(conn, user_id, include={month})
    months = [{"value": m, "label": month_label(m)} for m in sorted(month_values, reverse=True)]

    entries = transaction_entries(conn, user_id, month_start, month_end)
    income = total_between(conn, "income", user_id, month_start, month_end)
    expenses = total_between(conn, "expenses", user_id, month_start, month_end)

    # Entries dated after this month never appear in a month list, so they get their own list.
    future_start = month_bounds(shift_month(current_month(), 1))[0]
    future_entries = transaction_entries(conn, user_id, future_start)
    conn.close()

    return render_template(
        "transactions.html",
        active_page="dashboard",
        month=month,
        month_name=month_label(month),
        months=months,
        entries=entries,
        future_entries=future_entries,
        income=income,
        expenses=expenses
    )


@app.route("/transactions/delete", methods=["POST"])
def delete_transaction():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # The table name comes from this fixed list, never from the form text.
    table = {"expense": "expenses", "income": "income"}.get(request.form.get("kind"))
    try:
        entry_id = int(request.form.get("id", ""))
    except ValueError:
        entry_id = None

    if table and entry_id is not None:
        conn = get_db()
        conn.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (entry_id, session["user_id"]))
        conn.commit()
        conn.close()

    return redirect(url_for("transactions", month=pick_month(request.form.get("month"))))


# ---------------------------------------------------------------------------
# Money to receive: add, edit, mark received, delete
# ---------------------------------------------------------------------------

def parse_lending_form(form):
    """Read the add/edit money-to-receive form. Returns (values, error, warning)."""
    values = {
        "person": form.get("person_name", "").strip()[:80],
        "amount": None,
        "lent_date": form.get("lent_date", ""),
        "interest": None,
        "notes": form.get("notes", "").strip()[:300] or None,
    }
    amount_raw = form.get("amount", "")
    interest_raw = form.get("interest", "").strip()

    if not values["person"] or not amount_raw or not values["lent_date"]:
        return values, "Name, amount, and date are required.", None

    try:
        values["amount"] = to_paise(amount_raw)
        if values["amount"] <= 0:
            raise ValueError
    except ValueError:
        return values, "Amount must be a positive number.", None

    if interest_raw:
        try:
            values["interest"] = float(interest_raw)
            if not (0 <= values["interest"] <= 1000):
                raise ValueError
        except ValueError:
            return values, "Interest must be a number like 12 (leave it empty if there is none).", None

    error, warning = date_gate(values["lent_date"], form)
    return values, error, warning


@app.route("/lending/add", methods=["GET", "POST"])
def add_lending():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    warning = None
    today_str = date.today().isoformat()
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        values, error, warning = parse_lending_form(request.form)

        if not error and not warning:
            conn = get_db()
            conn.execute(
                """INSERT INTO lending (user_id, person_name, amount, lent_date, interest, notes, status)
                   VALUES (?,?,?,?,?,?, 'pending')""",
                (session["user_id"], values["person"], values["amount"], values["lent_date"],
                 values["interest"], values["notes"])
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dashboard"))

    return render_template(
        "add_lending.html",
        active_page="dashboard",
        editing=False,
        error=error,
        warning=warning,
        form=form,
        today=today_str,
        action_url=url_for("add_lending"),
        back_url=url_for("dashboard")
    )


@app.route("/lending/<int:lending_id>/edit", methods=["GET", "POST"])
def edit_lending(lending_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM lending WHERE id=? AND user_id=?", (lending_id, session["user_id"])
    ).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("dashboard"))

    error = None
    warning = None
    if request.method == "POST":
        form = request.form
        values, error, warning = parse_lending_form(request.form)

        if not error and not warning:
            # Pending or received is changed only by "Mark received", never here.
            conn.execute(
                """UPDATE lending SET person_name=?, amount=?, lent_date=?, interest=?, notes=?
                   WHERE id=? AND user_id=?""",
                (values["person"], values["amount"], values["lent_date"], values["interest"],
                 values["notes"], lending_id, session["user_id"])
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dashboard"))
    else:
        form = {
            "person_name": row["person_name"],
            "amount": to_rupees(row["amount"]),
            "lent_date": row["lent_date"] or "",
            "interest": f"{row['interest']:g}" if row["interest"] else "",
            "notes": row["notes"] or "",
        }
    conn.close()

    return render_template(
        "add_lending.html",
        active_page="dashboard",
        editing=True,
        error=error,
        warning=warning,
        form=form,
        today=date.today().isoformat(),
        action_url=url_for("edit_lending", lending_id=lending_id),
        back_url=url_for("dashboard")
    )


@app.route("/lending/<int:lending_id>/received", methods=["POST"])
def mark_received(lending_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    conn.execute(
        "UPDATE lending SET status='received', received_date=? WHERE id=? AND user_id=?",
        (date.today().isoformat(), lending_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard", month=pick_month(request.form.get("month"))))


@app.route("/lending/<int:lending_id>/delete", methods=["POST"])
def delete_lending(lending_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    conn.execute("DELETE FROM lending WHERE id=? AND user_id=?", (lending_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard", month=pick_month(request.form.get("month"))))


# ---------------------------------------------------------------------------
# Bills: add, edit this month's bill, mark paid, delete
# ---------------------------------------------------------------------------

@app.route("/bills/add", methods=["GET", "POST"])
def add_bill():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    today_str = date.today().isoformat()
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        amount_raw = request.form.get("amount", "")
        due_date = request.form.get("due_date", "")
        amount = None

        if not name or not amount_raw or not due_date:
            error = "Name, amount, and due date are required."
        else:
            try:
                amount = to_paise(amount_raw)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                error = "Amount must be a positive number."

        if not error and check_date(due_date)[0] is None:
            error = "Please enter a valid due date."

        if not error:
            conn = get_db()
            cursor = conn.execute(
                "INSERT INTO bills (user_id, name, amount, due_date, status) VALUES (?,?,?,?, 'unpaid')",
                (session["user_id"], name, amount, due_date)
            )
            # The month this bill is first due in. Later months are added as they arrive.
            conn.execute(
                "INSERT INTO bill_payments (bill_id, month, amount, due_date, is_paid) VALUES (?,?,?,?,0)",
                (cursor.lastrowid, due_date[:7], amount, due_date)
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dashboard"))

    return render_template(
        "add_bill.html",
        active_page="dashboard",
        editing=False,
        error=error,
        form=form,
        today=today_str,
        action_url=url_for("add_bill"),
        back_url=url_for("dashboard"),
        min_date="",
        max_date=""
    )


@app.route("/bills/<int:payment_id>/edit", methods=["GET", "POST"])
def edit_bill(payment_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    row = conn.execute(
        """SELECT bp.id AS payment_id, bp.bill_id, bp.month, bp.amount, bp.due_date, b.name
           FROM bill_payments bp JOIN bills b ON b.id = bp.bill_id
           WHERE bp.id=? AND b.user_id=?""",
        (payment_id, session["user_id"])
    ).fetchone()

    # Only this month's bills can be edited. Earlier months are history.
    if not row or row["month"] != current_month():
        conn.close()
        return redirect(url_for("dashboard"))

    month = row["month"]
    month_start, month_end = month_bounds(month)
    last_day = (date.fromisoformat(month_end) - timedelta(days=1)).isoformat()
    error = None

    if request.method == "POST":
        form = request.form
        name = request.form.get("name", "").strip()[:80]
        amount_raw = request.form.get("amount", "")
        due_date = request.form.get("due_date", "")
        amount = None

        if not name or not amount_raw or not due_date:
            error = "Name, amount, and due date are required."
        else:
            try:
                amount = to_paise(amount_raw)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                error = "Amount must be a positive number."

        if not error and check_date(due_date)[0] is None:
            error = "Please enter a valid due date."
        if not error and not (month_start <= due_date < month_end):
            error = f"The due date must be in {month_label(month)}."

        if not error:
            # The bill itself takes the new values too, so next month's copy follows them.
            conn.execute(
                "UPDATE bills SET name=?, amount=?, due_date=? WHERE id=? AND user_id=?",
                (name, amount, due_date, row["bill_id"], session["user_id"])
            )
            conn.execute(
                "UPDATE bill_payments SET amount=?, due_date=? WHERE id=?",
                (amount, due_date, payment_id)
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dashboard"))
    else:
        form = {"name": row["name"], "amount": to_rupees(row["amount"]), "due_date": row["due_date"]}
    conn.close()

    return render_template(
        "add_bill.html",
        active_page="dashboard",
        editing=True,
        error=error,
        form=form,
        today=date.today().isoformat(),
        action_url=url_for("edit_bill", payment_id=payment_id),
        back_url=url_for("dashboard"),
        min_date=month_start,
        max_date=last_day
    )


@app.route("/bills/<int:payment_id>/paid", methods=["POST"])
def mark_bill_paid(payment_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    conn.execute(
        """UPDATE bill_payments SET is_paid=1
           WHERE id=? AND bill_id IN (SELECT id FROM bills WHERE user_id=?)""",
        (payment_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard", month=pick_month(request.form.get("month"))))


@app.route("/bills/<int:bill_id>/delete", methods=["POST"])
def delete_bill(bill_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    # Only the owner's bill: its monthly rows go first, then the bill itself.
    owned = conn.execute(
        "SELECT id FROM bills WHERE id=? AND user_id=?", (bill_id, session["user_id"])
    ).fetchone()
    if owned:
        conn.execute("DELETE FROM bill_payments WHERE bill_id=?", (bill_id,))
        conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
        conn.commit()
    conn.close()
    return redirect(url_for("dashboard", month=pick_month(request.form.get("month"))))


MIN_PASSWORD_LENGTH = 8


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            error = "Username and password are required."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        else:
            conn = get_db()
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()

            if existing:
                conn.close()
                error = "That username is already taken."
            else:
                password_hash = generate_password_hash(password)
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, password_hash)
                )
                conn.commit()
                conn.close()
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))

    return render_template(
        "signup.html",
        active_page="signup",
        error=error
    )


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        again = request.form.get("new_password_again", "")

        conn = get_db()
        user = conn.execute(
            "SELECT password_hash FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()

        if not check_password_hash(user["password_hash"], current):
            error = "Your current password is not correct."
        elif len(new) < MIN_PASSWORD_LENGTH:
            error = f"The new password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif new != again:
            error = "The two new passwords do not match."
        elif new == current:
            error = "The new password must be different from the current one."
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (generate_password_hash(new), session["user_id"]))
            conn.commit()
            conn.close()
            session.clear()
            flash("Password changed. Please log in with your new password.", "success")
            return redirect(url_for("login"))
        conn.close()

    return render_template("change_password.html", active_page="dashboard", error=error)

    

def toolkit_attention(doc_attention, loans, goals):
    """The 'Needs attention' list on the Toolkit page, most urgent first.

    doc_attention = how many vault documents are expired or expiring soon.
    loans = build_loan() dicts, goals = build_goal() dicts for the active dreams.
    Document titles are left out on purpose: the vault is behind a PIN.
    """
    items = []

    for l in loans:
        if l["is_cleared"] or l["due_color"] not in ("red", "amber"):
            continue
        items.append({
            "level": l["due_color"],
            "text": f"{l['name']}: {l['due_text']}",
            "detail": f"EMI ₹{format_inr(l['emi'])}",
            "url": url_for("loan_detail", loan_id=l["id"]),
        })

    if doc_attention:
        many = doc_attention != 1
        items.append({
            "level": "amber",
            "text": f"{doc_attention} document{'s' if many else ''} need{'' if many else 's'} attention",
            "detail": f"Expired or expiring within {EXPIRY_WARNING_DAYS} days. Opens after your vault PIN.",
            "url": url_for("vault_home"),
        })

    for g in goals:
        if g["pace_kind"] == "behind":
            items.append({
                "level": "amber",
                "text": f"{g['name']} is behind pace",
                "detail": g["pace_text"],
                "url": url_for("dream_detail", goal_id=g["id"]),
            })

    order = {"red": 0, "amber": 1}
    items.sort(key=lambda item: order.get(item["level"], 2))
    return items


@app.route("/toolkit")
def toolkit():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]

    goal_rows = conn.execute("SELECT * FROM goals WHERE user_id=?", (user_id,)).fetchall()
    contributed = contribution_summary(conn, user_id)
    doc_count, doc_attention = vault_summary(conn, user_id)
    loan_rows = [build_loan(r) for r in
                 conn.execute("SELECT * FROM loans WHERE user_id=?", (user_id,)).fetchall()]
    conn.close()

    # Dream Planner card
    active_goals = [build_goal(g, contributed.get(g["id"])) for g in goal_rows if not g["is_achieved"]]
    dream_pct = 0
    if goal_rows:
        total_target = sum(g["target_amount"] for g in goal_rows)
        total_saved = sum(min(g["current_amount"], g["target_amount"]) for g in goal_rows)
        dream_pct = goal_pct(total_saved, total_target)
        if active_goals:
            dream_preview = f"{len(active_goals)} goal{'s' if len(active_goals) != 1 else ''} in progress · {dream_pct}% saved"
        else:
            dream_preview = "All dreams achieved 🎉"
    else:
        dream_preview = "No dreams yet"

    # Document Vault card
    if doc_count == 0:
        vault_preview = "No documents yet"
    elif doc_attention:
        vault_preview = f"{doc_count} document{'s' if doc_count != 1 else ''} · {doc_attention} need{'s' if doc_attention == 1 else ''} attention"
    else:
        vault_preview = f"{doc_count} document{'s' if doc_count != 1 else ''} · all up to date"

    # Loan Tracker card: how much of everything payable has been paid so far
    active_loans = [l for l in loan_rows if not l["is_cleared"]]
    loan_pct = goal_pct(sum(l["paid_amount"] for l in loan_rows),
                        sum(l["total_payable"] for l in loan_rows))
    if not loan_rows:
        loan_preview = "No loans tracked yet"
    elif not active_loans:
        loan_preview = "All loans cleared 🎉"
    else:
        loan_preview = f"{len(active_loans)} loan{'s' if len(active_loans) != 1 else ''} · ₹{format_inr(sum(l['remaining'] for l in active_loans))} outstanding"

    return render_template(
        "toolkit.html",
        active_page="toolkit",
        dream_preview=dream_preview,
        vault_preview=vault_preview,
        loan_preview=loan_preview,
        dream_pct=dream_pct,
        loan_pct=loan_pct,
        attention=toolkit_attention(doc_attention, loan_rows, active_goals)
    )


# ---------------------------------------------------------------------------
# Dream Planner
# ---------------------------------------------------------------------------

MILESTONES = (25, 50, 75, 100)

# First match wins. Words are compared against the category and the dream name.
DREAM_ICONS = [
    ("🏠", ("home", "house", "flat", "apartment", "land", "property")),
    ("✈️", ("travel", "trip", "vacation", "holiday", "tour", "flight")),
    ("💻", ("laptop", "phone", "electronic", "gadget", "computer", "mobile", "camera")),
    ("🚗", ("car", "bike", "vehicle", "scooter", "motorcycle")),
    ("🎓", ("education", "study", "course", "college", "school", "degree")),
    ("💍", ("wedding", "marriage", "engagement")),
    ("🛟", ("emergency", "safety", "rainy")),
]


def dream_icon(category, name):
    """Pick an emoji from the category and name (🎯 if nothing matches)."""
    words = re.findall(r"[a-z]+", f"{category or ''} {name or ''}".lower())
    for icon, keys in DREAM_ICONS:
        for word in words:
            for key in keys:
                if word == key or word == key + "s" or (len(key) >= 6 and word.startswith(key)):
                    return icon
    return "🎯"


def months_until(target_text):
    """Whole months from today to a date (a part month counts as one). None if no valid date."""
    try:
        target = date.fromisoformat(target_text)
    except (TypeError, ValueError):
        return None
    today = date.today()
    months = (target.year - today.year) * 12 + target.month - today.month
    if target.day > today.day:
        months += 1
    return months


def contribution_summary(conn, user_id):
    """{goal_id: (total paise contributed, date of the first contribution)}"""
    rows = conn.execute(
        """SELECT c.goal_id, SUM(c.amount) AS total, MIN(c.date) AS first_date
           FROM contributions c JOIN goals g ON g.id = c.goal_id
           WHERE g.user_id=? GROUP BY c.goal_id""",
        (user_id,)
    ).fetchall()
    return {r["goal_id"]: (r["total"], r["first_date"]) for r in rows}


def build_goal(g, contributed=None):
    """Turn a goals row into the dict the Dream Planner pages use.

    contributed is (total contributed, date of first contribution) or None.
    """
    total, first_date = contributed or (0, None)
    remaining = max(0, g["target_amount"] - g["current_amount"])
    done = goal_pct(g["current_amount"], g["target_amount"])

    # Average saved per month, counting months with nothing saved too.
    avg = None
    if total > 0 and first_date:
        try:
            today = date.today()
            months_active = max(1, (today.year * 12 + today.month)
                                - (int(first_date[:4]) * 12 + int(first_date[5:7])) + 1)
            avg = div_round(total, months_active)
        except ValueError:
            avg = None

    # Months left: from the real average if there is one, else the planned
    # monthly amount, else the distance to the target date.
    months_left = None
    if remaining > 0:
        rate = avg or g["monthly_target"]
        if rate:
            months_left = -(-remaining // rate)          # divide, rounding up
        elif g["target_date"]:
            m = months_until(g["target_date"])
            months_left = None if m is None else max(0, m)
        if months_left is not None and months_left > 1200:   # over 100 years: not a real estimate
            months_left = None
    est_label = month_label(shift_month(current_month(), months_left)) if months_left is not None else None
    if months_left is None:
        months_left_text = "—"
    elif months_left == 0:
        months_left_text = "Target date reached"
    elif months_left == 1:
        months_left_text = "1 month left"
    else:
        months_left_text = f"{months_left} months left"

    # The pace line.
    pace_text, pace_kind = None, None
    if remaining > 0:
        planned = g["monthly_target"]
        to_target = months_until(g["target_date"]) if g["target_date"] else None
        target_name = format_day(g["target_date"]) if g["target_date"] else ""
        if avg is None:
            pace_text = "Add your first contribution to see your pace."
            if to_target is not None and to_target >= 1:
                pace_text += f" You would need about ₹{format_inr(-(-remaining // to_target))}/month to finish by {target_name}."
        elif to_target is not None and to_target <= 0:
            pace_text = f"The target date ({target_name}) has passed. ₹{format_inr(remaining)} is still to go."
            pace_kind = "behind"
        elif to_target is not None:
            needed = -(-remaining // to_target)
            if avg >= needed:
                pace_text = f"On track: you save about ₹{format_inr(avg)}/month and need ₹{format_inr(needed)}/month to finish by {target_name}."
                pace_kind = "ahead"
            else:
                pace_text = f"Behind: you save about ₹{format_inr(avg)}/month but need ₹{format_inr(needed)}/month to finish by {target_name}."
                pace_kind = "behind"
        elif planned:
            if avg >= planned:
                pace_text = f"On plan: you save about ₹{format_inr(avg)}/month, and you planned ₹{format_inr(planned)}/month."
                pace_kind = "ahead"
            else:
                pace_text = f"Behind plan: you save about ₹{format_inr(avg)}/month, and you planned ₹{format_inr(planned)}/month."
                pace_kind = "behind"
        else:
            pace_text = f"You save about ₹{format_inr(avg)}/month."

    return {
        "id": g["id"],
        "name": g["name"],
        "category": g["category"],
        "icon": dream_icon(g["category"], g["name"]),
        "target_amount": g["target_amount"],
        "current_amount": g["current_amount"],
        "target_date": g["target_date"],
        "monthly_target": g["monthly_target"],
        "is_priority": g["is_priority"],
        "is_achieved": g["is_achieved"],
        "pct": done,
        "remaining": remaining,
        "avg_monthly": avg,
        "months_left_text": months_left_text,
        "est_label": est_label,
        "pace_text": pace_text,
        "pace_kind": pace_kind,
        "milestones": [{"value": m, "reached": done >= m} for m in MILESTONES],
    }


def group_contributions(rows):
    """Group contributions (newest first) by month: [{label, total, items}]."""
    groups = []
    for r in rows:
        key = (r["date"] or "")[:7]
        if not groups or groups[-1]["key"] != key:
            try:
                label = month_label(key)
            except ValueError:
                label = key or "Unknown date"
            groups.append({"key": key, "label": label, "total": 0, "items": []})
        groups[-1]["total"] += r["amount"]
        groups[-1]["items"].append({"date": r["date"], "amount": r["amount"]})
    return groups


def parse_dream_form(form):
    """Read the add/edit dream form. Returns (values, error)."""
    name = form.get("name", "").strip()[:80]
    category = form.get("category", "").strip()[:40]
    target_raw = form.get("target_amount", "")
    monthly_raw = form.get("monthly_target", "").strip()
    target_date = form.get("target_date", "").strip() or None
    values = {
        "name": name, "category": category, "target_amount": None,
        "monthly_target": None, "target_date": target_date,
        "is_priority": 1 if form.get("is_priority") else 0,
    }

    if not name or not category or not target_raw:
        return values, "Dream name, category and target amount are required."
    try:
        values["target_amount"] = to_paise(target_raw)
        if values["target_amount"] <= 0:
            raise ValueError
    except ValueError:
        return values, "Target amount must be a positive number."
    if monthly_raw:
        try:
            values["monthly_target"] = to_paise(monthly_raw)
            if values["monthly_target"] <= 0:
                raise ValueError
        except ValueError:
            return values, "Monthly contribution must be a positive number."
    if target_date and check_date(target_date)[0] is None:
        return values, "Please enter a valid target date."
    return values, None


@app.route("/dream-planner")
def dream_planner():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]

    active_rows = conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND is_achieved=0 ORDER BY is_priority DESC, id DESC",
        (user_id,)
    ).fetchall()

    achieved_rows = conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND is_achieved=1 ORDER BY id DESC",
        (user_id,)
    ).fetchall()

    summary = contribution_summary(conn, user_id)
    conn.close()

    goals = [build_goal(g, summary.get(g["id"])) for g in active_rows]
    achieved = [build_goal(g, summary.get(g["id"])) for g in achieved_rows]

    total_target = sum(g["target_amount"] for g in goals)
    total_saved = sum(min(g["current_amount"], g["target_amount"]) for g in goals)

    return render_template(
        "dream_planner.html",
        active_page="toolkit",
        goals=goals,
        achieved=achieved,
        total_target=total_target,
        total_saved=total_saved,
        overall_pct=goal_pct(total_saved, total_target)
    )


@app.route("/dream-planner/add", methods=["GET", "POST"])
def add_dream():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        values, error = parse_dream_form(request.form)

        if not error:
            conn = get_db()
            if values["is_priority"]:
                # Top priority is exclusive: the old one is cleared.
                conn.execute("UPDATE goals SET is_priority=0 WHERE user_id=?", (session["user_id"],))
            conn.execute(
                """INSERT INTO goals
                   (user_id, name, category, target_amount, current_amount, target_date, monthly_target, is_priority, is_achieved, created_date)
                   VALUES (?,?,?,?,0,?,?,?,0,?)""",
                (session["user_id"], values["name"], values["category"], values["target_amount"],
                 values["target_date"], values["monthly_target"], values["is_priority"],
                 date.today().isoformat())
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dream_planner"))

    return render_template(
        "add_dream.html",
        active_page="toolkit",
        editing=False,
        form=form,
        error=error,
        action_url=url_for("add_dream"),
        back_url=url_for("dream_planner")
    )


@app.route("/dream-planner/<int:goal_id>/edit", methods=["GET", "POST"])
def edit_dream(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_db()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
    ).fetchone()
    if not goal:
        conn.close()
        return redirect(url_for("dream_planner"))

    error = None
    if request.method == "POST":
        form = request.form
        values, error = parse_dream_form(request.form)

        if not error:
            # Reaching the target moves the dream to Achieved; raising it reopens the dream.
            achieved = 1 if goal["current_amount"] >= values["target_amount"] else 0
            priority = 1 if (values["is_priority"] and not achieved) else 0
            if priority:
                conn.execute("UPDATE goals SET is_priority=0 WHERE user_id=?", (user_id,))
            conn.execute(
                """UPDATE goals SET name=?, category=?, target_amount=?, monthly_target=?,
                          target_date=?, is_priority=?, is_achieved=?
                   WHERE id=? AND user_id=?""",
                (values["name"], values["category"], values["target_amount"], values["monthly_target"],
                 values["target_date"], priority, achieved, goal_id, user_id)
            )
            conn.commit()
            conn.close()
            return redirect(url_for("dream_detail", goal_id=goal_id))
    else:
        form = {
            "name": goal["name"],
            "category": goal["category"] or "",
            "target_amount": to_rupees(goal["target_amount"]),
            "monthly_target": to_rupees(goal["monthly_target"]) if goal["monthly_target"] else "",
            "target_date": goal["target_date"] or "",
            "is_priority": goal["is_priority"],
        }
    conn.close()

    return render_template(
        "add_dream.html",
        active_page="toolkit",
        editing=True,
        form=form,
        error=error,
        action_url=url_for("edit_dream", goal_id=goal_id),
        back_url=url_for("dream_detail", goal_id=goal_id)
    )


@app.route("/dream-planner/<int:goal_id>")
def dream_detail(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    user_id = session["user_id"]

    goal_row = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
    ).fetchone()

    if not goal_row:
        conn.close()
        return redirect(url_for("dream_planner"))

    contribution_rows = conn.execute(
        "SELECT amount, date FROM contributions WHERE goal_id=? ORDER BY date DESC, id DESC",
        (goal_id,)
    ).fetchall()
    summary = contribution_summary(conn, user_id)
    conn.close()

    goal = build_goal(goal_row, summary.get(goal_id))

    return render_template(
        "dream_detail.html",
        active_page="toolkit",
        goal=goal,
        groups=group_contributions(contribution_rows)
    )


@app.route("/dream-planner/<int:goal_id>/contribute", methods=["POST"])
def add_contribution(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    amount_raw = request.form.get("amount")

    conn = get_db()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
    ).fetchone()

    if not goal:
        conn.close()
        return redirect(url_for("dream_planner"))

    if goal["is_achieved"]:                      # nothing more to add to a finished dream
        conn.close()
        return redirect(url_for("dream_detail", goal_id=goal_id))

    try:
        amount = to_paise(amount_raw)
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        conn.close()
        return redirect(url_for("dream_detail", goal_id=goal_id))

    conn.execute(
        "INSERT INTO contributions (goal_id, amount, date) VALUES (?,?,?)",
        (goal_id, amount, date.today().isoformat())
    )

    new_amount = goal["current_amount"] + amount
    is_achieved = 1 if new_amount >= goal["target_amount"] else 0

    # Highest milestone now reached. It is celebrated once: only when it is
    # higher than the last one we celebrated for this dream.
    new_pct = goal_pct(new_amount, goal["target_amount"])
    reached = max([m for m in MILESTONES if new_pct >= m], default=0)
    celebrated = max(goal["milestone_celebrated"], reached)

    # A finished dream no longer holds the top-priority slot.
    conn.execute(
        """UPDATE goals SET current_amount=?, is_achieved=?, milestone_celebrated=?,
                  is_priority=CASE WHEN ?=1 THEN 0 ELSE is_priority END
           WHERE id=?""",
        (new_amount, is_achieved, celebrated, is_achieved, goal_id)
    )
    conn.commit()
    conn.close()

    if reached > goal["milestone_celebrated"]:
        if reached == 100:
            flash(f"🏆 Dream achieved! {goal['name']} is fully funded.", "celebrate")
        else:
            flash(f"🎉 {reached}% of {goal['name']} reached!", "celebrate")

    return redirect(url_for("dream_detail", goal_id=goal_id))


@app.route("/dream-planner/<int:goal_id>/priority", methods=["POST"])
def toggle_priority(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_db()
    goal = conn.execute(
        "SELECT is_priority, is_achieved FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
    ).fetchone()

    if goal and not goal["is_achieved"]:
        if goal["is_priority"]:
            conn.execute("UPDATE goals SET is_priority=0 WHERE id=?", (goal_id,))
        else:
            # Exclusive: clear the old top priority, then set this one.
            conn.execute("UPDATE goals SET is_priority=0 WHERE user_id=?", (user_id,))
            conn.execute("UPDATE goals SET is_priority=1 WHERE id=?", (goal_id,))
        conn.commit()

    conn.close()
    return redirect(url_for("dream_detail", goal_id=goal_id))


@app.route("/dream-planner/<int:goal_id>/delete", methods=["POST"])
def delete_dream(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    conn = get_db()
    goal = conn.execute(
        "SELECT id FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
    ).fetchone()

    if goal:
        conn.execute("DELETE FROM contributions WHERE goal_id=?", (goal_id,))
        conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
        conn.commit()

    conn.close()
    return redirect(url_for("dream_planner"))


# ---------------------------------------------------------------------------
# Document Vault: PIN lock, categories, uploads, viewer, manual archive
# ---------------------------------------------------------------------------

# (web address part, name stored in the database, emoji)
VAULT_CATEGORIES = [
    ("identity", "Identity", "🪪"),
    ("vehicle", "Vehicle", "🚗"),
    ("insurance", "Insurance", "🛡️"),
    ("financial", "Financial", "💰"),
    ("education", "Education", "🎓"),
    ("warranty", "Warranty & Bills", "🧾"),
]
VAULT_BY_SLUG = {slug: (name, icon) for slug, name, icon in VAULT_CATEGORIES}
VAULT_SLUG_OF = {name: slug for slug, name, icon in VAULT_CATEGORIES}
VAULT_ICON_OF = {name: icon for slug, name, icon in VAULT_CATEGORIES}

UPLOAD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "vault")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024          # 10 MB per file
VAULT_IDLE_SECONDS = 10 * 60                 # the vault locks itself after 10 quiet minutes
EXPIRY_WARNING_DAYS = 30
FILE_MIMETYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png"}

# Flask stops anything bigger than this before it even reaches our code.
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 2 * 1024 * 1024


@app.errorhandler(413)
def file_too_large(error):
    return render_template(
        "vault_message.html", active_page="toolkit",
        title="That file is too big",
        text="Documents can be up to 10 MB. Please choose a smaller file.",
        back_url=url_for("vault_add")
    ), 413


def vault_gate():
    """None if the vault is open for this user, else the page to send them to."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("vault_until", 0) <= time.time():
        return redirect(url_for("vault_unlock"))
    session["vault_until"] = time.time() + VAULT_IDLE_SECONDS      # still in use, so keep it open
    return None


def valid_pin(pin):
    return bool(re.fullmatch(r"\d{4,6}", pin or ""))


def sniff_file_type(data):
    """Look at the first bytes to see what a file really is. Returns pdf, jpg, png or None."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def expiry_info(expiry_text):
    """How a document's expiry should look. Sorted by 'order' to put urgent ones first."""
    try:
        days = (date.fromisoformat(expiry_text) - date.today()).days
    except (TypeError, ValueError):
        return {"status": "none", "text": "No expiry", "order": 10 ** 9, "attention": False}
    if days < 0:
        ago = f"{-days} day{'s' if days != -1 else ''} ago"
        return {"status": "expired", "text": f"Expired {ago}", "order": days, "attention": True}
    if days == 0:
        return {"status": "soon", "text": "Expires today", "order": days, "attention": True}
    if days <= EXPIRY_WARNING_DAYS:
        return {"status": "soon", "text": f"Expires in {days} day{'s' if days != 1 else ''}", "order": days, "attention": True}
    return {"status": "ok", "text": f"Expires {format_day(expiry_text)}", "order": days, "attention": False}


def build_document(row):
    info = expiry_info(row["expiry_date"])
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "slug": VAULT_SLUG_OF.get(row["category"], ""),
        "icon": "📄" if row["file_type"] == "pdf" else "🖼️",
        "file_type": row["file_type"],
        "issue_date": row["issue_date"],
        "expiry_date": row["expiry_date"],
        "notes": row["notes"],
        "is_archived": row["is_archived"],
        "is_favorite": row["is_favorite"],
        "expiry": info,
    }


def vault_file_path(file_path):
    """The real location of a stored file, or None if the name looks unsafe."""
    full = os.path.realpath(os.path.join(UPLOAD_ROOT, file_path))
    if not full.startswith(os.path.realpath(UPLOAD_ROOT) + os.sep):
        return None
    return full


def vault_documents(conn, user_id, archived=0):
    rows = conn.execute(
        "SELECT * FROM documents WHERE user_id=? AND is_archived=? ORDER BY id DESC",
        (user_id, archived)
    ).fetchall()
    return [build_document(r) for r in rows]


def vault_summary(conn, user_id):
    """(documents kept, documents needing attention) for the Toolkit card."""
    docs = vault_documents(conn, user_id)
    return len(docs), sum(1 for d in docs if d["expiry"]["attention"])


@app.route("/vault/unlock", methods=["GET", "POST"])
def vault_unlock():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_db()
    user = conn.execute(
        "SELECT vault_pin_hash, vault_failed, vault_retry_at FROM users WHERE id=?", (user_id,)
    ).fetchone()
    first_time = not user["vault_pin_hash"]
    error = None
    wait = max(0, int(-(-(user["vault_retry_at"] - time.time()) // 1))) if not first_time else 0

    if request.method == "POST":
        pin = request.form.get("pin", "")
        if first_time:
            if not valid_pin(pin):
                error = "Your PIN must be 4 to 6 digits."
            elif pin != request.form.get("pin_again", ""):
                error = "The two PINs do not match."
            else:
                conn.execute("UPDATE users SET vault_pin_hash=?, vault_failed=0, vault_retry_at=0 WHERE id=?",
                             (generate_password_hash(pin), user_id))
                conn.commit()
                conn.close()
                session["vault_until"] = time.time() + VAULT_IDLE_SECONDS
                return redirect(url_for("vault_home"))
        elif wait > 0:
            error = f"Please wait {wait} second{'s' if wait != 1 else ''} before trying again."
        elif check_password_hash(user["vault_pin_hash"], pin):
            conn.execute("UPDATE users SET vault_failed=0, vault_retry_at=0 WHERE id=?", (user_id,))
            conn.commit()
            conn.close()
            session["vault_until"] = time.time() + VAULT_IDLE_SECONDS
            return redirect(url_for("vault_home"))
        else:
            # No lock-out, just a short pause that grows with every wrong try (2s, 4s, 6s ... up to 60s).
            failed = user["vault_failed"] + 1
            delay = min(failed * 2, 60)
            conn.execute("UPDATE users SET vault_failed=?, vault_retry_at=? WHERE id=?",
                         (failed, time.time() + delay, user_id))
            conn.commit()
            wait = delay
            error = f"Wrong PIN. Please wait {delay} seconds before trying again."

    conn.close()
    return render_template("vault_unlock.html", active_page="toolkit",
                           first_time=first_time, error=error, wait=wait)


@app.route("/vault/forgot", methods=["GET", "POST"])
def vault_forgot():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        pin = request.form.get("pin", "")
        conn = get_db()
        user = conn.execute("SELECT password_hash FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], password):
            error = "That is not your account password."
        elif not valid_pin(pin):
            error = "Your new PIN must be 4 to 6 digits."
        elif pin != request.form.get("pin_again", ""):
            error = "The two PINs do not match."
        else:
            conn.execute("UPDATE users SET vault_pin_hash=?, vault_failed=0, vault_retry_at=0 WHERE id=?",
                         (generate_password_hash(pin), session["user_id"]))
            conn.commit()
            conn.close()
            session["vault_until"] = time.time() + VAULT_IDLE_SECONDS
            return redirect(url_for("vault_home"))
        conn.close()

    return render_template("vault_forgot.html", active_page="toolkit", error=error)


@app.route("/vault/lock", methods=["POST"])
def vault_lock():
    session.pop("vault_until", None)
    return redirect(url_for("toolkit"))


@app.route("/vault")
def vault_home():
    blocked = vault_gate()
    if blocked:
        return blocked

    user_id = session["user_id"]
    query = request.args.get("q", "").strip()[:60]
    conn = get_db()
    docs = vault_documents(conn, user_id)
    recent_rows = conn.execute(
        """SELECT * FROM documents WHERE user_id=? AND is_archived=0 AND last_viewed IS NOT NULL
           ORDER BY last_viewed DESC LIMIT 5""",
        (user_id,)
    ).fetchall()
    conn.close()

    results = None
    if query:
        needle = query.lower()
        results = [d for d in docs
                   if needle in d["title"].lower() or needle in d["category"].lower()
                   or needle in (d["notes"] or "").lower()]
        results.sort(key=lambda d: d["expiry"]["order"])

    categories = []
    for slug, name, icon in VAULT_CATEGORIES:
        mine = [d for d in docs if d["category"] == name]
        categories.append({
            "slug": slug, "name": name, "icon": icon, "count": len(mine),
            "attention": sum(1 for d in mine if d["expiry"]["attention"]),
        })

    return render_template(
        "vault.html",
        active_page="toolkit",
        query=query,
        results=results,
        favorites=[d for d in docs if d["is_favorite"]],
        recent=[build_document(r) for r in recent_rows],
        categories=categories,
        total=len(docs)
    )


@app.route("/vault/category/<slug>")
def vault_category(slug):
    blocked = vault_gate()
    if blocked:
        return blocked
    if slug not in VAULT_BY_SLUG:
        return redirect(url_for("vault_home"))

    name, icon = VAULT_BY_SLUG[slug]
    conn = get_db()
    active = [d for d in vault_documents(conn, session["user_id"]) if d["category"] == name]
    archived = [d for d in vault_documents(conn, session["user_id"], archived=1) if d["category"] == name]
    conn.close()
    active.sort(key=lambda d: d["expiry"]["order"])     # most urgent first

    return render_template("vault_category.html", active_page="toolkit",
                           slug=slug, name=name, icon=icon, docs=active, archived=archived)


def parse_document_form(form):
    """Read the add/edit form. Returns (values, error)."""
    values = {
        "title": form.get("title", "").strip()[:80],
        "category": form.get("category", "").strip(),
        "issue_date": form.get("issue_date", "").strip() or None,
        "expiry_date": form.get("expiry_date", "").strip() or None,
        "notes": form.get("notes", "").strip()[:300] or None,
    }
    if not values["title"] or not values["category"]:
        return values, "Title and category are required."
    if values["category"] not in VAULT_SLUG_OF:
        return values, "Please choose one of the categories."
    for key, label in (("issue_date", "issue date"), ("expiry_date", "expiry date")):
        if values[key] and check_date(values[key])[0] is None:
            return values, f"Please enter a valid {label}."
    if values["issue_date"] and values["expiry_date"] and values["expiry_date"] < values["issue_date"]:
        return values, "The expiry date cannot be before the issue date."
    return values, None


@app.route("/vault/add", methods=["GET", "POST"])
def vault_add():
    blocked = vault_gate()
    if blocked:
        return blocked

    error = None
    form = request.form if request.method == "POST" else {
        "category": VAULT_BY_SLUG.get(request.args.get("category", ""), ("",))[0]
    }

    if request.method == "POST":
        values, error = parse_document_form(request.form)
        upload = request.files.get("file")
        data, kind = b"", None

        if not error:
            if not upload or not upload.filename:
                error = "Please choose a file to upload."
            else:
                extension = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
                data = upload.read(MAX_UPLOAD_BYTES + 1)
                kind = sniff_file_type(data)
                if extension not in ("jpg", "jpeg", "png", "pdf") or kind is None:
                    error = "Only JPG, PNG and PDF files can be uploaded."
                elif len(data) > MAX_UPLOAD_BYTES:
                    error = "That file is bigger than 10 MB."
                elif not data:
                    error = "That file is empty."

        if not error:
            user_folder = os.path.join(UPLOAD_ROOT, str(session["user_id"]))
            os.makedirs(user_folder, exist_ok=True)
            stored = f"{uuid.uuid4().hex}.{kind}"
            with open(os.path.join(user_folder, stored), "wb") as handle:
                handle.write(vault_cipher.encrypt(data))

            conn = get_db()
            conn.execute(
                """INSERT INTO documents
                   (user_id, title, category, file_path, file_type, issue_date, expiry_date, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session["user_id"], values["title"], values["category"],
                 f"{session['user_id']}/{stored}", kind,
                 values["issue_date"], values["expiry_date"], values["notes"])
            )
            conn.commit()
            conn.close()
            return redirect(url_for("vault_category", slug=VAULT_SLUG_OF[values["category"]]))

    return render_template("vault_add.html", active_page="toolkit", editing=False, form=form,
                           error=error, categories=VAULT_CATEGORIES, action_url=url_for("vault_add"),
                           back_url=url_for("vault_home"))


def own_document(conn, doc_id):
    return conn.execute(
        "SELECT * FROM documents WHERE id=? AND user_id=?", (doc_id, session["user_id"])
    ).fetchone()


@app.route("/vault/doc/<int:doc_id>")
def vault_view(doc_id):
    blocked = vault_gate()
    if blocked:
        return blocked

    conn = get_db()
    row = own_document(conn, doc_id)
    if not row:
        conn.close()
        return redirect(url_for("vault_home"))
    conn.execute("UPDATE documents SET last_viewed=? WHERE id=?",
                 (datetime.now().isoformat(timespec="seconds"), doc_id))
    conn.commit()
    conn.close()

    doc = build_document(row)
    return render_template("vault_view.html", active_page="toolkit", doc=doc,
                           back_url=url_for("vault_category", slug=doc["slug"]))


@app.route("/vault/file/<int:doc_id>")
def vault_file(doc_id):
    blocked = vault_gate()
    if blocked:
        return blocked

    conn = get_db()
    row = own_document(conn, doc_id)
    conn.close()
    full = vault_file_path(row["file_path"]) if row else None
    if not full or not os.path.isfile(full):
        abort(404)

    with open(full, "rb") as handle:
        raw = handle.read()
    try:
        plain = vault_cipher.decrypt(raw)
    except InvalidToken:
        if sniff_file_type(raw) is None:
            return render_template(
                "vault_message.html", active_page="toolkit",
                title="This document cannot be opened",
                text="It could not be unlocked. The vault key may be different from the one used when it was uploaded.",
                back_url=url_for("vault_home")
            ), 500
        # An older file saved before encryption existed: lock it now.
        plain = raw
        with open(full, "wb") as handle:
            handle.write(vault_cipher.encrypt(plain))

    response = send_file(io.BytesIO(plain), mimetype=FILE_MIMETYPES.get(row["file_type"], "application/octet-stream"))
    response.headers["Content-Disposition"] = "inline"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/vault/doc/<int:doc_id>/edit", methods=["GET", "POST"])
def vault_edit(doc_id):
    blocked = vault_gate()
    if blocked:
        return blocked

    conn = get_db()
    row = own_document(conn, doc_id)
    if not row:
        conn.close()
        return redirect(url_for("vault_home"))

    error = None
    if request.method == "POST":
        form = request.form
        values, error = parse_document_form(request.form)
        if not error:
            conn.execute(
                """UPDATE documents SET title=?, category=?, issue_date=?, expiry_date=?, notes=?
                   WHERE id=? AND user_id=?""",
                (values["title"], values["category"], values["issue_date"], values["expiry_date"],
                 values["notes"], doc_id, session["user_id"])
            )
            conn.commit()
            conn.close()
            return redirect(url_for("vault_view", doc_id=doc_id))
    else:
        form = {"title": row["title"], "category": row["category"], "issue_date": row["issue_date"] or "",
                "expiry_date": row["expiry_date"] or "", "notes": row["notes"] or ""}
    conn.close()

    return render_template("vault_add.html", active_page="toolkit", editing=True, form=form,
                           error=error, categories=VAULT_CATEGORIES,
                           action_url=url_for("vault_edit", doc_id=doc_id),
                           back_url=url_for("vault_view", doc_id=doc_id))


@app.route("/vault/doc/<int:doc_id>/favorite", methods=["POST"])
def vault_favorite(doc_id):
    blocked = vault_gate()
    if blocked:
        return blocked
    conn = get_db()
    conn.execute("UPDATE documents SET is_favorite = 1 - is_favorite WHERE id=? AND user_id=?",
                 (doc_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("vault_view", doc_id=doc_id))


@app.route("/vault/doc/<int:doc_id>/archive", methods=["POST"])
def vault_archive(doc_id):
    """Archiving is always the user's own action. Nothing is archived automatically."""
    blocked = vault_gate()
    if blocked:
        return blocked
    conn = get_db()
    row = own_document(conn, doc_id)
    if row:
        conn.execute("UPDATE documents SET is_archived = 1 - is_archived WHERE id=?", (doc_id,))
        conn.commit()
    conn.close()
    if not row:
        return redirect(url_for("vault_home"))
    return redirect(url_for("vault_category", slug=VAULT_SLUG_OF.get(row["category"], "")))


@app.route("/vault/doc/<int:doc_id>/delete", methods=["POST"])
def vault_delete(doc_id):
    blocked = vault_gate()
    if blocked:
        return blocked
    conn = get_db()
    row = own_document(conn, doc_id)
    if row:
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        full = vault_file_path(row["file_path"])
        if full and os.path.isfile(full):
            os.remove(full)
    conn.close()
    if not row:
        return redirect(url_for("vault_home"))
    return redirect(url_for("vault_category", slug=VAULT_SLUG_OF.get(row["category"], "")))


# ---------------------------------------------------------------------------
# Loan Tracker
# ---------------------------------------------------------------------------

def add_months(start, months):
    """The date `months` months after `start`, keeping the day (a short month uses its last day)."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = index // 12, index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def simulate_prepayment(balance, yearly_rate, emi, extra=0):
    """Month-by-month loop: how long does the loan last, and how much interest is paid?

    balance is what is still owed (paise), yearly_rate is a percent like 9.5, emi and
    extra are paise per month. Returns (months, interest paid in paise), or None if the
    payment never covers the interest (the loan would never end).
    """
    monthly_rate = yearly_rate / 12 / 100
    pay = emi + extra
    months, interest = 0, 0.0
    while balance > 0.5:
        charge = balance * monthly_rate
        if pay <= charge:                    # the payment does not even cover the interest
            return None
        months += 1
        interest += charge
        balance = balance + charge - pay
        if months > 1200:                    # 100 years: something is wrong with the numbers
            return None
    return months, int(round(interest / 100)) * 100      # an estimate, so whole rupees


def outstanding_principal(loan):
    """Estimate of the principal still owed, by replaying the EMIs already paid."""
    balance = float(loan["principal"])
    monthly_rate = loan["interest_rate"] / 12 / 100
    for _ in range(loan["emis_paid"]):
        balance = max(0.0, balance * (1 + monthly_rate) - loan["emi_amount"])
    return balance


def build_loan(row):
    """Turn a loans row into the dict the Loan Tracker pages use."""
    tenure, paid, emi = row["tenure_months"], min(row["emis_paid"], row["tenure_months"]), row["emi_amount"]
    total_payable = emi * tenure
    cleared = paid >= tenure
    start = date.fromisoformat(row["start_date"])
    next_due = None if cleared else add_months(start, paid + 1)

    due_text, due_color = "", "muted"
    if next_due:
        days = (next_due - date.today()).days
        if days < 0:
            due_text, due_color = f"Overdue since {format_day(next_due.isoformat())}", "red"
        elif days == 0:
            due_text, due_color = "Due today", "red"
        elif days <= 7:
            due_text, due_color = f"Due in {days} day{'s' if days != 1 else ''} ({format_day(next_due.isoformat())})", "amber"
        else:
            due_text, due_color = f"Next EMI {format_day(next_due.isoformat())}", "muted"

    return {
        "id": row["id"],
        "name": row["name"],
        "lender": row["lender"],
        "principal": row["principal"],
        "interest_rate": row["interest_rate"],
        "rate_text": f"{row['interest_rate']:g}%",
        "tenure": tenure,
        "start_date": row["start_date"],
        "emi": emi,
        "emis_paid": paid,
        "emis_left": tenure - paid,
        "paid_amount": emi * paid,
        "remaining": emi * (tenure - paid),
        "total_payable": total_payable,
        "total_interest": max(0, total_payable - row["principal"]),
        "pct": min(100, paid * 100 // tenure) if tenure else 0,
        "is_cleared": cleared,
        "next_due": next_due.isoformat() if next_due else None,
        "due_text": due_text,
        "due_color": due_color,
    }


def parse_loan_form(form):
    """Read the add/edit loan form. Returns (values, error)."""
    values = {
        "name": form.get("name", "").strip()[:80],
        "lender": form.get("lender", "").strip()[:80],
        "principal": None, "interest_rate": None, "tenure_months": None,
        "start_date": form.get("start_date", "").strip(),
        "emi_amount": None, "emis_paid": 0,
    }
    required = [values["name"], values["lender"], form.get("principal", ""), form.get("interest_rate", "").strip(),
                form.get("tenure_months", "").strip(), values["start_date"], form.get("emi_amount", "")]
    if not all(required):
        return values, "Loan name, lender, amount, interest rate, tenure, start date and EMI are all required."

    try:
        values["principal"] = to_paise(form.get("principal"))
        values["emi_amount"] = to_paise(form.get("emi_amount"))
        if values["principal"] <= 0 or values["emi_amount"] <= 0:
            raise ValueError
    except ValueError:
        return values, "Loan amount and EMI must be positive numbers."
    try:
        values["interest_rate"] = float(form.get("interest_rate"))
        if not (0 <= values["interest_rate"] <= 100):
            raise ValueError
    except ValueError:
        return values, "Interest rate must be a number from 0 to 100."
    try:
        values["tenure_months"] = int(form.get("tenure_months"))
        if not (1 <= values["tenure_months"] <= 600):
            raise ValueError
    except ValueError:
        return values, "Tenure must be a whole number of months (1 to 600)."
    if check_date(values["start_date"])[0] is None:
        return values, "Please enter a valid start date."
    if values["emi_amount"] * values["tenure_months"] < values["principal"]:
        return values, "EMI x tenure is less than the loan amount. Please check the numbers."

    paid_raw = form.get("emis_paid", "").strip()
    if paid_raw:
        try:
            values["emis_paid"] = int(paid_raw)
            if not (0 <= values["emis_paid"] <= values["tenure_months"]):
                raise ValueError
        except ValueError:
            return values, "EMIs already paid must be a whole number, not more than the tenure."
    return values, None


def upcoming_emis(loans, limit=5):
    """The next EMI of each active loan, soonest first (build_loan() dicts in)."""
    rows = [
        {
            "loan_id": l["id"],
            "name": l["name"],
            "amount": l["emi"],
            "due_date": l["next_due"],
            "due_text": l["due_text"],
            "due_color": l["due_color"],
        }
        for l in loans if l["next_due"]
    ]
    rows.sort(key=lambda r: r["due_date"])
    return rows[:limit]


@app.route("/loans")
def loan_tracker():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    rows = conn.execute("SELECT * FROM loans WHERE user_id=? ORDER BY id DESC", (session["user_id"],)).fetchall()
    conn.close()

    loans = [build_loan(r) for r in rows]
    active = sorted((l for l in loans if not l["is_cleared"]), key=lambda l: l["next_due"])
    cleared = [l for l in loans if l["is_cleared"]]

    return render_template(
        "loan_tracker.html",
        active_page="toolkit",
        loans=active,
        cleared=cleared,
        total_outstanding=sum(l["remaining"] for l in active),
        total_emi=sum(l["emi"] for l in active),
        total_interest=sum(l["total_interest"] for l in active),
        upcoming=upcoming_emis(active)
    )


@app.route("/loans/add", methods=["GET", "POST"])
def add_loan():
    if "user_id" not in session:
        return redirect(url_for("login"))

    error = None
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        values, error = parse_loan_form(request.form)
        if not error:
            conn = get_db()
            cursor = conn.execute(
                """INSERT INTO loans (user_id, name, lender, principal, interest_rate, tenure_months,
                                      start_date, emi_amount, emis_paid, created_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session["user_id"], values["name"], values["lender"], values["principal"],
                 values["interest_rate"], values["tenure_months"], values["start_date"],
                 values["emi_amount"], values["emis_paid"], date.today().isoformat())
            )
            conn.commit()
            conn.close()
            return redirect(url_for("loan_detail", loan_id=cursor.lastrowid))

    return render_template("add_loan.html", active_page="toolkit", editing=False, form=form,
                           error=error, action_url=url_for("add_loan"), back_url=url_for("loan_tracker"))


def own_loan(conn, loan_id):
    return conn.execute(
        "SELECT * FROM loans WHERE id=? AND user_id=?", (loan_id, session["user_id"])
    ).fetchone()


@app.route("/loans/<int:loan_id>/edit", methods=["GET", "POST"])
def edit_loan(loan_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    row = own_loan(conn, loan_id)
    if not row:
        conn.close()
        return redirect(url_for("loan_tracker"))

    error = None
    if request.method == "POST":
        form = request.form
        values, error = parse_loan_form(request.form)
        if not error:
            # The number of EMIs paid is changed only by "Mark EMI paid", never here.
            conn.execute(
                """UPDATE loans SET name=?, lender=?, principal=?, interest_rate=?, tenure_months=?,
                          start_date=?, emi_amount=?
                   WHERE id=? AND user_id=?""",
                (values["name"], values["lender"], values["principal"], values["interest_rate"],
                 values["tenure_months"], values["start_date"], values["emi_amount"],
                 loan_id, session["user_id"])
            )
            conn.commit()
            conn.close()
            return redirect(url_for("loan_detail", loan_id=loan_id))
    else:
        form = {
            "name": row["name"], "lender": row["lender"],
            "principal": to_rupees(row["principal"]), "interest_rate": f"{row['interest_rate']:g}",
            "tenure_months": row["tenure_months"], "start_date": row["start_date"],
            "emi_amount": to_rupees(row["emi_amount"]),
        }
    conn.close()

    return render_template("add_loan.html", active_page="toolkit", editing=True, form=form,
                           error=error, action_url=url_for("edit_loan", loan_id=loan_id),
                           back_url=url_for("loan_detail", loan_id=loan_id))


@app.route("/loans/<int:loan_id>")
def loan_detail(loan_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    row = own_loan(conn, loan_id)
    if not row:
        conn.close()
        return redirect(url_for("loan_tracker"))
    payments = conn.execute(
        "SELECT * FROM loan_payments WHERE loan_id=? ORDER BY emi_number DESC", (loan_id,)
    ).fetchall()
    conn.close()

    loan = build_loan(row)

    # Prepayment simulator: only when the person typed an extra monthly amount.
    sim, sim_error = None, None
    extra_raw = request.args.get("extra", "").strip()
    if extra_raw and not loan["is_cleared"]:
        try:
            extra = to_paise(extra_raw)
            if extra <= 0:
                raise ValueError
        except ValueError:
            extra, sim_error = None, "Please enter a positive extra amount."
        if extra:
            balance = outstanding_principal(row)
            normal = simulate_prepayment(balance, loan["interest_rate"], loan["emi"])
            faster = simulate_prepayment(balance, loan["interest_rate"], loan["emi"], extra)
            if normal is not None and normal[0] == 0:
                sim_error = "From the rate and EMI you entered, this loan should already be paid off, so there is nothing to simulate."
            elif normal is None or faster is None:
                sim_error = "With these numbers the EMI does not cover the interest, so the loan would never end. Please check the loan details."
            else:
                sim = {
                    "extra": extra,
                    "balance": round(balance),
                    "normal_months": normal[0], "normal_interest": normal[1],
                    "faster_months": faster[0], "faster_interest": faster[1],
                    "months_saved": normal[0] - faster[0],
                    "interest_saved": normal[1] - faster[1],
                    "normal_end": add_months(date.fromisoformat(row["start_date"]), row["emis_paid"] + normal[0]).strftime("%B %Y"),
                    "faster_end": add_months(date.fromisoformat(row["start_date"]), row["emis_paid"] + faster[0]).strftime("%B %Y"),
                }

    return render_template(
        "loan_detail.html",
        active_page="toolkit",
        loan=loan,
        payments=[{"number": p["emi_number"], "amount": p["amount"],
                   "due_date": p["due_date"], "paid_date": p["paid_date"]} for p in payments],
        extra_text=extra_raw,
        sim=sim,
        sim_error=sim_error
    )


@app.route("/loans/<int:loan_id>/paid", methods=["POST"])
def mark_emi_paid(loan_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    row = own_loan(conn, loan_id)
    if row and row["emis_paid"] < row["tenure_months"]:
        number = row["emis_paid"] + 1
        due = add_months(date.fromisoformat(row["start_date"]), number)
        # A late payment is recorded exactly like an on-time one: we only keep both dates.
        conn.execute(
            "INSERT INTO loan_payments (loan_id, emi_number, amount, due_date, paid_date) VALUES (?,?,?,?,?)",
            (loan_id, number, row["emi_amount"], due.isoformat(), date.today().isoformat())
        )
        conn.execute("UPDATE loans SET emis_paid=? WHERE id=?", (number, loan_id))
        conn.commit()
    conn.close()
    return redirect(url_for("loan_detail", loan_id=loan_id))


@app.route("/loans/<int:loan_id>/delete", methods=["POST"])
def delete_loan(loan_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    if own_loan(conn, loan_id):
        conn.execute("DELETE FROM loan_payments WHERE loan_id=?", (loan_id,))
        conn.execute("DELETE FROM loans WHERE id=?", (loan_id,))
        conn.commit()
    conn.close()
    return redirect(url_for("loan_tracker"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FINSIGHT_DEBUG") == "1")