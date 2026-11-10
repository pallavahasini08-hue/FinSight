"""Current FinSight schema. Money columns are INTEGER paise."""

SCHEMA_VERSION = 5

# {name} is filled in with the table name when the table is created.
TABLES = {
    "users": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            vault_pin_hash TEXT,
            vault_failed INTEGER NOT NULL DEFAULT 0,
            vault_retry_at REAL NOT NULL DEFAULT 0
        )""",
    "budgets": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            monthly_limit INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    # One budget per user per month (month looks like '2026-10').
    "monthly_budgets": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            monthly_limit INTEGER NOT NULL,
            UNIQUE (user_id, month),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "goals": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            target_amount INTEGER NOT NULL,
            current_amount INTEGER NOT NULL DEFAULT 0,
            target_date TEXT,
            monthly_target INTEGER,
            is_priority INTEGER DEFAULT 0,
            is_achieved INTEGER DEFAULT 0,
            created_date TEXT,
            milestone_celebrated INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "contributions": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            date TEXT NOT NULL,
            FOREIGN KEY (goal_id) REFERENCES goals(id)
        )""",
    "documents": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            issue_date TEXT,
            expiry_date TEXT,
            notes TEXT,
            is_archived INTEGER NOT NULL DEFAULT 0,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            last_viewed TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "loans": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            lender TEXT NOT NULL,
            principal INTEGER NOT NULL,
            interest_rate REAL NOT NULL,
            tenure_months INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            emi_amount INTEGER NOT NULL,
            emis_paid INTEGER NOT NULL DEFAULT 0,
            created_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "loan_payments": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            emi_number INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            due_date TEXT NOT NULL,
            paid_date TEXT NOT NULL,
            FOREIGN KEY (loan_id) REFERENCES loans(id)
        )""",
    "income": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            amount INTEGER NOT NULL,
            date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "expenses": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "lending": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            person_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            lent_date TEXT,
            interest REAL,
            notes TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'received')) DEFAULT 'pending',
            received_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "bills": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('paid', 'unpaid')) DEFAULT 'unpaid',
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    "bill_payments": """
        CREATE TABLE IF NOT EXISTS {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            amount INTEGER NOT NULL,
            due_date TEXT NOT NULL,
            is_paid INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (bill_id) REFERENCES bills(id)
        )""",
}

# Money columns (paise) per table.
MONEY_COLUMNS = {
    "budgets": ["monthly_limit"],
    "monthly_budgets": ["monthly_limit"],
    "goals": ["target_amount", "current_amount", "monthly_target"],
    "contributions": ["amount"],
    "income": ["amount"],
    "expenses": ["amount"],
    "lending": ["amount"],
    "bills": ["amount"],
    "bill_payments": ["amount"],
    "loans": ["principal", "emi_amount"],
    "loan_payments": ["amount"],
}

# Like a book index: the database jumps straight to a user's rows for a date
# range instead of reading every row.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_income_user_date ON income(user_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_budgets_user ON budgets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id, is_achieved)",
    "CREATE INDEX IF NOT EXISTS idx_contributions_goal ON contributions(goal_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_lending_user ON lending(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_bills_user_due ON bills(user_id, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, category, is_archived)",
    "CREATE INDEX IF NOT EXISTS idx_loans_user ON loans(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_loan_payments_loan ON loan_payments(loan_id, emi_number)",
    "CREATE INDEX IF NOT EXISTS idx_bill_payments_bill_month ON bill_payments(bill_id, month)",
]