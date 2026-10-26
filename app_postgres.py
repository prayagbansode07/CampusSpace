from urllib.error import HTTPError
import json
from urllib.request import Request, urlopen
import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import psycopg
from datetime import datetime, timedelta
import secrets
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get(
    "BOOKMYSLOT_SECRET_KEY",
    "temporary-development-key"
)
DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE = "campusspace.db"


# -----------------------------
# DATABASE
# -----------------------------

class HybridRow(dict):
    """Row supports both SQLite-style numeric and named column access."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def postgres_row_factory(cursor):
    if cursor.description is None:
        return lambda values: values

    column_names = [column.name for column in cursor.description]

    def make_row(values):
        return HybridRow(zip(column_names, values))

    return make_row


class PostgresConnection:
    """Compatibility wrapper for this app's existing SQLite-style SQL."""
    def __init__(self, database_url):
        self.connection = psycopg.connect(
            database_url,
            row_factory=postgres_row_factory,
            connect_timeout=10
        )

    def execute(self, query, parameters=()):
        if "PRAGMA table_info(users)" in query:
            query = """
                SELECT ordinal_position, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'users'
                ORDER BY ordinal_position
            """
            parameters = ()
        elif "PRAGMA table_info(bookings)" in query:
            query = """
                SELECT ordinal_position, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'bookings'
                ORDER BY ordinal_position
            """
            parameters = ()
        query = query.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        query = query.replace("room_id INTEGER NOT NULL", "room_id BIGINT NOT NULL")
        query = query.replace("invited_by INTEGER NOT NULL", "invited_by BIGINT NOT NULL")
        query = query.replace("?", "%s")
        return self.connection.execute(query, parameters)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


def get_db():
    if DATABASE_URL:
        return PostgresConnection(DATABASE_URL)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection

def send_email_via_resend(receiver_email, subject, body):
    api_key = os.environ.get("RESEND_API_KEY")

    if not api_key:
        raise Exception("RESEND_API_KEY is not configured.")

    email_data = {
        "from": "CampusSpace <onboarding@resend.dev>",
        "to": [receiver_email],
        "subject": subject,
        "text": body
    }

    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(email_data).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "CampusSpace/1.0"
        },
        method="POST"
    )

    try:
        with urlopen(request, timeout=20) as response:
            response.read()

    except HTTPError as error:
        error_body = error.read().decode("utf-8")
        print("RESEND ERROR:", error_body)
        raise

def send_otp_email(receiver_email, otp):

    subject = "CampusSpace Email Verification OTP"

    body = f"""
Hello,

Your CampusSpace verification OTP is:

{otp}

This OTP is valid for 10 minutes.

If you did not request this registration, you can ignore this email.

Regards,
CampusSpace
"""

    send_email_via_resend(receiver_email, subject, body)

