"""
Bucket List Tracker
------------------
Run with:
    pip install streamlit plotly pandas requests pillow
    streamlit run app.py
"""

import streamlit as st
import sqlite3
import hashlib
import hmac
import os
import random
import datetime
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from io import BytesIO
from PIL import Image

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
st.set_page_config(page_title="My Bucket List Tracker", page_icon="🌍", layout="wide")

DB_FILE = "bucket_list.db"
COLOR_VISITED = "#2ecc71"
COLOR_REMAINING = "#e74c3c"
DEFAULT_SECTIONS = ["Temples", "Hill Stations", "Beaches"]
PRIORITY_OPTIONS = ["🔥 High", "⭐ Medium", "🌙 Someday"]

# ----------------------------------------------------------------------
# DATABASE SETUP + MIGRATIONS
# ----------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
                    "INSERT OR IGNORE INTO sections (user_id, name) VALUES (?, ?)",
                    (user_id, section_name),
                )
                cur.execute(
                    "SELECT id FROM sections WHERE user_id = ? AND name = ?",
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
            name TEXT NOT NULL,
            UNIQUE(user_id, name),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
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
    user_id = cur.lastrowid
    for section in DEFAULT_SECTIONS:
        cur.execute("INSERT INTO sections (user_id, name) VALUES (?, ?)", (user_id, section))
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
# GEOCODING (fuzzy, multiple fallback queries)
# ----------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def geocode(place_name: str):
    import time

    base_headers = {"User-Agent": "BucketListTrackerApp/1.0"}
    base_params = {
        "format": "json",
        "limit": 3,
        "addressdetails": 1,
    }

    queries = [
        place_name.strip(),
        f"{place_name.strip()}, India",
        f"{place_name.strip()} temple",
        f"{place_name.strip()} beach",
        f"{place_name.strip()} hill station",
    ]

    best = None
    best_score = 0.0

    for q in queries:
        try:
            params = {**base_params, "q": q}
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers=base_headers,
                timeout=8,
            )
            results = resp.json()
            time.sleep(0.2)

            for r in results:
                score = float(r.get("importance", 0.5))
                place_type = (r.get("type") or "").lower()
                if "temple" in place_type or "shrine" in place_type:
                    score += 0.1
                if "beach" in place_type or "coast" in place_type:
                    score += 0.1
                if "hill" in place_type or "peak" in place_type:
                    score += 0.1

                if score > best_score:
                    best_score = score
                    best = (float(r["lat"]), float(r["lon"]))
        except Exception:
            continue

    return best if best is not None else (None, None)


def small_map(lat, lon, label):
    fig = px.scatter_mapbox(
        pd.DataFrame([{"lat": lat, "lon": lon, "label": label}]),
        lat="lat", lon="lon", hover_name="label", zoom=9, height=280,
    )
    fig.update_traces(marker=dict(size=16, color=COLOR_REMAINING))
    fig.update_layout(mapbox_style="open-street-map", margin=dict(t=0, b=0, l=0, r=0))
    return fig


