from urllib.error import HTTPError
import json
from urllib.request import Request, urlopen
import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime, timedelta
import secrets
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get(
    "CAMPUSSPACE_SECRET_KEY",
    "temporary-development-key"
)
DATABASE = os.path.join(
    os.environ.get("DATA_DIR", "."),
    "campusspace.db"
)


# -----------------------------
# DATABASE
# -----------------------------

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection

def send_email_via_brevo(receiver_email, subject, body):
    api_key = os.environ.get("BREVO_API_KEY")

    if not api_key:
        raise Exception("BREVO_API_KEY is not configured.")

    email_data = {
        "sender": {
            "name": "BookMySlot",
            "email": "campusspace.college@gmail.com"
        },
        "to": [
            {
                "email": receiver_email
            }
        ],
        "subject": subject,
        "textContent": body
    }

    request = Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(email_data).encode("utf-8"),
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urlopen(request, timeout=20) as response:
            response.read()
            print("BREVO: Email sent successfully")

    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        print("BREVO ERROR:", error_body)
        raise

def send_otp_email(receiver_email, otp):

    subject = "BookMySlot Email Verification OTP"

    body = f"""
Hello,

Your BookMySlot verification OTP is:

{otp}

This OTP is valid for 10 minutes.

If you did not request this registration, you can ignore this email.

Regards,
BookMySlot
"""

    send_email_via_brevo(receiver_email, subject, body)

def send_admin_invitation_email(receiver_email, token):

    sender_email = os.environ.get("CAMPUSSPACE_EMAIL")

    invitation_link = (
        "https://campusspace-production-b1f4.up.railway.app/"
        "accept-admin-invitation/" + token
    )

    subject = "BookMySlot Admin Invitation"

    body = f"""
Hello,

You have been invited to become an administrator of BookMySlot.

Accept the invitation using this link:

{invitation_link}

This invitation is valid for 24 hours.

Regards,
BookMySlot
"""

    send_email_via_brevo(receiver_email, subject, body)
def init_db():
    connection = get_db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            room_type TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            floor TEXT NOT NULL
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            booking_time TEXT NOT NULL,
            duration INTEGER NOT NULL,
            people INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'student'
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS admin_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            invited_by INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            expires_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)



    connection.execute("""
        CREATE TABLE IF NOT EXISTS timetable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            subject TEXT NOT NULL,
            class_name TEXT NOT NULL,
            faculty_name TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        )
    """)

    user_columns = [
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    ]

    if "faculty_status" not in user_columns:
        connection.execute("""
            ALTER TABLE users
            ADD COLUMN faculty_status TEXT DEFAULT 'none'
        """)

    
    
    if "parent_email" not in user_columns:
        connection.execute("""
            ALTER TABLE users
            ADD COLUMN parent_email TEXT DEFAULT ''
        """)



    
    room_columns = [
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(rooms)"
        ).fetchall()
    ]

    if "approver_id" not in room_columns:
        connection.execute("""
            ALTER TABLE rooms
            ADD COLUMN approver_id INTEGER
        """)



    booking_columns = [
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(bookings)"
        ).fetchall()
    ]

    if "status" not in booking_columns:
        connection.execute("""
            ALTER TABLE bookings
            ADD COLUMN status TEXT DEFAULT 'confirmed'
        """)

    if "booker_role" not in booking_columns:
        connection.execute("""
            ALTER TABLE bookings
            ADD COLUMN booker_role TEXT DEFAULT 'student'
        """)

    
    if "approver_id" not in booking_columns:
        connection.execute("""
            ALTER TABLE bookings
            ADD COLUMN approver_id INTEGER
        """)

    if "approval_decided_at" not in booking_columns:
        connection.execute("""
            ALTER TABLE bookings
            ADD COLUMN approval_decided_at TEXT
        """)


    connection.commit()
    connection.close()


# -----------------------------
# HOME
# -----------------------------

@app.route("/")
def home():
    connection = get_db()

    try:
        rooms = connection.execute("""
            SELECT id, name, room_type, capacity, floor
            FROM rooms
            ORDER BY id
        """).fetchall()
    finally:
        connection.close()

    available_spaces = len(rooms)

    return render_template(
        "index.html",
        rooms=rooms,
        available_spaces=available_spaces
    )

# -----------------------------
# HELP & SUPPORT
# -----------------------------

@app.route("/help")
def help_page():
    return render_template("help.html")


# -----------------------------
# FIND ROOM
# -----------------------------


