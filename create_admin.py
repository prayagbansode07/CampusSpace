from getpass import getpass
from app import get_db
from werkzeug.security import generate_password_hash

name = input("Admin name: ").strip()
email = input("Admin email: ").strip().lower()
password = getpass("Admin password: ")
confirm = getpass("Confirm password: ")

if not name or not email or not password:
    print("Error: all fields are required.")
elif password != confirm:
    print("Error: passwords do not match.")
else:
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?",
            (email,)
        ).fetchone()

        if existing:
            print("An account with that email already exists.")
        else:
            db.execute(
                """INSERT INTO users
                   (name, email, password, role, faculty_status)
                   VALUES (?, ?, ?, 'admin', 'approved')""",
                (name, email, generate_password_hash(password))
            )
            db.commit()
            print("Admin account created successfully!")
    finally:
        db.close()