# ----------------------------------------------------------------------
# DATA HELPERS
# ----------------------------------------------------------------------
def get_sections(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM sections WHERE user_id = ? ORDER BY id", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]


def add_section(user_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO sections (user_id, name) VALUES (?, ?)", (user_id, name))
    conn.commit()
    conn.close()


def delete_section(section_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    conn.commit()
    conn.close()


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
    new_id = cur.lastrowid
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
    existing = {p["name"].strip().lower() for p in get_places(section_id)}
    added = []
    for raw_name in names:
        name = raw_name.strip()
        if not name or name.lower() in existing:
            continue
        lat, lon = geocode(name)
        new_id = insert_place(section_id, name, visited=False, lat=lat, lon=lon)
        existing.add(name.lower())
        added.append({"id": new_id, "name": name, "lat": lat, "lon": lon})
    return added


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
        "Upload photos",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key=f"upload_{place['id']}",
    )

    if uploaded_files:
        for f in uploaded_files:
            try:
                img = Image.open(f)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                buf = buf.getvalue()
                add_photo(place["id"], buf)
            except Exception:
                st.error(f"Could not process {f.name}")
        st.success("Photos uploaded!")
        st.rerun()


# ----------------------------------------------------------------------
# SIDEBAR: NAVIGATION + SECTION SELECTOR
# ----------------------------------------------------------------------
def render_sidebar(user_id):
    st.sidebar.title(f"👋 Hi, {st.session_state.username}")

    if st.sidebar.button("Log Out", use_container_width=True):
        logout()
        st.rerun()

    st.sidebar.divider()

    # Navigation
    st.sidebar.subheader("🧭 Navigate")
    pages = ["Dashboard", "My List", "Add Places", "Map"]
    selected_page = st.sidebar.radio("Page:", pages, index=pages.index(st.session_state.current_page))
    if selected_page != st.session_state.current_page:
        st.session_state.current_page = selected_page
        st.rerun()

    st.sidebar.divider()

    # Sections
    st.sidebar.subheader("📂 Sections")

    sections = get_sections(user_id)
    if not sections:
        st.sidebar.info("No sections yet. Create one below.")
    else:
        section_names = ["All Sections"] + [s["name"] for s in sections]
        selected_name = st.sidebar.selectbox("Select section:", section_names)

        if selected_name == "All Sections":
            st.session_state.selected_section_id = None
        else:
            selected = next(s for s in sections if s["name"] == selected_name)
            st.session_state.selected_section_id = selected["id"]

    st.sidebar.divider()

    # Create section
    with st.sidebar.expander("➕ Create a new section"):
        new_section_name = st.text_input("Section name", label_visibility="collapsed",
                                         placeholder="e.g. Waterfalls, Islands, National Parks")
        if st.button("Create", use_container_width=True):
            name = new_section_name.strip()
            existing = [s["name"] for s in get_sections(user_id)]
            if not name:
                st.warning("Type a section name first.")
            elif name in existing:
                st.warning("That section already exists.")
            else:
                add_section(user_id, name)
                st.rerun()


# ----------------------------------------------------------------------
# DASHBOARD PAGE
# ----------------------------------------------------------------------
def render_dashboard_page(user_id, selected_section_id=None):
    st.title("📊 Dashboard")

    sections = get_sections(user_id)
    if selected_section_id is not None:
        sections = [s for s in sections if s["id"] == selected_section_id]

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
# MY LIST PAGE
# ----------------------------------------------------------------------
def render_my_list_page(user_id, selected_section_id=None):
    sections = get_sections(user_id)
    if not sections:
        st.info("No sections yet — create one in the sidebar.")
        return

    if selected_section_id is not None:
        sections = [s for s in sections if s["id"] == selected_section_id]
        if not sections:
            st.info("Selected section not found.")
            return

    search_term = st.text_input("🔍 Search your places", placeholder="Type a place name...")

    any_shown = False
    for section in sections:
        places = get_places(section["id"])
        if search_term:
            places = [p for p in places if search_term.lower() in p["name"].lower()]
            if not places:
                continue
        any_shown = True
        visited_count = sum(p["visited"] for p in places)

        with st.container(border=True):
            head_col, del_col = st.columns([5, 1])
            head_col.markdown(f"### 📂 {section['name']}  \n{visited_count}/{len(places)} visited")
            if del_col.button("🗑️ Delete section", key=f"del_sec_{section['id']}"):
                delete_section(section["id"])
                st.rerun()

            table_col, chart_col = st.columns([3, 1])

            with table_col:
                df = pd.DataFrame(
                    [{"Place": p["name"], "Visited": p["visited"], "Priority": p["priority"],
                      "Notes": p["notes"], "Visited On": p["visited_date"] or "",
                      "Latitude": p["lat"], "Longitude": p["lon"]} for p in places]
                )
                if df.empty:
                    df = pd.DataFrame(columns=["Place", "Visited", "Priority", "Notes",
                                                "Visited On", "Latitude", "Longitude"])

                editor_key = f"editor_{section['id']}"
                st.data_editor(
                    df,
                    key=editor_key,
                    num_rows="dynamic",
                    use_container_width=True,
                    hide_index=True,
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
                st.caption("Tick ✅ to mark visited (date is stamped automatically). "
                           "Add rows at the bottom, delete via the trash icon on hover. "
                           "Leave Lat/Lon blank — new rows get auto-located for the map.")

                if st.button("💾 Save Changes", key=f"save_{section['id']}"):
                    diff = st.session_state.get(editor_key, {})
                    ids = [p["id"] for p in places]

                    for idx in diff.get("deleted_rows", []):
                        if idx < len(ids):
                            delete_place(ids[idx])

                    for idx_str, changes in diff.get("edited_rows", {}).items():
                        idx = int(idx_str)
                        if idx >= len(ids):
                            continue
                        update_place(
                            ids[idx],
                            name=changes.get("Place"),
                            visited=changes.get("Visited"),
                            lat=changes.get("Latitude"),
                            lon=changes.get("Longitude"),
                            notes=changes.get("Notes"),
                            priority=changes.get("Priority"),
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
                            lat=lat, lon=lon, notes=new_row.get("Notes", ""),
                            priority=new_row.get("Priority"),
                        )

                    st.success("Saved!")
                    st.rerun()

            with chart_col:
                if places:
                    remaining = len(places) - visited_count
                    fig = go.Figure(data=[go.Pie(
                        labels=["Visited", "Remaining"],
                        values=[visited_count, remaining],
                        marker=dict(colors=[COLOR_VISITED, COLOR_REMAINING]),
                        hole=0.45,
                    )])
                    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=260,
                                       showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=-0.25))
                    st.plotly_chart(fig, use_container_width=True, key=f"pie_{section['id']}")
                else:
                    st.write("_Add places to see a chart._")

        for p in places:
            with st.expander(f"📝 Details: {p['name']}"):
                render_place_detail(p)

    if search_term and not any_shown:
        st.info(f"No places matching '{search_term}'.")

    # CSV export
    all_rows = []
    for s in get_sections(user_id):
        if selected_section_id is not None and s["id"] != selected_section_id:
            continue
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
# ADD PLACES PAGE
# ----------------------------------------------------------------------
def render_add_places_page(user_id, selected_section_id=None):
    sections = get_sections(user_id)
    if not sections:
        st.info("Create a section first in the sidebar.")
        return

    if selected_section_id is not None:
        default_section = next((s["name"] for s in sections if s["id"] == selected_section_id), sections[0]["name"])
    else:
        default_section = sections[0]["name"]

    mode = st.radio("How do you want to add places?", ["✏️ Quick add (one at a time)", "📋 Bulk add (paste a list)"],
                     horizontal=True, label_visibility="collapsed")

    section_names = [s["name"] for s in sections]

    if mode.startswith("✏️"):
        st.subheader("✏️ Quick Add")
        c1, c2 = st.columns([2, 1])
        place_name = c1.text_input("Place name", placeholder="e.g. Golden Temple, Amritsar")
        chosen_name = c2.selectbox("Section", section_names, index=section_names.index(default_section))
        c3, c4 = st.columns(2)
        priority = c3.selectbox("Priority", PRIORITY_OPTIONS, index=1)
        notes = c4.text_input("Notes (optional)", placeholder="e.g. Go during winter")

        if st.button("📍 Add & Locate on Map", type="primary"):
            if not place_name.strip():
                st.warning("Type a place name first.")
            else:
                chosen_section = next(s for s in sections if s["name"] == chosen_name)
                existing_names = {p["name"].lower() for p in get_places(chosen_section["id"])}
                if place_name.strip().lower() in existing_names:
                    st.warning("That place is already in this section.")
                else:
                    with st.spinner("Locating on the map..."):
                        lat, lon = geocode(place_name.strip())
                    insert_place(chosen_section["id"], place_name.strip(), visited=False,
                                 lat=lat, lon=lon, notes=notes, priority=priority)
                    if lat is not None:
                        st.success(f"Added '{place_name.strip()}' to {chosen_name} — found it on the map!")
                        st.plotly_chart(small_map(lat, lon, place_name.strip()), use_container_width=True)
                    else:
                        st.success(f"Added '{place_name.strip()}' to {chosen_name}.")
                        st.info("Couldn't auto-locate this one — you can set Lat/Lon manually in the 'My List' tab.")

    else:
        st.subheader("📋 Bulk Add")
        chosen_name = st.selectbox("Which section?", section_names, index=section_names.index(default_section))
        chosen_section = next(s for s in sections if s["name"] == chosen_name)
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
                    added = bulk_add_places(chosen_section["id"], names)
                skipped = len(names) - len(added)
                msg = f"Added {len(added)} place(s) to {chosen_name}."
                if skipped:
                    msg += f" Skipped {skipped} duplicate(s)."
                st.success(msg)

                located = [a for a in added if a["lat"] is not None]
                if located:
                    fig = px.scatter_mapbox(
                        pd.DataFrame(located), lat="lat", lon="lon", hover_name="name",
                        zoom=2, height=320,
                    )
                    fig.update_traces(marker=dict(size=14, color=COLOR_REMAINING))
                    fig.update_layout(mapbox_style="open-street-map", margin=dict(t=0, b=0, l=0, r=0))
                    st.plotly_chart(fig, use_container_width=True)
                if len(located) < len(added):
                    st.caption(f"ℹ️ {len(added) - len(located)} place(s) couldn't be auto-located.")


# ----------------------------------------------------------------------
# MAP PAGE (interactive, clickable to Google Maps)
# ----------------------------------------------------------------------
def render_map_page(user_id, selected_section_id=None):
    st.subheader("🗺️ Your places on the map")

    sections = get_sections(user_id)
    if selected_section_id is not None:
        sections = [s for s in sections if s["id"] == selected_section_id]

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

    filter_choice = st.radio("Show:", ["All", "Visited only", "Remaining only"], horizontal=True)
    df = pd.DataFrame(all_rows)
    if filter_choice == "Visited only":
        df = df[df["Status"] == "Visited"]
    elif filter_choice == "Remaining only":
        df = df[df["Status"] == "Remaining"]

    if df.empty:
        st.info("Nothing to show for this filter.")
        return

    fig = px.scatter_mapbox(
        df, lat="lat", lon="lon", color="Status",
        color_discrete_map={"Visited": COLOR_VISITED, "Remaining": COLOR_REMAINING},
        hover_name="Place",
        hover_data={"Section": True, "Priority": True, "lat": False, "lon": False, "Status": False},
        zoom=1, height=550,
    )

    # Enable zoom/pan modebar and make markers clickable via customdata + JS is not directly possible,
    # so we add an info box below: when user selects a place from dropdown, open Google Maps.
    fig.update_layout(
        mapbox_style="open-street-map",
        margin=dict(t=0, b=0, l=0, r=0),
        dragmode="pan",
    )

    map_config = {
        "displayModeBar": True,
        "scrollZoom": True,
        "modeBarButtonsToAdd": ["zoomIn2d", "zoomOut2d", "resetScale2d"],
    }

    st.plotly_chart(fig, use_container_width=True, key="main_map", config=map_config)

    st.caption("💡 Tip: Use the mouse wheel or the +/− buttons on the map to zoom in/out. "
               "Click a marker, then use the 'Open in Google Maps' button below.")

    # Simple place selector for Google Maps redirect
    place_options = [f"{r['Place']} ({r['Section']})" for _, r in df.iterrows()]
    selected_place_label = st.selectbox("Select a place to open in Google Maps:", place_options)

    if selected_place_label:
        selected_row = df[df["Place"] + " (" + df["Section"] + ")" == selected_place_label].iloc[0]
        lat = selected_row["lat"]
        lon = selected_row["lon"]
        if st.button("🧭 Open in Google Maps"):
            url = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
            st.markdown(f"[Open directions in Google Maps]({url})", unsafe_allow_html=True)
            st.info("If the link doesn't open automatically, click it above.")

    missing = sum(1 for s in sections for p in get_places(s["id"]) if p["lat"] is None or p["lon"] is None)
    if missing:
        st.caption(f"ℹ️ {missing} place(s) couldn't be auto-located. "
                   "You can set their Latitude/Longitude manually in the 'My List' page.")


# ----------------------------------------------------------------------
# MAIN APP
# ----------------------------------------------------------------------
def main_app():
    user_id = st.session_state.user_id

    render_sidebar(user_id)

    selected_section_id = st.session_state.get("selected_section_id", None)

    if st.session_state.surprise_place is None:
        st.session_state.surprise_place = None

    st.sidebar.divider()
    if st.sidebar.button("🎲 Surprise Me — pick my next trip"):
        sections = get_sections(user_id)
        if selected_section_id is not None:
            sections = [s for s in sections if s["id"] == selected_section_id]
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

    page = st.session_state.current_page

    if page == "Dashboard":
        render_dashboard_page(user_id, selected_section_id)
    elif page == "My List":
        render_my_list_page(user_id, selected_section_id)
    elif page == "Add Places":
        render_add_places_page(user_id, selected_section_id)
    elif page == "Map":
        render_map_page(user_id, selected_section_id)


# ----------------------------------------------------------------------
# ROUTER
# ----------------------------------------------------------------------
if st.session_state.user_id is None:
    auth_page()
else:
    main_app()