def send_admin_invitation_email(receiver_email, token):

    sender_email = os.environ.get("CAMPUSSPACE_EMAIL")

    invitation_link = (
        "https://bookmyslot-v3lb.onrender.com/accept-admin-invitation/"
        + token
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

    send_email_via_resend(receiver_email, subject, body)

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

    connection.commit()
    connection.close()


# -----------------------------
# HOME
# -----------------------------


@app.route("/")
def home():
    connection = get_db()

    rooms = connection.execute("""
        SELECT id, name, room_type, capacity, floor
        FROM rooms
        ORDER BY id
    """).fetchall()

    connection.close()

    return render_template("index.html", rooms=rooms)

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
            room_conflicts={}
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

    available_rooms = []
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

        if room_available and not student_conflict:
            available_rooms.append(room)

        elif (
            is_approved_faculty
            and room_available
            and student_conflict
        ):
            # Faculty can see the room, with a conflict label.
            available_rooms.append(room)
            room_conflicts[room_id] = (
                "Conflicts with a student booking — faculty priority"
            )

    connection.close()

    return render_template(
        "find_room.html",
        rooms=available_rooms,
        selected_date=selected_date,
        selected_time=selected_time,
        selected_duration=selected_duration,
        room_conflicts=room_conflicts
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

        connection = get_db()

        if requested_role == "faculty":

            connection.execute("""
                INSERT INTO users
                (name, email, password, role, faculty_status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                email,
                password,
                "student",
                "pending"
            ))

        else:

            connection.execute("""
                INSERT INTO users
                (name, email, password, role, faculty_status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                email,
                password,
                "student",
                "none"
            ))

        connection.commit()
        connection.close()

        # Clear temporary registration information
        session.pop("registration_name", None)
        session.pop("registration_email", None)
        session.pop("registration_password", None)
        session.pop("registration_role", None)
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
            SELECT name, email, role, faculty_status
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
        # Verify faculty approval before granting priority.
        if (
            logged_in_user["role"] == "faculty"
            and logged_in_user["faculty_status"] != "approved"
        ):
            flash("Your faculty account is not approved yet.", "error")
            return redirect(url_for("find_room"))

        booking_date = request.form["date"]
        booking_time =request.form["time"]
        duration_text = request.form["duration"]
        people = request.form["people"]
        purpose = request.form["purpose"]

        duration = int(duration_text.split()[0])

        connection = get_db()

        # continue...

        # Get existing bookings for this room and date
        # Get existing confirmed bookings for this room and date
        existing_bookings = connection.execute("""
            SELECT id, booking_time, duration, booker_role
            FROM bookings
            WHERE room_id = ?
            AND booking_date = ?
            AND LOWER(status) = 'confirmed'
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

            # Check whether the two bookings overlap.
            if requested_start < existing_end and requested_end > existing_start:

                existing_role = booking["booker_role"]

                # Faculty priority applies only to approved faculty.
                if (
                    user_role == "faculty"
                    and logged_in_user["role"] == "faculty"
                    and logged_in_user["faculty_status"] == "approved"
                    and existing_role == "student"
                ):
                    # Defer cancellation until the new booking is saved.
                    student_bookings_to_cancel.append(booking["id"])
                    continue

                # Students cannot override faculty bookings.
                elif user_role == "student" and existing_role == "faculty":
                    connection.close()
                    flash(
                        "This room is already reserved by faculty for the selected time.",
                        "error"
                    )
                    return redirect(url_for("find_room"))

                # Other overlapping bookings remain blocked.
                else:
                    connection.close()
                    flash(
                        "This room is already booked for the selected time.",
                        "error"
                    )
                    return redirect(url_for("find_room"))


            # Save booking
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
                booker_role
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            room_id,
            name,
            email,
            booking_date,
            booking_time,
            duration,
            people,
            purpose,
            user_role
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


        for student_booking in cancelled_student_bookings:
            try:
                send_email_via_resend(
                    student_booking["email"],
                    "CampusSpace Booking Cancelled — Faculty Priority",
                    f"""Hello {student_booking['name']},

Your CampusSpace room booking has been cancelled because an approved faculty booking takes priority for an overlapping time slot.

Date: {student_booking['booking_date']}
Time: {student_booking['booking_time']}

Please visit CampusSpace to find another available room.

Regards,
CampusSpace
"""
                )
            except Exception as e:
                print("FACULTY PRIORITY EMAIL ERROR:", e)





        return f"""
        <div style="
            font-family: Arial;
            text-align: center;
            margin-top: 100px;
        ">

            <h1 style="color: #16a34a;">
                Booking Confirmed!
            </h1>

            <p>
                Thank you, <strong>{name}</strong>.
            </p>

            <p>
                <strong>Room:</strong> {room["name"]}
            </p>

            <p>
                <strong>Date:</strong> {booking_date}
            </p>

            <p>
                <strong>Time:</strong> {booking_time}
            </p>

            <p>
                <strong>Duration:</strong> {duration} hour(s)
            </p>

            <br>

            <a href="/"
               style="
                    display: inline-block;
                    background: #2563eb;
                    color: white;
                    padding: 12px 22px;
                    border-radius: 8px;
                    text-decoration: none;
               ">
                Back to CampusSpace
            </a>

        </div>
        """


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
            connection.close()
            flash(
                "An account with this email already exists. "
                "Use a different email for the admin invitation.",
                "error"
            )
            return redirect(url_for("home"))

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

    if request.method == "POST":

        name = request.form["name"].strip()
        room_type = request.form["room_type"].strip()
        capacity = request.form["capacity"].strip()

        if not capacity.isdigit() or int(capacity) <= 0:
            flash("Capacity must be a positive number.", "error")
            return redirect(url_for("add_room"))

        capacity = int(capacity)
        floor = request.form["floor"].strip()

        connection = get_db()

        connection.execute("""
            INSERT INTO rooms
            (name, room_type, capacity, floor)
            VALUES (?, ?, ?, ?)
        """, (
            name,
            room_type,
            capacity,
            floor
        ))

        connection.commit()
        connection.close()

        return redirect(url_for("admin"))

    return render_template("add_room.html")

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

        connection.execute("""
            UPDATE rooms
            SET name = ?,
                room_type = ?,
                capacity = ?,
                floor = ?
            WHERE id = ?
        """, (
            name,
            room_type,
            capacity,
            floor,
            room_id
        ))

        connection.commit()
        connection.close()

        return redirect(url_for("admin"))

    connection.close()

    return render_template(
        "edit_room.html",
        room=room
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
