"""
Bucket List Tracker
------------------
Run with:
    pip install streamlit plotly pandas requests pillow folium streamlit-folium libsql openpyxl
    streamlit run app.py

DATABASE:
This app stores data on Turso (a free, hosted SQLite-compatible database)
instead of a local file, because Streamlit Community Cloud wipes local
files whenever the app restarts/redeploys/sleeps. See the setup notes
in get_conn() below.
"""

import streamlit as st
import libsql
import hashlib
import hmac
import os
import random
import datetime
import time
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import folium
from streamlit_folium import st_folium
from io import BytesIO
from PIL import Image

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
st.set_page_config(page_title="My Bucket List Tracker", page_icon="🌍", layout="wide")

COLOR_VISITED = "#2ecc71"
COLOR_REMAINING = "#e74c3c"
DEFAULT_SECTIONS = ["Temples", "Hill Stations", "Beaches"]
PRIORITY_OPTIONS = ["🔥 High", "⭐ Medium", "🌙 Someday"]

# ----------------------------------------------------------------------
# DATABASE SETUP + MIGRATIONS
#
# SETUP (one-time):
#   1. Sign up free at https://turso.tech and install the CLI, or use the
#      web dashboard directly.
#   2. Create a database:  turso db create bucket-list
#   3. Get the URL:        turso db show bucket-list --url
#   4. Get a token:        turso db tokens create bucket-list
#   5. In Streamlit Cloud: go to your app -> Settings -> Secrets, and add:
#         TURSO_DATABASE_URL = "libsql://bucket-list-yourname.turso.io"
#         TURSO_AUTH_TOKEN = "your-token-here"
#      For local development, put the same two lines in a
#      .streamlit/secrets.toml file (add it to .gitignore - don't commit it).
# ----------------------------------------------------------------------
def get_conn():
    conn = libsql.connect(
        database=st.secrets["TURSO_DATABASE_URL"],
        auth_token=st.secrets["TURSO_AUTH_TOKEN"],
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass  # some remote libSQL setups manage this server-side instead
    return conn


def _lastrowid(cur, conn):
    """Get the id of the last inserted row, with a safe fallback for
    remote libSQL connections where .lastrowid isn't always populated."""
    rid = getattr(cur, "lastrowid", None)
    if rid:
        return rid
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return row[0] if row else None


def _table_columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _add_column_if_missing(cur, table, column, coltype):
    if column not in _table_columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


FINAL_PLACES_SCHEMA = """
    CREATE TABLE places (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        section_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        visited INTEGER NOT NULL DEFAULT 0,
        lat REAL,
        lon REAL,
        notes TEXT,
        priority TEXT,
        visited_date TEXT,
        description TEXT,
        FOREIGN KEY (section_id) REFERENCES sections (id) ON DELETE CASCADE
    )
"""

FINAL_PHOTOS_SCHEMA = """
    CREATE TABLE photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        place_id INTEGER NOT NULL,
        image_data BLOB NOT NULL,
        uploaded_at TEXT NOT NULL,
        FOREIGN KEY (place_id) REFERENCES places (id) ON DELETE CASCADE
    )
"""


def _rebuild_places_table(conn, cur):
    legacy_cols = _table_columns(cur, "places")
    optional_cols = ["lat", "lon", "notes", "priority", "visited_date", "description"]

    cur.execute("ALTER TABLE places RENAME TO places_legacy")
    cur.execute(FINAL_PLACES_SCHEMA)

    if "section_id" in legacy_cols:
        select_optional = ", ".join(
            c if c in legacy_cols else f"NULL AS {c}" for c in optional_cols
        )
        cur.execute(
            f"""
            INSERT INTO places (section_id, name, visited, lat, lon, notes, priority, visited_date, description)
            SELECT section_id, name, visited, {select_optional}
            FROM places_legacy
            WHERE section_id IS NOT NULL AND visited != -1
            """
        )
    elif "section" in legacy_cols and "user_id" in legacy_cols:
        select_optional = ", ".join(
            c if c in legacy_cols else f"NULL AS {c}" for c in optional_cols
        )
        cur.execute(
            f"""SELECT user_id, section, name, visited, {select_optional}
                FROM places_legacy WHERE visited != -1"""
        )
        rows = cur.fetchall()
        section_id_cache = {}
        for row in rows:
            user_id, section_name, name, visited = row[0], row[1], row[2], row[3]
            extras = row[4:]
            cache_key = (user_id, section_name)
            if cache_key not in section_id_cache:
                cur.execute(
                    "INSERT OR IGNORE INTO sections (user_id, parent_id, name) VALUES (?, NULL, ?)",
                    (user_id, section_name),
                )
                cur.execute(
                    "SELECT id FROM sections WHERE user_id = ? AND name = ? AND parent_id IS NULL",
                    (user_id, section_name),
                )
                section_id_cache[cache_key] = cur.fetchone()[0]
            section_id = section_id_cache[cache_key]
            cur.execute(
                """INSERT INTO places (section_id, name, visited, lat, lon, notes, priority, visited_date, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (section_id, name, visited, *extras),
            )

    cur.execute("DROP TABLE places_legacy")
    conn.commit()


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            parent_id INTEGER,
            name TEXT NOT NULL,
            UNIQUE(user_id, parent_id, name),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()

    # migration: older DBs won't have parent_id on sections yet
    _add_column_if_missing(cur, "sections", "parent_id", "INTEGER")
    conn.commit()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='places'")
    places_exists = cur.fetchone() is not None

    if not places_exists:
        cur.execute(FINAL_PLACES_SCHEMA)
        cur.execute(FINAL_PHOTOS_SCHEMA)
        conn.commit()
    else:
        legacy_cols = _table_columns(cur, "places")
        needs_rebuild = ("user_id" in legacy_cols) or ("section_id" not in legacy_cols)
        if needs_rebuild:
            _rebuild_places_table(conn, cur)
        else:
            for col, coltype in [
                ("lat", "REAL"), ("lon", "REAL"), ("notes", "TEXT"),
                ("priority", "TEXT"), ("visited_date", "TEXT"), ("description", "TEXT")
            ]:
                _add_column_if_missing(cur, "places", col, coltype)
            conn.commit()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='photos'")
    if not cur.fetchone():
        cur.execute(FINAL_PHOTOS_SCHEMA)
        conn.commit()

    conn.close()


init_db()


# ----------------------------------------------------------------------
# PASSWORD HASHING (PBKDF2)
# ----------------------------------------------------------------------
def hash_password(password: str, salt: bytes = None):
    if salt is None:
        salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex(), pwd_hash.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, computed_hash = hash_password(password, salt)
    return hmac.compare_digest(computed_hash, hash_hex)


def register_user(username: str, password: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        conn.close()
        return False, "Username already taken."

    salt_hex, hash_hex = hash_password(password)
    cur.execute(
        "INSERT INTO users (username, salt, password_hash) VALUES (?, ?, ?)",
        (username, salt_hex, hash_hex),
    )
    user_id = _lastrowid(cur, conn)
    for section in DEFAULT_SECTIONS:
        cur.execute("INSERT INTO sections (user_id, parent_id, name) VALUES (?, NULL, ?)", (user_id, section))
    conn.commit()
    conn.close()
    return True, "Account created! You can now log in."


def login_user(username: str, password: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, salt, password_hash FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False, None, "No account with that username."
    user_id, salt_hex, hash_hex = row
    if verify_password(password, salt_hex, hash_hex):
        return True, user_id, "Logged in!"
    return False, None, "Incorrect password."


# ----------------------------------------------------------------------
# GEOCODING
#
# Two fixes vs. earlier versions:
#   1. Results are now cached ONLY on success. Previously a failed lookup
#      (e.g. a transient network hiccup) got cached for 24h, so that place
#      would silently never get located again until the cache expired.
#      That's the most likely reason places "didn't take location
#      automatically."
#   2. Falls back to a second free provider (Photon) if OpenStreetMap's
#      Nominatim has no result, instead of giving up after one source.
# ----------------------------------------------------------------------
_geocode_cache = {}


def _try_nominatim(query):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "BucketListTrackerApp/1.0 (contact: example@example.com)"},
            timeout=8,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


def _try_photon(query):
    try:
        resp = requests.get(
            "https://photon.komoot.io/api/",
            params={"q": query, "limit": 1},
            timeout=8,
        )
        data = resp.json()
        feats = data.get("features", [])
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"][:2]
            return float(lat), float(lon)
    except Exception:
        pass
    return None, None


def geocode(place_name: str, hint: str = ""):
    key = (place_name.strip() + "|" + hint).lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    queries = [place_name.strip()]
    if hint:
        queries.append(f"{place_name.strip()}, {hint}")

    result = (None, None)
    for i, q in enumerate(queries):
        result = _try_nominatim(q)
        if result[0] is not None:
            break
        time.sleep(1.1)  # respect Nominatim's ~1 req/sec usage policy

    if result[0] is None:
        result = _try_photon(place_name.strip())

    if result[0] is not None:
        _geocode_cache[key] = result
    return result


def _normalize_priority(raw):
    if not raw:
        return None
    v = str(raw).strip().lower()
    for opt in PRIORITY_OPTIONS:
        if v in opt.lower():
            return opt
    if "high" in v:
        return PRIORITY_OPTIONS[0]
    if "med" in v:
        return PRIORITY_OPTIONS[1]
    if "low" in v or "some" in v:
        return PRIORITY_OPTIONS[2]
    return None


def small_map(lat, lon, label):
    fig = px.scatter_mapbox(
        pd.DataFrame([{"lat": lat, "lon": lon, "label": label}]),
        lat="lat", lon="lon", hover_name="label", zoom=9, height=280,
    )
    fig.update_traces(marker=dict(size=16, color=COLOR_REMAINING))
    fig.update_layout(mapbox_style="open-street-map", margin=dict(t=0, b=0, l=0, r=0))
    return fig


# ----------------------------------------------------------------------
# SECTION HELPERS (with sub-section support)
# ----------------------------------------------------------------------
def get_all_sections(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, parent_id, name FROM sections WHERE user_id = ? ORDER BY id", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "parent_id": r[1], "name": r[2]} for r in rows]


def build_section_tree(user_id):
    """Returns dict: parent_id (or None for top-level) -> list of section dicts."""
    all_secs = get_all_sections(user_id)
    by_parent = {}
    for s in all_secs:
        by_parent.setdefault(s["parent_id"], []).append(s)
    return by_parent


def flatten_tree_for_display(tree, parent_id=None, depth=0):
    """Depth-first list of (indented_label, section_id) for use in dropdowns."""
    result = []
    for s in tree.get(parent_id, []):
        prefix = ("　" * depth) + ("↳ " if depth > 0 else "")
        result.append((prefix + s["name"], s["id"]))
        result.extend(flatten_tree_for_display(tree, s["id"], depth + 1))
    return result


def get_descendant_section_ids(user_id, section_id):
    tree = build_section_tree(user_id)
    result = [section_id]

    def recurse(pid):
        for child in tree.get(pid, []):
            result.append(child["id"])
            recurse(child["id"])

    recurse(section_id)
    return result


def add_section(user_id, name, parent_id=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sections (user_id, parent_id, name) VALUES (?, ?, ?)",
        (user_id, parent_id, name),
    )
    conn.commit()
    new_id = _lastrowid(cur, conn)
    conn.close()
    return new_id


def delete_section(section_id):
    """Recursively deletes a section, its subsections, and all their places."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM sections WHERE parent_id = ?", (section_id,))
    child_ids = [r[0] for r in cur.fetchall()]
    conn.close()

    for cid in child_ids:
        delete_section(cid)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM places WHERE section_id = ?", (section_id,))
    cur.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# PLACES HELPERS
# ----------------------------------------------------------------------
def get_places(section_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, name, visited, lat, lon, notes, priority, visited_date, description
           FROM places WHERE section_id = ? ORDER BY id""",
        (section_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "name": r[1], "visited": bool(r[2]), "lat": r[3], "lon": r[4],
            "notes": r[5] or "", "priority": r[6] or PRIORITY_OPTIONS[1],
            "visited_date": r[7], "description": r[8] or "",
        }
        for r in rows
    ]


def insert_place(section_id, name, visited=False, lat=None, lon=None, notes="", priority=None, description=""):
    priority = priority or PRIORITY_OPTIONS[1]
    visited_date = datetime.date.today().isoformat() if visited else None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO places (section_id, name, visited, lat, lon, notes, priority, visited_date, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (section_id, name, int(visited), lat, lon, notes, priority, visited_date, description),
    )
    conn.commit()
    new_id = _lastrowid(cur, conn)
    conn.close()
    return new_id


def update_place(place_id, name=None, visited=None, lat=None, lon=None, notes=None, priority=None, description=None):
    conn = get_conn()
    cur = conn.cursor()
    fields, values = [], []

    if name is not None:
        fields.append("name = ?"); values.append(name)
    if visited is not None:
        fields.append("visited = ?"); values.append(int(visited))
        fields.append("visited_date = ?")
        values.append(datetime.date.today().isoformat() if visited else None)
    if lat is not None:
        fields.append("lat = ?"); values.append(lat)
    if lon is not None:
        fields.append("lon = ?"); values.append(lon)
    if notes is not None:
        fields.append("notes = ?"); values.append(notes)
    if priority is not None:
        fields.append("priority = ?"); values.append(priority)
    if description is not None:
        fields.append("description = ?"); values.append(description)

    if fields:
        values.append(place_id)
        cur.execute(f"UPDATE places SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    conn.close()


def delete_place(place_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM places WHERE id = ?", (place_id,))
    conn.commit()
    conn.close()


def bulk_add_places(section_id, names):
    """Insert many places at once. Returns (added_list, duplicate_names)."""
    existing = {p["name"].strip().lower() for p in get_places(section_id)}
    added, duplicates = [], []
    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue
        if name.lower() in existing:
            duplicates.append(name)
            continue
        lat, lon = geocode(name)
        new_id = insert_place(section_id, name, visited=False, lat=lat, lon=lon)
        existing.add(name.lower())
        added.append({"id": new_id, "name": name, "lat": lat, "lon": lon})
    return added, duplicates


# ----------------------------------------------------------------------
# PHOTOS HELPERS
# ----------------------------------------------------------------------
def get_photos(place_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, image_data, uploaded_at FROM photos WHERE place_id = ? ORDER BY id",
        (place_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "image_data": r[1], "uploaded_at": r[2]} for r in rows]


def add_photo(place_id, image_bytes: bytes):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO photos (place_id, image_data, uploaded_at) VALUES (?, ?, ?)",
        (place_id, image_bytes, datetime.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def delete_photo(photo_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# SESSION STATE
# ----------------------------------------------------------------------
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "username" not in st.session_state:
    st.session_state.username = None
if "surprise_place" not in st.session_state:
    st.session_state.surprise_place = None
if "selected_section_id" not in st.session_state:
    st.session_state.selected_section_id = None
if "current_page" not in st.session_state:
    st.session_state.current_page = "Dashboard"


def logout():
    st.session_state.user_id = None
    st.session_state.username = None
    st.session_state.selected_section_id = None
    st.session_state.current_page = "Dashboard"


# ----------------------------------------------------------------------
# LOGIN / REGISTER PAGE
# ----------------------------------------------------------------------
def auth_page():
    st.title("🌍 My Bucket List Tracker")
    st.caption("Log in or create an account to start tracking the places you dream of visiting.")

    tab_login, tab_register = st.tabs(["🔑 Login", "📝 Register"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log In", use_container_width=True):
                if not username or not password:
                    st.warning("Please enter both a username and password.")
                else:
                    ok, user_id, msg = login_user(username.strip(), password)
                    if ok:
                        st.session_state.user_id = user_id
                        st.session_state.username = username.strip()
                        st.rerun()
                    else:
                        st.error(msg)

    with tab_register:
        with st.form("register_form"):
            new_username = st.text_input("Choose a username")
            new_password = st.text_input("Choose a password", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            if st.form_submit_button("Create Account", use_container_width=True):
                if not new_username or not new_password:
                    st.warning("Please fill in all fields.")
                elif new_password != confirm_password:
                    st.warning("Passwords do not match.")
                else:
                    ok, msg = register_user(new_username.strip(), new_password)
                    if ok:
                        st.success(msg + " Switch to the Login tab to sign in.")
                    else:
                        st.error(msg)


# ----------------------------------------------------------------------
# PLACE DETAIL (DESCRIPTION + PHOTOS)
# ----------------------------------------------------------------------
def render_place_detail(place):
    st.subheader(f"📍 {place['name']}")

    desc = st.text_area(
        "Description",
        value=place.get("description", ""),
        placeholder="Write about your experience, tips, best time to visit, etc.",
        key=f"desc_{place['id']}",
    )
    if st.button("💾 Save Description", key=f"save_desc_{place['id']}"):
        update_place(place["id"], description=desc)
        st.success("Description saved!")
        st.rerun()

    st.divider()
    st.write("📸 Photos")

    photos = get_photos(place["id"])
    if photos:
        cols = st.columns(3)
        for i, ph in enumerate(photos):
            with cols[i % 3]:
                st.image(BytesIO(ph["image_data"]), use_container_width=True)
                if st.button("🗑️ Delete", key=f"del_photo_{ph['id']}"):
                    delete_photo(ph["id"])
                    st.rerun()
    else:
        st.info("No photos yet. Upload your first memory below.")

    uploaded_files = st.file_uploader(
        "Upload photos", type=["png", "jpg", "jpeg"],
        accept_multiple_files=True, key=f"upload_{place['id']}",
    )
    if uploaded_files:
        for f in uploaded_files:
            try:
                img = Image.open(f)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                add_photo(place["id"], buf.getvalue())
            except Exception:
                st.error(f"Could not process {f.name}")
        st.success("Photos uploaded!")
        st.rerun()


# ----------------------------------------------------------------------
# SIDEBAR: NAVIGATION + HIERARCHICAL SECTION SELECTOR + DEBUG PANEL
# ----------------------------------------------------------------------
def render_sidebar(user_id):
    st.sidebar.title(f"👋 Hi, {st.session_state.username}")

    if st.sidebar.button("Log Out", use_container_width=True):
        logout()
        st.rerun()

    with st.sidebar.expander("🔧 Debug: Database Status"):
        try:
            dbg_conn = get_conn()
            dbg_cur = dbg_conn.cursor()
            dbg_cur.execute("SELECT COUNT(*) FROM users")
            user_count = dbg_cur.fetchone()[0]
            dbg_cur.execute("SELECT COUNT(*) FROM sections")
            section_count = dbg_cur.fetchone()[0]
            dbg_cur.execute("SELECT COUNT(*) FROM places")
            place_count = dbg_cur.fetchone()[0]
            dbg_conn.close()

            st.success("✅ Connected to Turso")
            st.write(f"👤 Users: {user_count}")
            st.write(f"📂 Sections: {section_count}")
            st.write(f"📍 Places: {place_count}")
            db_url = st.secrets.get("TURSO_DATABASE_URL", "NOT SET")
            st.caption(f"DB URL: {db_url[:35]}...")
        except Exception as e:
            st.error("❌ Not connected")
            st.code(str(e))
            st.caption("Common causes: wrong/missing TURSO_DATABASE_URL or "
                       "TURSO_AUTH_TOKEN in Secrets, or a typo in either value.")

    st.sidebar.divider()

    # Navigation
    st.sidebar.subheader("🧭 Navigate")
    pages = ["Dashboard", "My List", "Add Places", "Map", "Statistics"]
    selected_page = st.sidebar.radio("Page:", pages, index=pages.index(st.session_state.current_page))
    if selected_page != st.session_state.current_page:
        st.session_state.current_page = selected_page
        st.rerun()

    st.sidebar.divider()

    # Sections (hierarchical)
    st.sidebar.subheader("📂 Sections")
    tree = build_section_tree(user_id)
    flat_display = flatten_tree_for_display(tree)

    if not flat_display:
        st.sidebar.info("No sections yet. Create one below.")
    else:
        options = ["All Sections"] + [label for label, _ in flat_display]
        selected_name = st.sidebar.selectbox("Select section:", options)
        if selected_name == "All Sections":
            st.session_state.selected_section_id = None
        else:
            idx = options.index(selected_name) - 1
            st.session_state.selected_section_id = flat_display[idx][1]

    st.sidebar.divider()

    # Create section (optionally as a subsection of an existing one)
    with st.sidebar.expander("➕ Create a new section"):
        new_section_name = st.text_input("Section name", label_visibility="collapsed",
                                         placeholder="e.g. Waterfalls, Islands, National Parks")
        parent_options = ["None (top-level)"] + [label for label, _ in flat_display]
        parent_choice = st.selectbox("Parent section (optional)", parent_options)
        if st.button("Create", use_container_width=True):
            name = new_section_name.strip()
            if not name:
                st.warning("Type a section name first.")
            else:
                parent_id = None
                if parent_choice != "None (top-level)":
                    parent_id = flat_display[parent_options.index(parent_choice) - 1][1]
                siblings = [s["name"].lower() for s in tree.get(parent_id, [])]
                if name.lower() in siblings:
                    st.warning("A section with that name already exists there.")
                else:
                    add_section(user_id, name, parent_id)
                    st.rerun()


# ----------------------------------------------------------------------
# DASHBOARD PAGE
# ----------------------------------------------------------------------
def _scope_sections(user_id, selected_section_id):
    """Returns the list of section dicts in scope for the current sidebar filter."""
    all_secs = get_all_sections(user_id)
    if selected_section_id is None:
        return all_secs
    scope_ids = set(get_descendant_section_ids(user_id, selected_section_id))
    return [s for s in all_secs if s["id"] in scope_ids]


def render_dashboard_page(user_id, selected_section_id=None):
    st.title("📊 Dashboard")

    sections = _scope_sections(user_id, selected_section_id)
    all_places = [p for s in sections for p in get_places(s["id"])]
    total_all = len(all_places)
    visited_all = sum(p["visited"] for p in all_places)
    remaining_all = total_all - visited_all
    pct = int((visited_all / total_all) * 100) if total_all else 0

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Places", total_all)
    m2.metric("Visited ✅", visited_all)
    m3.metric("Remaining ⏳", remaining_all)
    st.progress(pct, text=f"{pct}% of your bucket list complete")

    if total_all == 0:
        st.info("No places yet. Go to 'Add Places' to start building your list.")
        return

    st.header("📈 Progress Overview")
    c1, c2 = st.columns(2)
    with c1:
        fig_overall = go.Figure(data=[go.Pie(
            labels=["Visited", "Remaining"],
            values=[visited_all, remaining_all],
            marker=dict(colors=[COLOR_VISITED, COLOR_REMAINING]),
            hole=0.45,
        )])
        fig_overall.update_layout(title="All Places Combined", height=350,
                                   margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(fig_overall, use_container_width=True, key="pie_overall_dash")

    with c2:
        sec_names = [s["name"] for s in sections]
        visited_counts = [sum(p["visited"] for p in get_places(s["id"])) for s in sections]
        totals = [len(get_places(s["id"])) for s in sections]
        remaining_counts = [t - v for t, v in zip(totals, visited_counts)]
        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(name="Visited", x=sec_names, y=visited_counts, marker_color=COLOR_VISITED))
        fig_bar.add_trace(go.Bar(name="Remaining", x=sec_names, y=remaining_counts, marker_color=COLOR_REMAINING))
        fig_bar.update_layout(barmode="stack", title="Progress by Section", height=350,
                               margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(fig_bar, use_container_width=True, key="bar_overall_dash")


# ----------------------------------------------------------------------
# MY LIST PAGE (recursive rendering for subsections)
# ----------------------------------------------------------------------
def _section_has_search_match(section, tree, search_term):
    places = get_places(section["id"])
    if any(search_term.lower() in p["name"].lower() for p in places):
        return True
    return any(_section_has_search_match(c, tree, search_term) for c in tree.get(section["id"], []))


def render_section_block(section, tree, search_term, depth, user_id):
    if search_term and not _section_has_search_match(section, tree, search_term):
        return False

    places = get_places(section["id"])
    display_places = (
        [p for p in places if search_term.lower() in p["name"].lower()] if search_term else places
    )
    visited_count = sum(p["visited"] for p in places)

    if depth == 0:
        box = st.container(border=True)
    else:
        indent_col, content_col = st.columns([min(depth, 4) * 0.5, 10])
        box = content_col.container(border=True)

    with box:
        icon = "📂" if depth == 0 else "📁"
        head_col, del_col = st.columns([5, 1])
        head_col.markdown(f"### {icon} {section['name']}  \n{visited_count}/{len(places)} visited")
        if del_col.button("🗑️ Delete", key=f"del_sec_{section['id']}"):
            delete_section(section["id"])
            st.rerun()

        table_col, chart_col = st.columns([3, 1])

        with table_col:
            df = pd.DataFrame(
                [{"Place": p["name"], "Visited": p["visited"], "Priority": p["priority"],
                  "Notes": p["notes"], "Visited On": p["visited_date"] or "",
                  "Latitude": p["lat"], "Longitude": p["lon"]} for p in display_places]
            )
            if df.empty:
                df = pd.DataFrame(columns=["Place", "Visited", "Priority", "Notes",
                                            "Visited On", "Latitude", "Longitude"])

            editor_key = f"editor_{section['id']}"
            st.data_editor(
                df, key=editor_key, num_rows="dynamic", use_container_width=True, hide_index=True,
                column_config={
                    "Place": st.column_config.TextColumn("Place", required=True),
                    "Visited": st.column_config.CheckboxColumn("Visited ✅"),
                    "Priority": st.column_config.SelectboxColumn("Priority", options=PRIORITY_OPTIONS),
                    "Notes": st.column_config.TextColumn("Notes"),
                    "Visited On": st.column_config.TextColumn("Visited On", disabled=True),
                    "Latitude": st.column_config.NumberColumn("Lat (optional)", format="%.4f"),
                    "Longitude": st.column_config.NumberColumn("Lon (optional)", format="%.4f"),
                },
            )
            st.caption("Tick ✅ to mark visited. Add rows at the bottom, delete via the trash icon on "
                       "hover. Leave Lat/Lon blank — new rows get auto-located for the map.")

            if st.button("💾 Save Changes", key=f"save_{section['id']}"):
                diff = st.session_state.get(editor_key, {})
                ids = [p["id"] for p in display_places]

                for idx in diff.get("deleted_rows", []):
                    if idx < len(ids):
                        delete_place(ids[idx])

                for idx_str, changes in diff.get("edited_rows", {}).items():
                    idx = int(idx_str)
                    if idx >= len(ids):
                        continue
                    update_place(
                        ids[idx], name=changes.get("Place"), visited=changes.get("Visited"),
                        lat=changes.get("Latitude"), lon=changes.get("Longitude"),
                        notes=changes.get("Notes"), priority=changes.get("Priority"),
                    )

                for new_row in diff.get("added_rows", []):
                    name = (new_row.get("Place") or "").strip()
                    if not name:
                        continue
                    lat = new_row.get("Latitude")
                    lon = new_row.get("Longitude")
                    if lat is None or lon is None:
                        lat, lon = geocode(name)
                    insert_place(
                        section["id"], name, visited=bool(new_row.get("Visited", False)),
                        lat=lat, lon=lon, notes=new_row.get("Notes", ""), priority=new_row.get("Priority"),
                    )

                st.success("Saved!")
                st.rerun()

            with st.expander("➕ Add a subsection here"):
                sub_name = st.text_input("Subsection name", key=f"subname_{section['id']}",
                                         label_visibility="collapsed", placeholder="e.g. South India")
                if st.button("Create Subsection", key=f"addsub_{section['id']}"):
                    name = sub_name.strip()
                    existing_children = [c["name"].lower() for c in tree.get(section["id"], [])]
                    if not name:
                        st.warning("Type a name first.")
                    elif name.lower() in existing_children:
                        st.warning("A subsection with that name already exists here.")
                    else:
                        add_section(user_id, name, parent_id=section["id"])
                        st.rerun()

        with chart_col:
            if places:
                remaining = len(places) - visited_count
                fig = go.Figure(data=[go.Pie(
                    labels=["Visited", "Remaining"], values=[visited_count, remaining],
                    marker=dict(colors=[COLOR_VISITED, COLOR_REMAINING]), hole=0.45,
                )])
                fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=260,
                                   showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=-0.25))
                st.plotly_chart(fig, use_container_width=True, key=f"pie_{section['id']}")
            else:
                st.write("_Add places to see a chart._")

        for p in display_places:
            with st.expander(f"📝 Details: {p['name']}"):
                render_place_detail(p)

    for child in tree.get(section["id"], []):
        render_section_block(child, tree, search_term, depth + 1, user_id)

    return True


def render_my_list_page(user_id, selected_section_id=None):
    all_secs = get_all_sections(user_id)
    if not all_secs:
        st.info("No sections yet — create one in the sidebar.")
        return

    tree = build_section_tree(user_id)
    search_term = st.text_input("🔍 Search your places", placeholder="Type a place name...")

    any_shown = False
    if selected_section_id is None:
        for root in tree.get(None, []):
            any_shown = render_section_block(root, tree, search_term, 0, user_id) or any_shown
    else:
        node = next((s for s in all_secs if s["id"] == selected_section_id), None)
        if node is None:
            st.info("Selected section not found.")
            return
        any_shown = render_section_block(node, tree, search_term, 0, user_id)

    if search_term and not any_shown:
        st.info(f"No places matching '{search_term}'.")

    # CSV export (scoped to the sidebar filter)
    scope = _scope_sections(user_id, selected_section_id)
    all_rows = []
    for s in scope:
        for p in get_places(s["id"]):
            all_rows.append({
                "Section": s["name"], "Place": p["name"], "Visited": p["visited"],
                "Visited On": p["visited_date"] or "", "Priority": p["priority"],
                "Notes": p["notes"], "Latitude": p["lat"], "Longitude": p["lon"],
                "Description": p["description"],
            })
    if all_rows:
        csv_data = pd.DataFrame(all_rows).to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Export my list as CSV", csv_data, "bucket_list.csv", "text/csv")


# ----------------------------------------------------------------------
# ADD PLACES PAGE — Quick Add / Bulk Add / Excel Upload
# ----------------------------------------------------------------------
def render_add_places_page(user_id, selected_section_id=None):
    all_secs = get_all_sections(user_id)
    if not all_secs:
        st.info("Create a section first in the sidebar.")
        return

    tree = build_section_tree(user_id)
    flat_display = flatten_tree_for_display(tree)
    labels = [label for label, _ in flat_display]
    ids = [sid for _, sid in flat_display]
    default_index = ids.index(selected_section_id) if selected_section_id in ids else 0

    mode = st.radio(
        "How do you want to add places?",
        ["✏️ Quick add (one at a time)", "📋 Bulk add (paste a list)", "📊 Excel upload"],
        horizontal=True, label_visibility="collapsed",
    )

    # ---------------- Quick Add ----------------
    if mode.startswith("✏️"):
        st.subheader("✏️ Quick Add")
        c1, c2 = st.columns([2, 1])
        place_name = c1.text_input("Place name", placeholder="e.g. Golden Temple, Amritsar")
        chosen_label = c2.selectbox("Section", labels, index=default_index)
        chosen_section_id = ids[labels.index(chosen_label)]

        c3, c4 = st.columns(2)
        priority = c3.selectbox("Priority", PRIORITY_OPTIONS, index=1)
        notes = c4.text_input("Notes (optional)", placeholder="e.g. Go during winter")

        with st.expander("📍 Set location manually (optional — leave blank to auto-locate)"):
            mc1, mc2 = st.columns(2)
            manual_lat = mc1.number_input("Latitude", value=None, format="%.6f", placeholder="e.g. 28.6139")
            manual_lon = mc2.number_input("Longitude", value=None, format="%.6f", placeholder="e.g. 77.2090")

        if st.button("📍 Add & Locate on Map", type="primary"):
            if not place_name.strip():
                st.warning("Type a place name first.")
            else:
                existing_names = {p["name"].lower() for p in get_places(chosen_section_id)}
                if place_name.strip().lower() in existing_names:
                    st.warning("That place is already in this section.")
                else:
                    if manual_lat is not None and manual_lon is not None:
                        lat, lon = manual_lat, manual_lon
                    else:
                        with st.spinner("Locating on the map..."):
                            lat, lon = geocode(place_name.strip())
                    insert_place(chosen_section_id, place_name.strip(), visited=False,
                                 lat=lat, lon=lon, notes=notes, priority=priority)
                    if lat is not None:
                        st.success(f"Added '{place_name.strip()}' — found it on the map!")
                        st.plotly_chart(small_map(lat, lon, place_name.strip()), use_container_width=True)
                    else:
                        st.warning(f"Added '{place_name.strip()}', but couldn't auto-locate it. "
                                   "Open 'My List' to set Lat/Lon manually, or try a more specific name "
                                   "(e.g. add the city or country).")

    # ---------------- Bulk Add ----------------
    elif mode.startswith("📋"):
        st.subheader("📋 Bulk Add")
        chosen_label = st.selectbox("Which section?", labels, index=default_index)
        chosen_section_id = ids[labels.index(chosen_label)]
        st.write("Paste as many places as you like, **one per line**:")
        bulk_text = st.text_area(
            "Places", label_visibility="collapsed",
            placeholder="Taj Mahal\nGolden Temple\nMeenakshi Temple\nEiffel Tower",
            height=180,
        )

        if st.button("Add All Places", type="primary"):
            names = [line for line in bulk_text.splitlines() if line.strip()]
            if not names:
                st.warning("Type or paste at least one place name.")
            else:
                with st.spinner(f"Adding {len(names)} place(s) and locating them..."):
                    added, duplicates = bulk_add_places(chosen_section_id, names)

                msg = f"Added {len(added)} place(s) to {chosen_label.strip()}."
                if duplicates:
                    msg += f" Skipped {len(duplicates)} duplicate(s)."
                st.success(msg)

                located = [a for a in added if a["lat"] is not None]
                not_located = [a["name"] for a in added if a["lat"] is None]
                if located:
                    fig = px.scatter_mapbox(
                        pd.DataFrame(located), lat="lat", lon="lon", hover_name="name", zoom=2, height=320,
                    )
                    fig.update_traces(marker=dict(size=14, color=COLOR_REMAINING))
                    fig.update_layout(mapbox_style="open-street-map", margin=dict(t=0, b=0, l=0, r=0))
                    st.plotly_chart(fig, use_container_width=True)
                if not_located:
                    st.warning(f"Couldn't auto-locate: {', '.join(not_located)}. "
                               "You can set coordinates manually in 'My List'.")

    # ---------------- Excel Upload ----------------
    else:
        st.subheader("📊 Excel Upload")
        st.write("Upload a spreadsheet of places — matching columns are filled in automatically.")
        st.caption("Recognized columns (case-insensitive): **Place** (required), Section, "
                   "Latitude/Lat, Longitude/Lon, Notes, Priority, Visited.")

        template_df = pd.DataFrame([
            {"Place": "Taj Mahal", "Section": "Temples", "Latitude": 27.1751, "Longitude": 78.0421,
             "Notes": "Best at sunrise", "Priority": "High", "Visited": False},
            {"Place": "Ooty", "Section": "Hill Stations", "Latitude": "", "Longitude": "",
             "Notes": "", "Priority": "Medium", "Visited": False},
        ])
        template_buf = BytesIO()
        template_df.to_excel(template_buf, index=False, engine="openpyxl")
        st.download_button(
            "⬇️ Download a template", template_buf.getvalue(), "bucket_list_template.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        default_label = st.selectbox(
            "Default section (used only for rows with no 'Section' value)", labels, index=default_index,
        )
        default_section_id = ids[labels.index(default_label)]

        uploaded_file = st.file_uploader("Upload Excel file", type=["xlsx", "xls"])

        if uploaded_file is not None:
            try:
                df_upload = pd.read_excel(uploaded_file)
            except Exception as e:
                st.error(f"Couldn't read that file: {e}")
                df_upload = None

            if df_upload is not None and len(df_upload) > 0:
                col_map = {str(c).lower().strip(): c for c in df_upload.columns}

                def find_col(*names):
                    for n in names:
                        if n in col_map:
                            return col_map[n]
                    for key, orig in col_map.items():
                        for n in names:
                            if n in key:
                                return orig
                    return None

                place_col = find_col("place", "name", "places")
                section_col = find_col("section", "category")
                lat_col = find_col("latitude", "lat")
                lon_col = find_col("longitude", "lon", "long", "lng")
                notes_col = find_col("notes", "note")
                priority_col = find_col("priority")
                visited_col = find_col("visited", "done")

                if place_col is None:
                    st.error("Couldn't find a 'Place' column in your file — check the template above.")
                else:
                    st.write(f"Found {len(df_upload)} row(s). Preview:")
                    st.dataframe(df_upload.head(10), use_container_width=True)

                    if st.button("📥 Import All Rows", type="primary"):
                        imported, skipped_dupe, failed_geocode = 0, 0, []
                        section_cache = {s["name"].lower(): s["id"] for s in get_all_sections(user_id)}

                        with st.spinner("Importing places..."):
                            for _, row in df_upload.iterrows():
                                name = str(row.get(place_col, "")).strip()
                                if not name or name.lower() == "nan":
                                    continue

                                target_section_id = default_section_id
                                if section_col:
                                    sec_name = str(row.get(section_col, "")).strip()
                                    if sec_name and sec_name.lower() != "nan":
                                        if sec_name.lower() not in section_cache:
                                            section_cache[sec_name.lower()] = add_section(
                                                user_id, sec_name, parent_id=None
                                            )
                                        target_section_id = section_cache[sec_name.lower()]

                                existing_names = {p["name"].lower() for p in get_places(target_section_id)}
                                if name.lower() in existing_names:
                                    skipped_dupe += 1
                                    continue

                                lat = row.get(lat_col) if lat_col else None
                                lon = row.get(lon_col) if lon_col else None
                                lat = float(lat) if pd.notna(lat) else None
                                lon = float(lon) if pd.notna(lon) else None
                                if lat is None or lon is None:
                                    lat, lon = geocode(name)
                                    if lat is None:
                                        failed_geocode.append(name)

                                notes = (str(row.get(notes_col, "")).strip()
                                         if notes_col and pd.notna(row.get(notes_col)) else "")
                                priority = (_normalize_priority(row.get(priority_col))
                                            if priority_col and pd.notna(row.get(priority_col)) else None)
                                visited = (bool(row.get(visited_col))
                                           if visited_col and pd.notna(row.get(visited_col)) else False)

                                insert_place(target_section_id, name, visited=visited, lat=lat, lon=lon,
                                             notes=notes, priority=priority)
                                imported += 1

                        msg = f"Imported {imported} place(s)."
                        if skipped_dupe:
                            msg += f" Skipped {skipped_dupe} duplicate(s)."
                        st.success(msg)
                        if failed_geocode:
                            preview = ", ".join(failed_geocode[:8])
                            more = "..." if len(failed_geocode) > 8 else ""
                            st.warning(f"Couldn't auto-locate {len(failed_geocode)}: {preview}{more}. "
                                       "You can set coordinates manually in 'My List'.")
                        st.rerun()


# ----------------------------------------------------------------------
# MAP PAGE — Folium: native zoom controls + clickable "Get Directions"
# ----------------------------------------------------------------------
def render_map_page(user_id, selected_section_id=None):
    st.subheader("🗺️ Your places on the map")

    sections = _scope_sections(user_id, selected_section_id)
    all_rows = []
    for s in sections:
        for p in get_places(s["id"]):
            if p["lat"] is not None and p["lon"] is not None:
                all_rows.append({
                    "Place": p["name"], "Section": s["name"],
                    "Status": "Visited" if p["visited"] else "Remaining",
                    "Priority": p["priority"], "lat": p["lat"], "lon": p["lon"],
                })

    if not all_rows:
        st.info("No located places yet. Add places in the 'Add Places' page — they're auto-located automatically.")
        return

    c1, c2 = st.columns([2, 2])
    filter_choice = c1.radio("Show:", ["All", "Visited only", "Remaining only"], horizontal=True)
    priority_filter = c2.multiselect("Filter by priority", PRIORITY_OPTIONS, default=PRIORITY_OPTIONS)

    df = pd.DataFrame(all_rows)
    if filter_choice == "Visited only":
        df = df[df["Status"] == "Visited"]
    elif filter_choice == "Remaining only":
        df = df[df["Status"] == "Remaining"]
    df = df[df["Priority"].isin(priority_filter)]

    if df.empty:
        st.info("Nothing to show for this filter.")
        return

    center_lat = df["lat"].mean()
    center_lon = df["lon"].mean()
    fmap = folium.Map(location=[center_lat, center_lon], zoom_start=4,
                       control_scale=True, tiles="OpenStreetMap")

    for _, row in df.iterrows():
        color = "green" if row["Status"] == "Visited" else "red"
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={row['lat']},{row['lon']}"
        popup_html = f"""
            <div style="font-family: sans-serif; font-size: 14px; min-width: 160px;">
                <b>{row['Place']}</b><br>
                {row['Section']} &middot; {row['Priority']}<br>
                <a href="{gmaps_url}" target="_blank" rel="noopener noreferrer">📍 Get Directions</a>
            </div>
        """
        folium.Marker(
            location=[row["lat"], row["lon"]],
            tooltip=row["Place"],
            popup=folium.Popup(popup_html, max_width=250),
            icon=folium.Icon(color=color, icon="flag"),
        ).add_to(fmap)

    st_folium(fmap, use_container_width=True, height=550, returned_objects=[])

    st.caption("💡 Use the **+ / −** buttons (top-left of the map) or scroll to zoom. "
               "Click a marker, then tap **'Get Directions'** in its popup to open Google Maps.")

    missing = sum(1 for s in sections for p in get_places(s["id"]) if p["lat"] is None or p["lon"] is None)
    if missing:
        st.caption(f"ℹ️ {missing} place(s) couldn't be auto-located. "
                   "You can set their Latitude/Longitude manually in the 'My List' page.")


# ----------------------------------------------------------------------
# STATISTICS PAGE
# ----------------------------------------------------------------------
def render_statistics_page(user_id, selected_section_id=None):
    st.title("📈 Statistics")

    sections = _scope_sections(user_id, selected_section_id)
    all_places = [p for s in sections for p in get_places(s["id"])]

    if not all_places:
        st.info("Add some places to see statistics here.")
        return

    visited_dates = sorted(p["visited_date"] for p in all_places if p["visited_date"])
    st.subheader("Visits Over Time")
    if visited_dates:
        df_dates = pd.DataFrame({"date": pd.to_datetime(visited_dates)})
        df_dates["count"] = 1
        df_dates = df_dates.groupby("date").sum().cumsum().reset_index()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df_dates["date"], y=df_dates["count"],
                                  mode="lines+markers", line=dict(color=COLOR_VISITED)))
        fig.update_layout(title="Places Visited (Cumulative)", height=350,
                           margin=dict(t=40, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No visits recorded yet — check some places off to see your trend here.")

    st.subheader("By Priority")
    priority_counts = {}
    for p in all_places:
        priority_counts[p["priority"]] = priority_counts.get(p["priority"], 0) + 1
    fig2 = go.Figure(data=[go.Bar(
        x=list(priority_counts.keys()), y=list(priority_counts.values()),
        marker_color=["#e74c3c", "#f39c12", "#3498db"][:len(priority_counts)],
    )])
    fig2.update_layout(title="Places by Priority", height=300, margin=dict(t=40, b=10, l=10, r=10))
    st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Sections by Size")
    sec_counts = sorted(
        ((s["name"], len(get_places(s["id"]))) for s in sections), key=lambda x: -x[1]
    )
    for name, cnt in sec_counts[:15]:
        st.write(f"- {name}: {cnt} place(s)")


# ----------------------------------------------------------------------
# MAIN APP
# ----------------------------------------------------------------------
def main_app():
    user_id = st.session_state.user_id
    render_sidebar(user_id)
    selected_section_id = st.session_state.get("selected_section_id", None)

    st.sidebar.divider()
    if st.sidebar.button("🎲 Surprise Me — pick my next trip"):
        sections = _scope_sections(user_id, selected_section_id)
        unvisited = [(s["name"], p) for s in sections for p in get_places(s["id"]) if not p["visited"]]
        st.session_state.surprise_place = random.choice(unvisited) if unvisited else "none"

    if st.session_state.surprise_place:
        if st.session_state.surprise_place == "none":
            st.sidebar.info("Everything's visited — add more places!")
        else:
            sec_name, place = st.session_state.surprise_place
            st.sidebar.success(f"✨ Go to **{place['name']}** ({sec_name})!")
            if place["lat"] is not None:
                st.sidebar.plotly_chart(small_map(place["lat"], place["lon"], place["name"]),
                                         use_container_width=True)

    st.title("🌍 My Bucket List Tracker")

    page = st.session_state.current_page
    if page == "Dashboard":
        render_dashboard_page(user_id, selected_section_id)
    elif page == "My List":
        render_my_list_page(user_id, selected_section_id)
    elif page == "Add Places":
        render_add_places_page(user_id, selected_section_id)
    elif page == "Map":
        render_map_page(user_id, selected_section_id)
    elif page == "Statistics":
        render_statistics_page(user_id, selected_section_id)


# ----------------------------------------------------------------------
# ROUTER
# ----------------------------------------------------------------------
if st.session_state.user_id is None:
    auth_page()
else:
    main_app()