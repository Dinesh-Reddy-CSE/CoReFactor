import os
import uuid
import sqlite3
import logging
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, join_room, emit, leave_room as socket_leave_room
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import requests
import json
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme123")
bcrypt = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = "login"

DATABASE_PATH = os.path.join(os.environ.get('RENDER_DISK_PATH', '.'), 'database.db')

class User(UserMixin):
    pass

@login_manager.user_loader
def load_user(user_id):
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, email, name, avatar FROM users WHERE id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            return None
        user = User()
        user.id = user_data["id"]
        user.email = user_data["email"]
        user.name = user_data["name"]
        user.avatar = user_data["avatar"]
        return user

def init_db():
    with sqlite3.connect(DATABASE_PATH) as conn:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT,
                     avatar TEXT, password TEXT, theme TEXT DEFAULT 'dark')''')

        c.execute('''CREATE TABLE IF NOT EXISTS rooms
                     (id TEXT PRIMARY KEY, name TEXT, created_at TIMESTAMP,
                     creator_id TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_rooms
                     (user_id TEXT, room_id TEXT, joined_at TIMESTAMP,
                     PRIMARY KEY (user_id, room_id))''')

        c.execute('''CREATE TABLE IF NOT EXISTS activities
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT,
                     user_id TEXT, action TEXT, timestamp TIMESTAMP)''')

        c.execute('''CREATE TABLE IF NOT EXISTS chat_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT,
                     sender_id TEXT, message TEXT, timestamp TIMESTAMP)''')

        conn.commit()

@app.cli.command("init-db")
def init_db_command():
    """Initializes the database."""
    init_db()
    logger.info("Database initialized from command line.")

def room_owner_required(f):
    @wraps(f)
    def decorated_function(room_id, *args, **kwargs):
        with sqlite3.connect(DATABASE_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM rooms WHERE id = ? AND creator_id = ?",
                     (room_id, current_user.id))
            if not c.fetchone():
                flash("Only room owner can perform this action", "error")
                return redirect(url_for('room', room_id=room_id))
        return f(room_id, *args, **kwargs)
    return decorated_function

@app.route("/")
@login_required
def home():
    return render_template("index.html", user=current_user)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        with sqlite3.connect(DATABASE_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, email, name, avatar, password FROM users WHERE email = ?", (email,))
            user_data = c.fetchone()
            if user_data and bcrypt.check_password_hash(user_data[4], password):
                user = User()
                user.id = user_data[0]
                user.email = user_data[1]
                user.name = user_data[2]
                user.avatar = user_data[3]
                login_user(user)
                return redirect(url_for("home"))
        flash("Invalid email or password", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip()
        username = request.form["username"].strip()
        password = request.form["password"]
        hashed = bcrypt.generate_password_hash(password).decode("utf-8")
        avatar = f"https://ui-avatars.com/api/?name={username}&background=random"
        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                c = conn.cursor()
                user_id = str(uuid.uuid4())
                c.execute("INSERT INTO users (id, email, name, password, avatar) VALUES (?, ?, ?, ?, ?)",
                          (user_id, email, username, hashed, avatar))
                conn.commit()
            flash("Registration successful! Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email already registered", "error")
    return render_template("register.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/profile")
@login_required
def profile():
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT r.id, r.name, ur.joined_at FROM rooms r JOIN user_rooms ur ON r.id = ur.room_id WHERE ur.user_id = ?", (current_user.id,))
        user_rooms = c.fetchall()
        c.execute("SELECT * FROM activities WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20", (current_user.id,))
        activities = c.fetchall()
    return render_template("profile.html", user=current_user, rooms=user_rooms, activities=activities)

@app.route("/room/create", methods=["POST"])
@login_required
def create_room():
    room_id = request.form.get("room_id") or str(uuid.uuid4())[:8]
    return redirect(url_for("room", room_id=room_id))

@app.route("/room/join", methods=["POST"])
@login_required
def join_room_route():
    room_id = request.form["room_id"].strip()
    return redirect(url_for("room", room_id=room_id))

@app.route("/room/<room_id>")
@login_required
def room(room_id):
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT creator_id FROM rooms WHERE id = ?", (room_id,))
        room_data = c.fetchone()

        if not room_data:
            c.execute("INSERT INTO rooms (id, name, created_at, creator_id) VALUES (?, ?, ?, ?)",
                     (room_id, f"Room {room_id}", datetime.now(), current_user.id))

        c.execute("INSERT OR IGNORE INTO user_rooms (user_id, room_id, joined_at) VALUES (?, ?, ?)",
                 (current_user.id, room_id, datetime.now()))
        conn.commit()

    return render_template("editor.html", room_id=room_id, user=current_user)

@app.route("/room/<room_id>/leave")
@login_required
def leave_room(room_id):
    with sqlite3.connect(DATABASE_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM user_rooms WHERE user_id = ? AND room_id = ?",
                 (current_user.id, room_id))

        c.execute("SELECT COUNT(*) FROM user_rooms WHERE room_id = ?", (room_id,))
        if c.fetchone()[0] == 0:
            c.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

        conn.commit()
    return redirect(url_for('home'))

@socketio.on("connect")
@login_required
def handle_connect():
    pass

@socketio.on("disconnect")
def handle_disconnect():
    if current_user.is_authenticated:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT room_id FROM user_rooms WHERE user_id = ?", (current_user.id,))
            rooms = c.fetchall()
            for room in rooms:
                room_id = room['room_id']
                participants = get_participants_list(room_id)
                socketio.emit("participants_update", {
                    "participants": participants,
                    "sender": current_user.id
                }, room=room_id)

@socketio.on("leave_room_event")
def handle_leave_room_event(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return

    room_id = data.get("room_id")
    if not room_id:
        return

    socket_leave_room(room_id)

    with sqlite3.connect(DATABASE_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM user_rooms WHERE user_id = ? AND room_id = ?",
                 (current_user.id, room_id))

        c.execute("SELECT COUNT(*) FROM user_rooms WHERE room_id = ?", (room_id,))
        if c.fetchone()[0] == 0:
            c.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

        conn.commit()

    participants = get_participants_list(room_id)
    socketio.emit("participants_update", {
        "participants": participants,
        "sender": current_user.id
    }, room=room_id)

@socketio.on("join")
def handle_join(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return
    room_id = data["room_id"]
    join_room(room_id)

    participants = get_participants_list(room_id)
    socketio.emit("participants_update", {
        "participants": participants,
        "sender": current_user.id
    }, room=room_id)

    with sqlite3.connect(DATABASE_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO activities (room_id, user_id, action, timestamp) VALUES (?, ?, ?, ?)",
            (room_id, current_user.id, "joined", datetime.now()),
        )
        conn.commit()

def get_participants_list(room_id):
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.name, u.avatar, r.creator_id = ? as is_owner
            FROM user_rooms ur
            JOIN users u ON ur.user_id = u.id
            JOIN rooms r ON ur.room_id = r.id
            WHERE ur.room_id = ?
        """, (current_user.id, room_id))
        return [dict(row) for row in c.fetchall()]

@socketio.on("text_change")
def handle_text_change(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return
    room_id = data["room_id"]
    socketio.emit(
        "remote_change",
        {"content": data["content"], "sender": current_user.id},
        room=room_id,
    )
    with sqlite3.connect(DATABASE_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO activities (room_id, user_id, action, timestamp) VALUES (?, ?, ?, ?)",
            (room_id, current_user.id, "edited code", datetime.now()),
        )
        conn.commit()

@socketio.on("run_code")
def handle_run_code(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return
    room_id = data["room_id"]
    code = data["code"]
    input_data = data["input"]

    if any(keyword in code for keyword in ['os.', 'sys.', 'subprocess.']):
        socketio.emit("code_output", {"output": "Error: Restricted keywords detected.", "sender": current_user.id}, room=room_id)
        return

    try:
        from io import StringIO
        import sys, contextlib

        output_buffer = StringIO()
        input_buffer = StringIO(input_data)
        original_stdin, original_stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = input_buffer, output_buffer

        with contextlib.redirect_stdout(output_buffer):
            exec(code)

        sys.stdin, sys.stdout = original_stdin, original_stdout
        output = output_buffer.getvalue()

        if not output:
            output = "Code executed successfully with no output."

        socketio.emit("code_output", {"output": output, "sender": current_user.id}, room=room_id)
    except Exception as e:
        socketio.emit("code_output", {"output": f"Error: {str(e)}", "sender": current_user.id}, room=room_id)

@socketio.on("general_chat_message")
def handle_general_chat_message(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return

    room_id = data.get("room_id")
    message = data.get("message")

    if not room_id or not message:
        return

    emit("new_general_chat_message", {
        "sender_name": current_user.name,
        "message": message
    }, room=room_id)


@socketio.on("chatbot_request")
def handle_chatbot_request(data):
    if not current_user.is_authenticated:
        emit("unauthorized", {"msg": "Please login"})
        return

    room_id = data["room_id"]
    query = data["query"]
    current_code = data["code"]

    if os.environ.get("OLLAMA_ENABLED") != "true":
        socketio.emit("chatbot_response", {
            "user_name": "AI",
            "query": query,
            "ai_response": "The chatbot is not enabled. Please set OLLAMA_ENABLED=true in your environment."
        }, room=room_id)
        return

    try:
        url = "http://localhost:11434/api/generate"
        headers = {"Content-Type": "application/json"}
        prompt = f"""
        You are a helpful AI assistant for a collaborative code editor.
        Your task is to analyze the provided Python code and answer a user's question about it.
        Be concise and helpful. Respond with code examples when appropriate.
        Current Python Code:
        python
        {current_code}

        User's question: {query}
        """

        payload = {
            "model": "llama3",
            "prompt": prompt,
            "stream": False
        }

        response = requests.post(url, headers=headers, data=json.dumps(payload))
        response.raise_for_status()

        ai_response = response.json()["response"]

        with sqlite3.connect(DATABASE_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO chat_history (room_id, sender_id, message, timestamp) VALUES (?, ?, ?, ?)",
                      (room_id, "AI", ai_response, datetime.now()))
            c.execute("INSERT INTO chat_history (room_id, sender_id, message, timestamp) VALUES (?, ?, ?, ?)",
                      (room_id, current_user.name, query, datetime.now()))
            conn.commit()

        socketio.emit("chatbot_response", {
            "user_name": current_user.name,
            "query": query,
            "ai_response": ai_response
        }, room=room_id)

    except requests.exceptions.RequestException as e:
        socketio.emit("chatbot_response", {
            "user_name": "AI",
            "query": query,
            "ai_response": f"Error: Could not connect to the Ollama server. Please ensure Ollama is running and the 'llama3' model is pulled. Details: {str(e)}"
        }, room=room_id)
    except Exception as e:
        socketio.emit("chatbot_response", {
            "user_name": "AI",
            "query": query,
            "ai_response": f"Error: An error occurred while processing your request: {str(e)}"
        }, room=room_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "False").lower() == "true")