@app.route("/find-room")
def find_room():

    selected_date = request.args.get("date")
    selected_time = request.args.get("time")
    selected_duration = request.args.get("duration")
    selected_room_type = request.args.get("room_type", "")

    today = datetime.today().strftime("%Y-%m-%d")

    connection = get_db()

    # Check whether the logged-in user is approved faculty.
    is_approved_faculty = False

    if "user_id" in session:
        user = connection.execute("""
            SELECT role, faculty_status
            FROM users
            WHERE id = ?
        """, (session["user_id"],)).fetchone()

        if user:
            is_approved_faculty = (
                user["role"] == "faculty"
                and user["faculty_status"] == "approved"
            )


    room_categories = connection.execute("""
        SELECT DISTINCT room_type
        FROM rooms
        WHERE room_type IS NOT NULL AND room_type != ''
        ORDER BY room_type
    """).fetchall()

    room_categories = [row["room_type"] for row in room_categories]

    if selected_room_type:
        rooms = connection.execute("""
            SELECT id, name, room_type, capacity, floor
            FROM rooms
            WHERE room_type = ?
            ORDER BY id
        """, (selected_room_type,)).fetchall()
    else:
        rooms = connection.execute("""
            SELECT id, name, room_type, capacity, floor
            FROM rooms
            ORDER BY id
        """).fetchall()


    # No search submitted: show all rooms as before.
    if not selected_date or not selected_time or not selected_duration:
        connection.close()
        return render_template(
            "find_room.html",
            rooms=rooms,
            selected_date=selected_date,
            selected_time=selected_time,
            selected_duration=selected_duration,
            room_categories=room_categories,
            selected_room_type=selected_room_type,
            room_conflicts={},
            today=today
        )

    try:
        duration = int(selected_duration)

        requested_start = datetime.strptime(
            f"{selected_date} {selected_time}",
            "%Y-%m-%d %H:%M"
        )

    except (ValueError, TypeError):
        connection.close()
        flash("Invalid date, time, or duration selected.", "error")
        return redirect(url_for("find_room"))

    if duration not in (1, 2, 3, 4):
        connection.close()
        flash("Invalid duration selected.", "error")
        return redirect(url_for("find_room"))

    requested_end = requested_start + timedelta(hours=duration)
    day_name = requested_start.strftime("%A")

    display_rooms = list(rooms)
    room_conflicts = {}

    for room in rooms:

        room_id = room["id"]
        room_available = True
        student_conflict = False

        # Keep existing timetable restrictions.
        lectures = connection.execute("""
            SELECT start_time, end_time
            FROM timetable
            WHERE room_id = ? AND day = ?
        """, (room_id, day_name)).fetchall()

        for lecture in lectures:

            lecture_start = datetime.strptime(
                f"{selected_date} {lecture['start_time']}",
                "%Y-%m-%d %H:%M"
            )

            lecture_end = datetime.strptime(
                f"{selected_date} {lecture['end_time']}",
                "%Y-%m-%d %H:%M"
            )

            if (
                requested_start < lecture_end
                and requested_end > lecture_start
            ):
                room_available = False
                break

        if not room_available:
            continue

        # Check existing non-cancelled bookings.
        bookings = connection.execute("""
            SELECT booking_time, duration, booker_role
            FROM bookings
            WHERE room_id = ?
              AND booking_date = ?
              AND LOWER(status) != 'cancelled'
        """, (room_id, selected_date)).fetchall()

        for booking in bookings:

            booking_start = datetime.strptime(
                f"{selected_date} {booking['booking_time']}",
                "%Y-%m-%d %I:%M %p"
            )

            booking_end = booking_start + timedelta(
                hours=booking["duration"]
            )

            if (
                requested_start < booking_end
                and requested_end > booking_start
            ):

                if booking["booker_role"] == "student":
                    student_conflict = True
                else:
                    # Existing faculty or other non-student booking.
                    room_available = False
                    break

        if not room_available:
            room_conflicts[room_id] = (
                "Unavailable during the selected time"
            )

        elif student_conflict and not is_approved_faculty:
            room_conflicts[room_id] = (
                "Booked by a student during the selected time"
            )

    connection.close()

    return render_template(
        "find_room.html",
        rooms=display_rooms,
        selected_date=selected_date,
        selected_time=selected_time,
        selected_duration=selected_duration,
        room_categories=room_categories,
        selected_room_type=selected_room_type,
        room_conflicts=room_conflicts,
        today=today
    )


@app.route("/my-bookings")
def my_bookings():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db()

    bookings = connection.execute("""
        SELECT
            bookings.*,
            rooms.name AS room_name
        FROM bookings
        JOIN rooms
            ON bookings.room_id = rooms.id
        WHERE LOWER(TRIM(bookings.email)) = LOWER(TRIM(?))
        ORDER BY bookings.booking_date DESC, bookings.booking_time DESC
    """, (session["user_email"],)).fetchall()

    connection.close()

    return render_template(
        "my_bookings.html",
        bookings=bookings,
        email=session["user_email"]
    )

