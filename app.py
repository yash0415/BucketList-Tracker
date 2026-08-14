"""
Bucket List Tracker
--------------------
Run with:
    pip install streamlit plotly pandas requests
    streamlit run bucket_list_app.py
"""

import streamlit as st
import sqlite3
import hashlib
import hmac
import os
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
st.set_page_config(page_title="My Bucket List Tracker", page_icon="🌍", layout="wide")

DB_FILE = "bucket_list.db"
COLOR_VISITED = "#2ecc71"
COLOR_REMAINING = "#e74c3c"
DEFAULT_SECTIONS = ["Temples", "Hill Stations", "Beaches"]


# ----------------------------------------------------------------------
# DATABASE SETUP
# ----------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            visited INTEGER NOT NULL DEFAULT 0,
            lat REAL,
            lon REAL,
            FOREIGN KEY (section_id) REFERENCES sections (id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()

    # --- lightweight migration: add lat/lon columns if this is an older DB file ---
    cur.execute("PRAGMA table_info(places)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "lat" not in existing_cols:
        cur.execute("ALTER TABLE places ADD COLUMN lat REAL")
    if "lon" not in existing_cols:
        cur.execute("ALTER TABLE places ADD COLUMN lon REAL")

    # --- migration: older DBs stored section as a text column directly on
    #     places (no separate sections table). Move that data across. ---
    cur.execute("PRAGMA table_info(places)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "section" in existing_cols and "section_id" not in existing_cols:
        cur.execute("ALTER TABLE places ADD COLUMN section_id INTEGER")
        cur.execute("SELECT DISTINCT user_id, section FROM places WHERE section IS NOT NULL")
        for user_id, section_name in cur.fetchall():
            cur.execute(
                "INSERT OR IGNORE INTO sections (user_id, name) VALUES (?, ?)",
                (user_id, section_name),
            )
            cur.execute("SELECT id FROM sections WHERE user_id = ? AND name = ?", (user_id, section_name))
            section_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE places SET section_id = ? WHERE user_id = ? AND section = ?",
                (section_id, user_id, section_name),
            )
        # drop rows that were only placeholders for empty sections (old schema trick)
        cur.execute("DELETE FROM places WHERE visited = -1")

    conn.commit()
    conn.close()


init_db()


# ----------------------------------------------------------------------
# PASSWORD HASHING (PBKDF2 - no external deps needed)
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
# GEOCODING (free, no API key - OpenStreetMap Nominatim)
# ----------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def geocode(place_name: str):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place_name, "format": "json", "limit": 1},
            headers={"User-Agent": "BucketListTrackerApp/1.0"},
            timeout=6,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


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
        "SELECT id, name, visited, lat, lon FROM places WHERE section_id = ? ORDER BY id",
        (section_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "visited": bool(r[2]), "lat": r[3], "lon": r[4]} for r in rows]


def insert_place(section_id, name, visited=False, lat=None, lon=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO places (section_id, name, visited, lat, lon) VALUES (?, ?, ?, ?, ?)",
        (section_id, name, int(visited), lat, lon),
    )
    conn.commit()
    conn.close()


def update_place(place_id, name=None, visited=None, lat=None, lon=None):
    conn = get_conn()
    cur = conn.cursor()
    fields, values = [], []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if visited is not None:
        fields.append("visited = ?")
        values.append(int(visited))
    if lat is not None:
        fields.append("lat = ?")
        values.append(lat)
    if lon is not None:
        fields.append("lon = ?")
        values.append(lon)
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
    """Insert many places at once, skipping duplicates, and try to geocode each."""
    existing = {p["name"].strip().lower() for p in get_places(section_id)}
    added = 0
    for raw_name in names:
        name = raw_name.strip()
        if not name or name.lower() in existing:
            continue
        lat, lon = geocode(name)
        insert_place(section_id, name, visited=False, lat=lat, lon=lon)
        existing.add(name.lower())
        added += 1
    return added


# ----------------------------------------------------------------------
# SESSION STATE
# ----------------------------------------------------------------------
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "username" not in st.session_state:
    st.session_state.username = None


def logout():
    st.session_state.user_id = None
    st.session_state.username = None


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
# TAB: MY LIST (editable table per section)
# ----------------------------------------------------------------------
def render_my_list_tab(user_id):
    with st.expander("➕ Create a new section"):
        c1, c2 = st.columns([4, 1])
        new_section_name = c1.text_input("Section name", label_visibility="collapsed",
                                          placeholder="e.g. Waterfalls, Islands, National Parks")
        if c2.button("Create", use_container_width=True):
            name = new_section_name.strip()
            existing = [s["name"] for s in get_sections(user_id)]
            if not name:
                st.warning("Type a section name first.")
            elif name in existing:
                st.warning("That section already exists.")
            else:
                add_section(user_id, name)
                st.rerun()

    sections = get_sections(user_id)
    if not sections:
        st.info("No sections yet — create one above to get started.")
        return

    for section in sections:
        places = get_places(section["id"])
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
                    [{"Place": p["name"], "Visited": p["visited"],
                      "Latitude": p["lat"], "Longitude": p["lon"]} for p in places]
                )
                if df.empty:
                    df = pd.DataFrame(columns=["Place", "Visited", "Latitude", "Longitude"])

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
                        "Latitude": st.column_config.NumberColumn("Lat (optional)", format="%.4f"),
                        "Longitude": st.column_config.NumberColumn("Lon (optional)", format="%.4f"),
                    },
                )
                st.caption("Tick a row's ✅ to mark it visited. Add new rows at the bottom, "
                           "or delete a row using the trash icon that appears on hover. "
                           "Leave Lat/Lon blank — they'll be auto-located for the map.")

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
                        place_id = ids[idx]
                        update_place(
                            place_id,
                            name=changes.get("Place"),
                            visited=changes.get("Visited"),
                            lat=changes.get("Latitude"),
                            lon=changes.get("Longitude"),
                        )

                    for new_row in diff.get("added_rows", []):
                        name = (new_row.get("Place") or "").strip()
                        if not name:
                            continue
                        lat = new_row.get("Latitude")
                        lon = new_row.get("Longitude")
                        if lat is None or lon is None:
                            lat, lon = geocode(name)
                        insert_place(section["id"], name, visited=bool(new_row.get("Visited", False)),
                                     lat=lat, lon=lon)

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


