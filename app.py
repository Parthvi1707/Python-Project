import sqlite3
import json
import random
import uuid
import os
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory, g

# ---------- Configuration ----------
DB_PATH = "games.db"
WORD_POOLS = [
    ["cat", "dog", "bat"],
    ["frog", "tree", "lamp"],
    ["planet", "rocket", "python"],
    ["computer", "language", "keyboard"],
    ["adventure", "chocolate", "explosion"]
]
TIMERS = [5, 4, 3, 2, 1]

app = Flask(__name__, static_folder="static", template_folder="templates")


# ---------- Database Helpers ----------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        need_init = not os.path.exists(DB_PATH)
        db = g._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
        if need_init:
            init_db(db)
    return db

def init_db(db):
    cur = db.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        players TEXT,
        lives TEXT,
        current_round INTEGER,
        turn_order TEXT,
        played_this_round TEXT,
        last_assigned TEXT,
        used_words TEXT,
        history TEXT,
        created_at TEXT
    )
    """)
    db.commit()

def save_game_state(game_id, state):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
      INSERT OR REPLACE INTO games (id, players, lives, current_round, turn_order, played_this_round, last_assigned, used_words, history, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game_id,
        json.dumps(state["players"]),
        json.dumps(state["lives"]),
        state["current_round"],
        json.dumps(state.get("turn_order", state["players"])),
        json.dumps(list(state.get("played_this_round", []))),
        json.dumps(state.get("last_assigned_word", {})),
        json.dumps(list(state.get("used_words_in_round", []))),
        json.dumps(state.get("history", [])),
        state.get("created_at", datetime.utcnow().isoformat())
    ))
    db.commit()

def load_game_state(game_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    row = cur.fetchone()
    if not row:
        return None
    state = {
        "players": json.loads(row["players"]),
        "lives": json.loads(row["lives"]),
        "current_round": int(row["current_round"]),
        "turn_order": json.loads(row["turn_order"]),
        "played_this_round": set(json.loads(row["played_this_round"])),
        "last_assigned_word": json.loads(row["last_assigned"]),
        "used_words_in_round": set(json.loads(row["used_words"])),
        "history": json.loads(row["history"]),
        "created_at": row["created_at"]
    }
    return state

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/create_game", methods=["POST"])
def create_game():
    data = request.json or {}
    players = data.get("players", [])
    players = [p.strip() for p in players if p and p.strip()]
    if not players:
        return jsonify({"error": "No players given."}), 400

    game_id = str(uuid.uuid4())[:8]
    state = {
        "players": players,
        "lives": {p: 3 for p in players},
        "current_round": 0,
        "turn_order": players.copy(),
        "played_this_round": set(),
        "last_assigned_word": {},
        "used_words_in_round": set(),
        "history": [],
        "created_at": datetime.utcnow().isoformat()
    }
    save_game_state(game_id, state)
    return jsonify({"game_id": game_id, "timers": TIMERS, "players": players})

@app.route("/game/<game_id>/word", methods=["GET"])
def get_word(game_id):
    state = load_game_state(game_id)
    if not state:
        return jsonify({"error": "Game not found."}), 404

    round_idx = state["current_round"]
    if round_idx >= len(WORD_POOLS):
        return jsonify({"error": "No more rounds."}), 400

    # player can be passed as param, else auto-assign
    player = request.args.get("player")
    alive = [p for p in state["players"] if state["lives"].get(p, 0) > 0]

    if player:
        player = player.strip()
        if player not in state["players"] or state["lives"].get(player, 0) <= 0:
            return jsonify({"error": "Player not valid or eliminated."}), 400
    else:
        player = None
        for p in state["turn_order"]:
            if state["lives"].get(p, 0) > 0 and p not in state["last_assigned_word"]:
                player = p
                break
        # If all have been assigned, pick first alive
        if player is None:
            for p in state["turn_order"]:
                if state["lives"].get(p, 0) > 0:
                    player = p
                    break

    if not player:
        return jsonify({"error": "No alive players."}), 400

    pool = WORD_POOLS[round_idx]
    unused = [w for w in pool if w not in state["used_words_in_round"]]
    if not unused:
        state["used_words_in_round"].clear()
        unused = pool.copy()
    word = random.choice(unused)

    # store assignment
    state["last_assigned_word"][player] = word
    state["used_words_in_round"].add(word)
    state["history"].append({"round": round_idx, "player": player, "word": word, "ts": datetime.utcnow().isoformat()})

    save_game_state(game_id, state)

    return jsonify({
        "word": word,
        "round": round_idx,
        "timer": TIMERS[round_idx],
        "player": player,
        "lives": state["lives"],
        "players": state["players"],
        "alive": alive
    })

@app.route("/game/<game_id>/submit", methods=["POST"])
def submit_word(game_id):
    state = load_game_state(game_id)
    if not state:
        return jsonify({"error": "Game not found."}), 404

    data = request.json or {}
    player = data.get("player")
    typed = data.get("typed", "")
    elapsed = float(data.get("elapsed", 9999))
    round_idx = int(data.get("round", state["current_round"]))

    if player not in state["players"]:
        return jsonify({"error": "Player not in game."}), 400
    if state["lives"].get(player, 0) <= 0:
        return jsonify({"error": "Player eliminated."}), 400

    expected = state["last_assigned_word"].get(player)
    if expected is None:
        return jsonify({"error": "No assigned word for player (request /word first)."}), 400

    # Accept case-insensitive correct input (user-friendly improvement)
    timer = TIMERS[round_idx] if 0 <= round_idx < len(TIMERS) else TIMERS[-1]
    success = (typed.strip().lower() == expected.lower() and elapsed <= timer)

    if not success:
        state["lives"][player] = max(0, state["lives"].get(player, 0) - 1)

    state["played_this_round"].add(player)
    state["last_assigned_word"].pop(player, None)

    alive = [p for p in state["players"] if state["lives"].get(p, 0) > 0]
    if all(p in state["played_this_round"] for p in alive):
        state["current_round"] += 1
        state["played_this_round"].clear()
        state["used_words_in_round"].clear()
        state["history"].append({"round_advance": state["current_round"], "ts": datetime.utcnow().isoformat()})

    save_game_state(game_id, state)

    return jsonify({
        "success": success,
        "expected": expected,
        "typed": typed,
        "timer": timer,
        "elapsed": elapsed,
        "lives": state["lives"],
        "current_round": state["current_round"],
        "player": player
    })

@app.route("/game/<game_id>/status", methods=["GET"])
def status(game_id):
    state = load_game_state(game_id)
    if not state:
        return jsonify({"error": "Game not found."}), 404
    return jsonify({
        "players": state["players"],
        "lives": state["lives"],
        "current_round": state["current_round"]
    })

@app.route("/game/<game_id>/history", methods=["GET"])
def history(game_id):
    state = load_game_state(game_id)
    if not state:
        return jsonify({"error": "Game not found."}), 404
    return jsonify({"history": state["history"]})

@app.route("/sounds/<path:fname>")
def sound_file(fname):
    return send_from_directory("static/sounds", fname)

if __name__ == "__main__":
    # Ensure DB exists/init inside application context (fixes "working outside application context")
    with app.app_context():
        get_db()
    print("Serving on http://127.0.0.1:5000/ — template folder:", app.template_folder)
    app.run(debug=True, port=5000)
    