@app.route("/cancel-booking/<int:booking_id>")
def cancel_booking(booking_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db()

    connection.execute("""
        UPDATE bookings
        SET status = 'Cancelled'
        WHERE id = ?
        AND email = ?
    """, (
        booking_id,
        session["user_email"]
    ))

    connection.commit()
    connection.close()

    return redirect(url_for("my_bookings"))

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        password = request.form["password"]

        connection = get_db()

        user = connection.execute("""
            SELECT *
            FROM users
            WHERE email = ?
        """, (email,)).fetchone()

        connection.close()

        if user is None:
            flash("No account found with this email.", "error")
            return redirect(url_for("login"))

        if not check_password_hash(user["password"], password):
            flash("Incorrect password. Please try again.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_email"] = user["email"]
        session["user_role"] = user["role"]

        return redirect(url_for("home"))

    return render_template("login.html")

    @app.route("/logout")
    def logout():

        session.clear()

        return redirect(url_for("home"))

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        parent_email = request.form.get("parent_email", "").strip().lower()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]
        requested_role = request.form.get("requested_role", "student")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))
        if len(password) < 8 or not any(char.isdigit() for char in password):
            flash(
                "Password must be at least 8 characters long and contain at least one number.",
                "error"
            )
            return redirect(url_for("register"))

        connection = get_db()

        existing_user = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        connection.close()

        if existing_user:
            flash("An account with this email already exists. Please log in.", "error")
            return redirect(url_for("login"))

        # Generate 6-digit OTP
        otp = str(secrets.randbelow(900000) + 100000)
        otp_created_at = datetime.now().timestamp()

        # Store registration information temporarily in session
        session["registration_name"] = name
        session["registration_email"] = email
        session["registration_password"] = password
        session["registration_role"] = requested_role
        session["registration_parent_email"] = parent_email
        session["registration_otp"] = otp
        session["registration_otp_created_at"] = otp_created_at

        # Send OTP
        try:
            send_otp_email(email, otp)
        except Exception as e:
            print("OTP EMAIL ERROR:", e)

            session.pop("registration_name", None)
            session.pop("registration_email", None)
            session.pop("registration_password", None)
            session.pop("registration_role", None)
            session.pop("registration_otp", None)

            flash(
                "Unable to send verification email. Please check your email address and try again.",
                "error"
            )
            return redirect(url_for("register"))

        return redirect(url_for("verify_otp"))

    return render_template("register.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        email = request.form["email"].strip().lower()

        connection = get_db()

        user = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        connection.close()

        if user is None:
            flash("No account found with this email.", "error")
            return redirect(url_for("forgot_password"))

        otp = str(secrets.randbelow(900000) + 100000)

        session["reset_email"] = email
        session["reset_otp"] = otp
        session["reset_otp_created_at"] = datetime.now().timestamp()

        try:
            send_otp_email(email, otp)
        except Exception as e:
            print("PASSWORD RESET OTP ERROR:", e)

            session.pop("reset_email", None)
            session.pop("reset_otp", None)
            session.pop("reset_otp_created_at", None)

            flash("Unable to send OTP. Please try again.", "error")
            return redirect(url_for("forgot_password"))

        return redirect(url_for("reset_password"))

    return render_template("forgot_password.html")

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():

    if "reset_otp" not in session or "reset_email" not in session:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":

        entered_otp = request.form["otp"].strip()
        new_password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        otp_created_at = session.get("reset_otp_created_at")

        if datetime.now().timestamp() - otp_created_at > 600:
            session.pop("reset_email", None)
            session.pop("reset_otp", None)
            session.pop("reset_otp_created_at", None)

            flash("Your OTP has expired. Please request a new one.", "error")
            return redirect(url_for("forgot_password"))

        if entered_otp != session["reset_otp"]:
            flash("Invalid OTP. Please enter the correct OTP.", "error")
            return redirect(url_for("reset_password"))

        if new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password"))

        if len(new_password) < 8 or not any(char.isdigit() for char in new_password):
            flash(
                "Password must be at least 8 characters long and contain at least one number.",
                "error"
            )
            return redirect(url_for("reset_password"))

        hashed_password = generate_password_hash(new_password)

        connection = get_db()

        connection.execute(
            "UPDATE users SET password = ? WHERE email = ?",
            (hashed_password, session["reset_email"])
        )

        connection.commit()
        connection.close()

        session.pop("reset_email", None)
        session.pop("reset_otp", None)
        session.pop("reset_otp_created_at", None)

        flash("Password reset successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html")

@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():

    if "registration_otp" not in session:
        return redirect(url_for("register"))

    if request.method == "POST":

        otp_created_at = session.get("registration_otp_created_at")

        if otp_created_at is None:
            flash("OTP expired. Please register again.", "error")
            return redirect(url_for("register"))

        if datetime.now().timestamp() - otp_created_at > 600:

            session.pop("registration_name", None)
            session.pop("registration_email", None)
            session.pop("registration_password", None)
            session.pop("registration_role", None)
            session.pop("registration_otp", None)
            session.pop("registration_otp_created_at", None)

            flash("Your registration OTP has expired. Please register again.", "error")
            return redirect(url_for("register"))

        entered_otp = request.form["otp"].strip()

        if entered_otp != session["registration_otp"]:
            flash("Incorrect OTP. Please try again.", "error")
            return redirect(url_for("verify_otp"))


        # OTP is correct — create the account
        name = session["registration_name"]
        email = session["registration_email"]
        password = session["registration_password"]
        password = generate_password_hash(password)
        requested_role = session["registration_role"]
        parent_email = session.get("registration_parent_email", "")

        connection = get_db()

        if requested_role == "faculty":

            connection.execute("""
                INSERT INTO users
                (name, email, password, role, faculty_status, parent_email)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name,
                email,
                password,
                "student",
                "pending",
                parent_email
            ))

        else:

            connection.execute("""
                INSERT INTO users
                (name, email, password, role, faculty_status, parent_email)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name,
                email,
                password,
                "student",
                "none",
                parent_email
            ))

        connection.commit()
        connection.close()

        # Clear temporary registration information
        session.pop("registration_name", None)
        session.pop("registration_email", None)
        session.pop("registration_password", None)
        session.pop("registration_role", None)
        session.pop("registration_parent_email", None)
        session.pop("registration_otp", None)
        session.pop("registration_otp_created_at", None)

        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("verify_otp.html")

@app.route("/resend-otp")
def resend_otp():

    if "registration_email" not in session:
        return redirect(url_for("register"))

    # Generate a new OTP
    otp = str(secrets.randbelow(900000) + 100000)

    # Reset OTP timer
    session["registration_otp"] = otp
    session["registration_otp_created_at"] = datetime.now().timestamp()

    email = session["registration_email"]

    try:
        send_otp_email(email, otp)

    except Exception as e:
        print("RESEND OTP ERROR:", e)

        flash("Unable to resend OTP. Please try again.", "error")
        return redirect(url_for("verify_otp"))

    return redirect(url_for("verify_otp"))

# -----------------------------
# BOOK ROOM
# -----------------------------