# ----------------------------------------------------------------------
# TAB: ADD PLACES (bulk add)
# ----------------------------------------------------------------------
def render_add_places_tab(user_id):
    st.subheader("➕ Add places — one at a time or a whole list at once")
    sections = get_sections(user_id)

    if not sections:
        st.info("Create a section first in the 'My List' tab.")
        return

    section_names = [s["name"] for s in sections]
    chosen_name = st.selectbox("Which section?", section_names)
    chosen_section = next(s for s in sections if s["name"] == chosen_name)

    st.write("Paste as many places as you like, **one per line**:")
    bulk_text = st.text_area(
        "Places",
        label_visibility="collapsed",
        placeholder="Taj Mahal\nGolden Temple\nMeenakshi Temple\nEiffel Tower",
        height=180,
    )

    if st.button("Add All Places", type="primary"):
        names = [line for line in bulk_text.splitlines() if line.strip()]
        if not names:
            st.warning("Type or paste at least one place name.")
        else:
            with st.spinner(f"Adding {len(names)} place(s) and locating them on the map..."):
                added = bulk_add_places(chosen_section["id"], names)
            skipped = len(names) - added
            msg = f"Added {added} place(s) to {chosen_name}."
            if skipped:
                msg += f" Skipped {skipped} duplicate(s)."
            st.success(msg)


# ----------------------------------------------------------------------
# TAB: MAP
# ----------------------------------------------------------------------
def render_map_tab(user_id):
    st.subheader("🗺️ Your places on the map")

    sections = get_sections(user_id)
    all_rows = []
    for s in sections:
        for p in get_places(s["id"]):
            if p["lat"] is not None and p["lon"] is not None:
                all_rows.append({
                    "Place": p["name"],
                    "Section": s["name"],
                    "Status": "Visited" if p["visited"] else "Remaining",
                    "lat": p["lat"],
                    "lon": p["lon"],
                })

    if not all_rows:
        st.info("No located places yet. Add places in the 'Add Places' tab — they're auto-located automatically.")
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
        df,
        lat="lat",
        lon="lon",
        color="Status",
        color_discrete_map={"Visited": COLOR_VISITED, "Remaining": COLOR_REMAINING},
        hover_name="Place",
        hover_data={"Section": True, "lat": False, "lon": False, "Status": False},
        zoom=1,
        height=550,
    )
    fig.update_layout(mapbox_style="open-street-map", margin=dict(t=0, b=0, l=0, r=0))
    st.plotly_chart(fig, use_container_width=True)

    missing = sum(
        1 for s in sections for p in get_places(s["id"]) if p["lat"] is None or p["lon"] is None
    )
    if missing:
        st.caption(f"ℹ️ {missing} place(s) couldn't be auto-located (unusual/ambiguous names). "
                   "You can set their Latitude/Longitude manually in the 'My List' tab.")


# ----------------------------------------------------------------------
# MAIN APP
# ----------------------------------------------------------------------
def main_app():
    user_id = st.session_state.user_id

    st.sidebar.title(f"👋 Hi, {st.session_state.username}")
    if st.sidebar.button("Log Out", use_container_width=True):
        logout()
        st.rerun()

    st.title("🌍 My Bucket List Tracker")

    sections = get_sections(user_id)
    all_places = [p for s in sections for p in get_places(s["id"])]
    total_all = len(all_places)
    visited_all = sum(p["visited"] for p in all_places)
    remaining_all = total_all - visited_all

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Places", total_all)
    m2.metric("Visited ✅", visited_all)
    m3.metric("Remaining ⏳", remaining_all)

    tab_list, tab_add, tab_map = st.tabs(["📋 My List", "➕ Add Places", "🗺️ Map"])
    with tab_list:
        render_my_list_tab(user_id)
        if total_all > 0:
            st.header("📊 Overall Progress")
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
                st.plotly_chart(fig_overall, use_container_width=True, key="pie_overall")
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
                st.plotly_chart(fig_bar, use_container_width=True, key="bar_overall")

    with tab_add:
        render_add_places_tab(user_id)

    with tab_map:
        render_map_tab(user_id)


# ----------------------------------------------------------------------
# ROUTER
# ----------------------------------------------------------------------
if st.session_state.user_id is None:
    auth_page()
else:
    main_app()