@app.route("/book-room", methods=["GET", "POST"])
def book_room():

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_role = session.get("user_role", "student")

    if user_role == "admin":
        user_role = "admin"

    room_id = request.args.get("room_id", "1")

    connection = get_db()

    room = connection.execute("""
        SELECT *
        FROM rooms
        WHERE id = ?
    """, (room_id,)).fetchone()

    connection.close()

    if room is None:
        flash("Room not found.", "error")
        return redirect(url_for("find_room"))

    if request.method == "POST":

        user_id = session["user_id"]


        connection = get_db()

        logged_in_user = connection.execute("""
            SELECT name, email, role, faculty_status, parent_phone
            FROM users
            WHERE id = ?
        """, (user_id,)).fetchone()

        connection.close()



        if logged_in_user is None:
            flash("User account not found.", "error")
            return redirect(url_for("home"))

        name = logged_in_user["name"]
        email = logged_in_user["email"]
        user_role = logged_in_user["role"]

        
        # Student bookings require an assigned, approved faculty approver.
        if logged_in_user["role"] == "student":
            approver_id = room["approver_id"]

            if not approver_id:
                flash(
                    "This room does not have an assigned approver yet. "
                    "Please contact the administrator.",
                    "error"
                )
                return redirect(url_for("find_room"))

            approver_connection = get_db()
            approver = approver_connection.execute("""
                SELECT id, name, email
                FROM users
                WHERE id = ?
                  AND role = 'faculty'
                  AND faculty_status = 'approved'
            """, (approver_id,)).fetchone()
            approver_connection.close()

            if approver is None:
                flash(
                    "This room's assigned approver is no longer an approved faculty member. "
                    "Please contact the administrator.",
                    "error"
                )
                return redirect(url_for("find_room"))


        # Verify faculty approval before granting priority.
        if (
            logged_in_user["role"] == "faculty"
            and logged_in_user["faculty_status"] != "approved"
        ):
            flash("Your faculty account is not approved yet.", "error")
            return redirect(url_for("find_room"))

        
        booking_date = request.form.get("date", "").strip()
        booking_time = request.form.get("time", "").strip()
        duration_text = request.form.get("duration", "").strip()
        people_text = request.form.get("people", "").strip()
        purpose = request.form.get("purpose", "").strip()

        # Validate the booking date and time.
        try:
            requested_start = datetime.strptime(
                f"{booking_date} {booking_time}",
                "%Y-%m-%d %I:%M %p"
            )
        except ValueError:
            flash("Please select a valid booking date and time.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        # Prevent bookings in the past.
        if requested_start < datetime.now():
            flash("You cannot book a room for a past date or time.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        # Validate duration.
        try:
            duration = int(duration_text.split()[0])
        except (ValueError, IndexError):
            flash("Please select a valid booking duration.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        if duration not in (1, 2, 3, 4):
            flash("Please select a duration between 1 and 4 hours.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        # Validate number of people against the room's capacity.
        try:
            people = int(people_text)
        except ValueError:
            flash("Please enter a valid number of people.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        if people < 1 or people > room["capacity"]:
            flash(
                f"This room can accommodate 1 to {room['capacity']} people.",
                "error"
            )
            return redirect(url_for("book_room", room_id=room_id))

        if not purpose:
            flash("Please enter the purpose of your booking.", "error")
            return redirect(url_for("book_room", room_id=room_id))

        # Reopen the database connection for booking checks and saving.
        connection = get_db()

        # Get existing bookings for this room and date
        # Get existing confirmed bookings for this room and date
        
        existing_bookings = connection.execute("""
            SELECT id, booking_time, duration, booker_role, status
            FROM bookings
            WHERE room_id = ?
            AND booking_date = ?
            AND LOWER(status) IN ('confirmed', 'pending')
        """, (room_id, booking_date)).fetchall()

        # Convert requested time into datetime
        requested_start = datetime.strptime(
            f"{booking_date} {booking_time}",
            "%Y-%m-%d %I:%M %p"
        )

        requested_end = requested_start + timedelta(hours=duration)

        # Check for timetable conflicts
        day_name = requested_start.strftime("%A")

        lectures = connection.execute("""
            SELECT start_time, end_time
            FROM timetable
            WHERE room_id = ?
            AND day = ?
        """, (
            room_id,
            day_name
        )).fetchall()

        for lecture in lectures:

            lecture_start = datetime.strptime(
                f"{booking_date} {lecture['start_time']}",
                "%Y-%m-%d %H:%M"
            )

            lecture_end = datetime.strptime(
                f"{booking_date} {lecture['end_time']}",
                "%Y-%m-%d %H:%M"
            )


            if (
                requested_start < lecture_end
                and requested_end > lecture_start
            ):
                connection.close()
                flash("This room is reserved for a lecture during the selected time.", "error")
                return redirect(url_for("find_room"))



        # Check for overlapping bookings
        student_bookings_to_cancel = []
        cancelled_student_bookings = []

        
        for booking in existing_bookings:

            existing_start = datetime.strptime(
                f"{booking_date} {booking['booking_time']}",
                "%Y-%m-%d %I:%M %p"
            )

            existing_end = existing_start + timedelta(
                hours=booking["duration"]
            )

            if requested_start < existing_end and requested_end > existing_start:

                existing_role = booking["booker_role"]
                existing_status = booking["status"].lower()

                # Approved faculty can take priority over student bookings.
                if (
                    user_role == "faculty"
                    and logged_in_user["role"] == "faculty"
                    and logged_in_user["faculty_status"] == "approved"
                    and existing_role == "student"
                ):
                    student_bookings_to_cancel.append(booking["id"])
                    continue

                # Students cannot overlap an existing faculty booking.
                elif user_role == "student" and existing_role == "faculty":
                    connection.close()
                    flash(
                        "This room is already reserved by faculty for the selected time.",
                        "error"
                    )
                    return redirect(url_for("find_room"))

                # All other overlapping bookings are blocked.
                else:
                    connection.close()
                    flash(
                        "This room already has a booking or pending request for the selected time.",
                        "error"
                    )
                    return redirect(url_for("find_room"))



            
        # Save booking
        # Students must wait for approval; faculty bookings are confirmed.
        if user_role == "student":
            booking_status = "Pending"
            booking_approver_id = room["approver_id"]
        else:
            booking_status = "Confirmed"
            booking_approver_id = None

        connection.execute("""
            INSERT INTO bookings
            (
                room_id,
                name,
                email,
                booking_date,
                booking_time,
                duration,
                people,
                purpose,
                booker_role,
                status,
                approver_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            room_id,
            name,
            email,
            booking_date,
            booking_time,
            duration,
            people,
            purpose,
            user_role,
            booking_status,
            booking_approver_id
        ))




        # Save the new booking first.

        # Cancel overlapping student bookings only after
        # the faculty booking has been saved successfully.
        for student_booking_id in student_bookings_to_cancel:

            student_booking = connection.execute("""
                SELECT name, email, booking_date, booking_time
                FROM bookings
                WHERE id = ?
            """, (student_booking_id,)).fetchone()

            connection.execute("""
                UPDATE bookings
                SET status = 'Cancelled'
                WHERE id = ?
            """, (student_booking_id,))

            if student_booking:
                cancelled_student_bookings.append({
                    "name": student_booking["name"],
                    "email": student_booking["email"],
                    "booking_date": student_booking["booking_date"],
                    "booking_time": student_booking["booking_time"]
                })

        connection.commit()
        connection.close()

        
        # Notify the assigned approver about a new student request.
        if booking_status == "Pending":
            try:
                approval_link = url_for(
                    "faculty_booking_requests",
                    _external=True
                )

                send_email_via_brevo(
                    approver["email"],
                    "BookMySlot: Student Booking Request Needs Review",
                    f"""Hello {approver['name']},

A student has submitted a room booking request that requires your review.

Student: {name}
Room: {room['name']}
Date: {booking_date}
Time: {booking_time}
Duration: {duration} hour(s)
Purpose: {purpose}

Please log in to BookMySlot to review this request:
{approval_link}

Regards,
BookMySlot"""
                )
            except Exception as e:
                print("APPROVER NOTIFICATION EMAIL ERROR:", e)



        # Notify students whose bookings were cancelled
        # because an approved faculty member received priority.
        for student_booking in cancelled_student_bookings:
            try:
                send_email_via_brevo(
                    student_booking["email"],
                    "BookMySlot Booking Cancelled — Faculty Priority",
                    f"""Hello {student_booking['name']},

Your BookMySlot room booking has been cancelled because an approved faculty booking takes priority for an overlapping time slot.

Date: {student_booking['booking_date']}
Time: {student_booking['booking_time']}

Please visit BookMySlot to find another available room.

Regards,
BookMySlot
"""
                )
            except Exception as e:
                print("FACULTY PRIORITY EMAIL ERROR:", e)

        
        return render_template(
            "booking_confirmation.html",
            name=name,
            room=room,
            booking_date=booking_date,
            booking_time=booking_time,
            duration=duration,
            booking_status=booking_status
        )


    return render_template("book_room.html", room=room)

@app.route("/admin/cancel-booking/<int:booking_id>", methods=["POST"])
def admin_cancel_booking(booking_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    booking = connection.execute("""
        SELECT id
        FROM bookings
        WHERE id = ?
    """, (booking_id,)).fetchone()

    if booking is None:
        connection.close()
        flash("Booking not found.", "error")
        return redirect(url_for("admin"))

    connection.execute("""
        UPDATE bookings
        SET status = 'Cancelled'
        WHERE id = ?
    """, (booking_id,))

    connection.commit()
    connection.close()

    return redirect(url_for("admin"))
# -----------------------------
# START APPLICATION
# -----------------------------

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/admin")
def admin():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()


    total_rooms = connection.execute("""
        SELECT COUNT(*) AS count
        FROM rooms
    """).fetchone()["count"]

    today = datetime.now().strftime("%Y-%m-%d")

    today_bookings = connection.execute("""
        SELECT COUNT(*) AS count
        FROM bookings
        WHERE booking_date = ?
    """, (today,)).fetchone()["count"]

    total_users = connection.execute("""
        SELECT COUNT(*) AS count
        FROM users
    """).fetchone()["count"]

    faculty_requests = connection.execute("""
    SELECT id, name, email, faculty_status
    FROM users
    WHERE faculty_status = 'pending'
    ORDER BY id DESC
""").fetchall()

    rooms = connection.execute("""
        SELECT *
        FROM rooms
        ORDER BY id
    """).fetchall()

    recent_bookings = connection.execute("""
        SELECT
            bookings.*,
            rooms.name AS room_name
        FROM bookings
        JOIN rooms
            ON bookings.room_id = rooms.id
        ORDER BY bookings.id DESC
        LIMIT 10
    """).fetchall()

    connection.close()

    return render_template(
        "admin.html",
        total_rooms=total_rooms,
        today_bookings=today_bookings,
        total_users=total_users,
        rooms=rooms,
        recent_bookings=recent_bookings,
        faculty_requests=faculty_requests
    )


@app.route("/faculty/booking-requests")
def faculty_booking_requests():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "faculty":
        flash("Access denied. Faculty only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    requests = connection.execute("""
        SELECT
            bookings.*,
            rooms.name AS room_name
        FROM bookings
        JOIN rooms ON bookings.room_id = rooms.id
        WHERE bookings.approver_id = ?
          AND LOWER(bookings.status) = 'pending'
        ORDER BY bookings.booking_date, bookings.booking_time
    """, (session["user_id"],)).fetchall()

    connection.close()

    return render_template(
        "faculty_booking_requests.html",
        requests=requests
    )


@app.route("/faculty/reject-booking/<int:booking_id>", methods=["POST"])
def reject_booking(booking_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "faculty":
        flash("Access denied. Faculty only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    faculty = connection.execute("""
        SELECT id
        FROM users
        WHERE id = ?
          AND role = 'faculty'
          AND faculty_status = 'approved'
    """, (session["user_id"],)).fetchone()

    if faculty is None:
        connection.close()
        flash("Your faculty account is not approved.", "error")
        return redirect(url_for("home"))

    booking = connection.execute("""
        SELECT bookings.id, bookings.name, bookings.email,
               bookings.booking_date, bookings.booking_time,
               rooms.name AS room_name
        FROM bookings
        JOIN rooms ON rooms.id = bookings.room_id
        WHERE bookings.id = ?
          AND bookings.approver_id = ?
          AND LOWER(bookings.status) = 'pending'
    """, (booking_id, session["user_id"])).fetchone()

    if booking is None:
        connection.close()
        flash(
            "This pending request was not found or is not assigned to you.",
            "error"
        )
        return redirect(url_for("faculty_booking_requests"))

    connection.execute("""
        UPDATE bookings
        SET status = 'Rejected',
            approval_decided_at = ?
        WHERE id = ?
          AND approver_id = ?
          AND LOWER(status) = 'pending'
    """, (
        datetime.now().isoformat(timespec="seconds"),
        booking_id,
        session["user_id"]
    ))

    connection.commit()
    connection.close()

    try:
        send_email_via_brevo(
            booking["email"],
            "BookMySlot Booking Request Rejected",
            f"""Hello {booking['name']},

Your BookMySlot booking request has been rejected.

Room: {booking['room_name']}
Date: {booking['booking_date']}
Time: {booking['booking_time']}

Please visit BookMySlot to choose another available time or room.

Regards,
BookMySlot"""
        )
    except Exception as e:
        print("BOOKING REJECTION EMAIL ERROR:", e)

    flash("Booking request rejected.", "success")
    return redirect(url_for("faculty_booking_requests"))


@app.route("/faculty/approve-booking/<int:booking_id>", methods=["POST"])
def approve_booking(booking_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "faculty":
        flash("Access denied. Faculty only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    faculty = connection.execute("""
        SELECT id
        FROM users
        WHERE id = ?
          AND role = 'faculty'
          AND faculty_status = 'approved'
    """, (session["user_id"],)).fetchone()

    if faculty is None:
        connection.close()
        flash("Your faculty account is not approved.", "error")
        return redirect(url_for("home"))

    
    booking = connection.execute("""
        SELECT
            bookings.*,
            rooms.name AS room_name,
            users.parent_email AS parent_email
        FROM bookings
        JOIN rooms ON rooms.id = bookings.room_id
        JOIN users ON LOWER(users.email) = LOWER(bookings.email)
        WHERE bookings.id = ?
          AND bookings.approver_id = ?
          AND LOWER(bookings.status) = 'pending'
    """, (booking_id, session["user_id"])).fetchone()


    if booking is None:
        connection.close()
        flash(
            "This pending request was not found or is not assigned to you.",
            "error"
        )
        return redirect(url_for("faculty_booking_requests"))

    try:
        requested_start = datetime.strptime(
            f"{booking['booking_date']} {booking['booking_time']}",
            "%Y-%m-%d %I:%M %p"
        )
    except ValueError:
        connection.close()
        flash("The booking has an invalid date or time.", "error")
        return redirect(url_for("faculty_booking_requests"))

    requested_end = requested_start + timedelta(hours=booking["duration"])

    # Recheck the room timetable before confirming.
    day_name = requested_start.strftime("%A")

    lectures = connection.execute("""
        SELECT start_time, end_time
        FROM timetable
        WHERE room_id = ?
          AND day = ?
    """, (booking["room_id"], day_name)).fetchall()

    for lecture in lectures:
        lecture_start = datetime.strptime(
            f"{booking['booking_date']} {lecture['start_time']}",
            "%Y-%m-%d %H:%M"
        )
        lecture_end = datetime.strptime(
            f"{booking['booking_date']} {lecture['end_time']}",
            "%Y-%m-%d %H:%M"
        )

        if requested_start < lecture_end and requested_end > lecture_start:
            connection.close()
            flash(
                "This request conflicts with a scheduled lecture and cannot be approved.",
                "error"
            )
            return redirect(url_for("faculty_booking_requests"))

    # Recheck confirmed bookings in case the slot was taken meanwhile.
    existing_bookings = connection.execute("""
        SELECT id, booking_time, duration, booker_role
        FROM bookings
        WHERE room_id = ?
          AND booking_date = ?
          AND LOWER(status) = 'confirmed'
          AND id != ?
    """, (
        booking["room_id"],
        booking["booking_date"],
        booking_id
    )).fetchall()

    for existing in existing_bookings:
        existing_start = datetime.strptime(
            f"{booking['booking_date']} {existing['booking_time']}",
            "%Y-%m-%d %I:%M %p"
        )
        existing_end = existing_start + timedelta(hours=existing["duration"])

        if requested_start < existing_end and requested_end > existing_start:
            connection.close()
            flash(
                "This request now conflicts with a confirmed booking and cannot be approved.",
                "error"
            )
            return redirect(url_for("faculty_booking_requests"))

    # Confirm only if the request is still pending and assigned to this faculty member.
    cursor = connection.execute("""
        UPDATE bookings
        SET status = 'Confirmed',
            approval_decided_at = ?
        WHERE id = ?
          AND approver_id = ?
          AND LOWER(status) = 'pending'
    """, (
        datetime.now().isoformat(timespec="seconds"),
        booking_id,
        session["user_id"]
    ))

    if cursor.rowcount != 1:
        connection.close()
        flash(
            "This request has already been processed. Please refresh the page.",
            "error"
        )
        return redirect(url_for("faculty_booking_requests"))

    connection.commit()
    connection.close()

    try:
        send_email_via_brevo(
            booking["email"],
            "BookMySlot Booking Approved",
            f"""Hello {booking['name']},

Your BookMySlot booking request has been approved!

Room: {booking['room_name']}
Date: {booking['booking_date']}
Time: {booking['booking_time']}
Duration: {booking['duration']} hour(s)

Regards,
BookMySlot"""
        )
    except Exception as e:
        print("BOOKING APPROVAL EMAIL ERROR:", e)

    
    # Notify the parent/guardian only after the booking is approved.
    parent_email = (booking["parent_email"] or "").strip()

    if parent_email:
        try:
            send_email_via_brevo(
                parent_email,
                "BookMySlot: Your Child's Booking Has Been Approved",
                f"""Hello Parent/Guardian,

This is to inform you that your child's room booking has been approved.

Student: {booking['name']}
Room: {booking['room_name']}
Date: {booking['booking_date']}
Time: {booking['booking_time']}
Duration: {booking['duration']} hour(s)

Regards,
BookMySlot"""
            )
        except Exception as e:
            print("PARENT APPROVAL EMAIL ERROR:", e)
    else:
        print("PARENT APPROVAL EMAIL SKIPPED: No parent email registered.")


    flash("Booking approved successfully.", "success")
    return redirect(url_for("faculty_booking_requests"))


@app.route("/admin/invite-admin", methods=["GET", "POST"])
def invite_admin():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":

        email = request.form["email"].strip().lower()

        if not email:
            return "Email is required. <a href='/admin/invite-admin'>Go back</a>", 400

        token = secrets.token_urlsafe(32)

        expires_at = (
            datetime.now() + timedelta(hours=24)
        ).strftime("%Y-%m-%d %H:%M:%S")

        connection = get_db()

        connection.execute("""
            INSERT INTO admin_invitations
            (email, token, invited_by, expires_at)
            VALUES (?, ?, ?, ?)
        """, (
            email,
            token,
            session["user_id"],
            expires_at
        ))

        connection.commit()
        connection.close()

        send_admin_invitation_email(email, token)

        flash("Admin invitation created successfully.", "success")
        return redirect(url_for("invite_admin"))

    return render_template("invite_admin.html")

@app.route(
    "/accept-admin-invitation/<token>",
    methods=["GET", "POST"]
)
def accept_admin_invitation(token):

    connection = get_db()

    invitation = connection.execute("""
        SELECT *
        FROM admin_invitations
        WHERE token = ?
        AND status = 'pending'
    """, (token,)).fetchone()

    connection.close()

    if invitation is None:
        flash("This invitation is invalid or has already been used.", "error")
        return redirect(url_for("home"))

    expires_at = datetime.strptime(
        invitation["expires_at"],
        "%Y-%m-%d %H:%M:%S"
    )

    if datetime.now() > expires_at:
        flash("This invitation has expired.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":

        name = request.form["name"].strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if not name:
            flash("Name is required. Please try again.", "error")
            return redirect(url_for("accept_admin_invitation", token=token))

        if len(password) < 8 or not any(
            character.isdigit() for character in password
        ):
            flash(
                "Password must be at least 8 characters long and contain at least one number.",
                "error"
            )
            return redirect(url_for("accept_admin_invitation", token=token))

        if password != confirm_password:
            flash("Passwords do not match. Please try again.", "error")
            return redirect(url_for("accept_admin_invitation", token=token))

        hashed_password = generate_password_hash(password)

        connection = get_db()

        existing_user = connection.execute("""
            SELECT id
            FROM users
            WHERE email = ?
        """, (invitation["email"],)).fetchone()

        if existing_user:
            connection.execute("""
                UPDATE users
                SET name = ?, password = ?, role = 'admin'
                WHERE email = ?
            """, (
                name,
                hashed_password,
                invitation["email"]
            ))
        else:
            connection.execute("""
                INSERT INTO users
                (name, email, password, role, faculty_status)
                VALUES (?, ?, ?, 'admin', 'approved')
            """, (
                name,
                invitation["email"],
                hashed_password
            ))

        connection.execute("""
            UPDATE admin_invitations
            SET status = 'accepted'
            WHERE token = ?
        """, (token,))

        connection.commit()
        connection.close()

        flash("Admin account created successfully. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template(
        "accept_admin_invitation.html",
        invitation_email=invitation["email"]
    )

@app.route("/admin/approve-faculty/<int:user_id>")
def approve_faculty(user_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    connection.execute("""
        UPDATE users
        SET role = 'faculty',
            faculty_status = 'approved'
        WHERE id = ?
          AND faculty_status = 'pending'
    """, (user_id,))

    connection.commit()
    connection.close()

    return redirect(url_for("admin"))

@app.route("/admin/reject-faculty/<int:user_id>")
def reject_faculty(user_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    connection.execute("""
        UPDATE users
        SET role = 'student',
            faculty_status = 'rejected'
        WHERE id = ?
          AND faculty_status = 'pending'
    """, (user_id,))

    connection.commit()
    connection.close()

    return redirect(url_for("admin"))

@app.route("/admin/timetable", methods=["GET", "POST"])
def manage_timetable():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    # Add a lecture
    if request.method == "POST":

        room_id = request.form["room_id"]
        day = request.form["day"]
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]
        subject = request.form["subject"]
        class_name = request.form["class_name"]
        faculty_name = request.form["faculty_name"]

        connection.execute("""
            INSERT INTO timetable
            (
                room_id,
                day,
                start_time,
                end_time,
                subject,
                class_name,
                faculty_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            room_id,
            day,
            start_time,
            end_time,
            subject,
            class_name,
            faculty_name
        ))

        connection.commit()
        connection.close()

        return redirect(url_for("manage_timetable"))

    rooms = connection.execute("""
        SELECT *
        FROM rooms
        ORDER BY name
    """).fetchall()

    timetable = connection.execute("""
        SELECT
            timetable.*,
            rooms.name AS room_name
        FROM timetable
        JOIN rooms
            ON timetable.room_id = rooms.id
        ORDER BY
            CASE timetable.day
                WHEN 'Monday' THEN 1
                WHEN 'Tuesday' THEN 2
                WHEN 'Wednesday' THEN 3
                WHEN 'Thursday' THEN 4
                WHEN 'Friday' THEN 5
                WHEN 'Saturday' THEN 6
                WHEN 'Sunday' THEN 7
            END,
            timetable.start_time
    """).fetchall()

    connection.close()

    return render_template(
        "admin_timetable.html",
        rooms=rooms,
        timetable=timetable
    )

@app.route("/admin/delete-timetable/<lecture_id>", methods=["POST"])
def delete_timetable(lecture_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    connection.execute("""
        DELETE FROM timetable
        WHERE id = ?
    """, (lecture_id,))

    connection.commit()
    connection.close()

    return redirect(url_for("manage_timetable"))

@app.route("/admin/edit-timetable/<lecture_id>", methods=["GET", "POST"])
def edit_timetable(lecture_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    lecture = connection.execute("""
        SELECT *
        FROM timetable
        WHERE id = ?
    """, (lecture_id,)).fetchone()

    if lecture is None:
        connection.close()
        flash("Lecture not found.", "error")
        return redirect(url_for("manage_timetable"))

    if request.method == "POST":

        room_id = request.form["room_id"]
        day = request.form["day"]
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]
        subject = request.form["subject"]
        class_name = request.form["class_name"]
        faculty_name = request.form["faculty_name"]

        connection.execute("""
            UPDATE timetable
            SET
                room_id = ?,
                day = ?,
                start_time = ?,
                end_time = ?,
                subject = ?,
                class_name = ?,
                faculty_name = ?
            WHERE id = ?
        """, (
            room_id,
            day,
            start_time,
            end_time,
            subject,
            class_name,
            faculty_name,
            lecture_id
        ))

        connection.commit()
        connection.close()

        return redirect(url_for("manage_timetable"))

    rooms = connection.execute("""
        SELECT *
        FROM rooms
        ORDER BY name
    """).fetchall()

    connection.close()

    return render_template(
        "edit_timetable.html",
        lecture=lecture,
        rooms=rooms
    )


@app.route("/add-room", methods=["GET", "POST"])
def add_room():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    if request.method == "POST":

        name = request.form["name"].strip()
        room_type = request.form["room_type"].strip()
        capacity = request.form["capacity"].strip()
        floor = request.form["floor"].strip()
        approver_id = request.form.get("approver_id", "").strip()

        if not capacity.isdigit() or int(capacity) <= 0:
            connection.close()
            flash("Capacity must be a positive number.", "error")
            return redirect(url_for("add_room"))

        capacity = int(capacity)

        if approver_id and not approver_id.isdigit():
            connection.close()
            flash("Please select a valid booking approver.", "error")
            return redirect(url_for("add_room"))

        approver_id = int(approver_id) if approver_id else None

        if approver_id is not None:
            approved_faculty = connection.execute("""
                SELECT id
                FROM users
                WHERE id = ?
                  AND role = 'faculty'
                  AND faculty_status = 'approved'
            """, (approver_id,)).fetchone()

            if approved_faculty is None:
                connection.close()
                flash("Please select an approved faculty member.", "error")
                return redirect(url_for("add_room"))

        connection.execute("""
            INSERT INTO rooms
            (name, room_type, capacity, floor, approver_id)
            VALUES (?, ?, ?, ?, ?)
        """, (
            name,
            room_type,
            capacity,
            floor,
            approver_id
        ))

        connection.commit()
        connection.close()

        return redirect(url_for("admin"))

    approved_faculty = connection.execute("""
        SELECT id, name, email
        FROM users
        WHERE role = 'faculty'
          AND faculty_status = 'approved'
        ORDER BY name
    """).fetchall()

    connection.close()

    return render_template(
        "add_room.html",
        approved_faculty=approved_faculty
    )


@app.route("/edit-room/<int:room_id>", methods=["GET", "POST"])
def edit_room(room_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    room = connection.execute("""
        SELECT *
        FROM rooms
        WHERE id = ?
    """, (room_id,)).fetchone()

    if room is None:
        connection.close()
        flash("Room not found.", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":

        name = request.form["name"].strip()
        room_type = request.form["room_type"].strip()
        capacity = request.form["capacity"].strip()

        if not capacity.isdigit() or int(capacity) <= 0:
            flash("Capacity must be a positive number.", "error")
            return redirect(url_for("edit_room", room_id=room_id))

        
        capacity = int(capacity)
        floor = request.form["floor"].strip()
        approver_id = request.form.get("approver_id", "").strip()

        if approver_id and not approver_id.isdigit():
            flash("Please select a valid booking approver.", "error")
            return redirect(url_for("edit_room", room_id=room_id))

        approver_id = int(approver_id) if approver_id else None


        
        connection.execute("""
            UPDATE rooms
            SET name = ?,
                room_type = ?,
                capacity = ?,
                floor = ?,
                approver_id = ?
            WHERE id = ?
        """, (
            name,
            room_type,
            capacity,
            floor,
            approver_id,
            room_id
        ))


        connection.commit()
        connection.close()

        return redirect(url_for("admin"))

    
    approved_faculty = connection.execute("""
        SELECT id, name, email
        FROM users
        WHERE role = 'faculty'
          AND faculty_status = 'approved'
        ORDER BY name
    """).fetchall()

    connection.close()

    return render_template(
        "edit_room.html",
        room=room,
        approved_faculty=approved_faculty
    )


@app.route("/delete-room/<int:room_id>", methods=["POST"])
def delete_room(room_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("user_role") != "admin":
        flash("Access denied. Admin only.", "error")
        return redirect(url_for("home"))

    connection = get_db()

    # Check whether this room has any bookings
    booking = connection.execute("""
        SELECT id
        FROM bookings
        WHERE room_id = ?
        LIMIT 1
    """, (room_id,)).fetchone()

    if booking:
        connection.close()
        flash("Room cannot be deleted because it has existing bookings.", "error")
        return redirect(url_for("admin"))

    # Delete the room
    connection.execute("""
        DELETE FROM rooms
        WHERE id = ?
    """, (room_id,))

    connection.commit()
    connection.close()

    return redirect(url_for("admin"))

init_db()

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )

