            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    cols = [col[1] for col in cur.fetchall()]
    if "role_model" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN role_model TEXT DEFAULT ''")
    if "telegram_chat_id" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            target_date TEXT NOT NULL,
            target_time TEXT DEFAULT '',
            is_completed INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            external_id TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    cur.execute("PRAGMA table_info(schedule_items)")
    s_cols = [col[1] for col in cur.fetchall()]
    if "target_time" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN target_time TEXT DEFAULT ''")
    if "source" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN source TEXT DEFAULT 'manual'")
    if "external_id" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN external_id TEXT DEFAULT ''")
    if "reminder_sent" not in s_cols:
        cur.execute("ALTER TABLE schedule_items ADD COLUMN reminder_sent INTEGER DEFAULT 0")

    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS video_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            stage TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            perspective INTEGER DEFAULT 1,
            source_pdf TEXT DEFAULT '',
            result_json TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT DEFAULT ''
        )
    """)

    cur.execute("PRAGMA table_info(video_jobs)")
    v_cols = [col[1] for col in cur.fetchall()]
    for col_name, col_def in [
        ("perspective", "INTEGER DEFAULT 1"),
        ("source_pdf", "TEXT DEFAULT ''"),
        ("result_json", "TEXT DEFAULT ''"),
        ("video_url", "TEXT DEFAULT ''"),
        ("error", "TEXT DEFAULT ''"),
    ]:
        if col_name not in v_cols:
            cur.execute(f"ALTER TABLE video_jobs ADD COLUMN {col_name} {col_def}")
    cur.execute("PRAGMA table_info(video_jobs)")
    v_cols = [c[1] for c in cur.fetchall()]
    for col_name, col_def in [
        ("user_id", "INTEGER"),
        ("language", "TEXT DEFAULT 'ar'"),
        ("source_hash", "TEXT DEFAULT ''"),
        ("engine", "TEXT DEFAULT 'cinematic'"),
        ("mode", "TEXT DEFAULT 'ai'"),
        ("youtube_json", "TEXT DEFAULT '[]'"),
        ("material_id", "INTEGER"),
    ]:
        if col_name not in v_cols:
            cur.execute(f"ALTER TABLE video_jobs ADD COLUMN {col_name} {col_def}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_video_jobs_queue ON video_jobs(status,created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_video_jobs_user ON video_jobs(user_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS study_room_settings (
        user_id INTEGER PRIMARY KEY, focus_mode INTEGER DEFAULT 1, audio_type TEXT DEFAULT 'none',
        audio_volume REAL DEFAULT 0.35, quran_reciter TEXT DEFAULT '1', quran_surah INTEGER DEFAULT 1,
        noise_type TEXT DEFAULT 'white', updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS app_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT NOT NULL, message TEXT DEFAULT '',
        kind TEXT DEFAULT 'info', is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON app_notifications(user_id,created_at)")

    # Existing users get a stable public ID once.
    # IMPORTANT: this runs only AFTER the full users-column migration above.
    cur.execute("PRAGMA table_info(users)")
    final_user_cols = {c[1] for c in cur.fetchall()}
    if "public_user_id" in final_user_cols:
        rows = cur.execute("SELECT id, public_user_id FROM users ORDER BY id").fetchall()
        for row in rows:
            if not row[1]:
                code = "STX-" + secrets.token_hex(5).upper()
                cur.execute("UPDATE users SET public_user_id=? WHERE id=?", (code, row[0]))

    # If the server restarted while a video was processing, put it back in the durable queue.
    cur.execute("UPDATE video_jobs SET status='queued', stage='أعيد إلى قائمة الانتظار بعد إعادة تشغيل السيرفر' WHERE status='processing'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_link_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS focus_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            is_done INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blueprint_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            node_order INTEGER NOT NULL,
            title TEXT NOT NULL,
            philosophy TEXT NOT NULL,
            timeframe TEXT DEFAULT '',
            course_title TEXT DEFAULT '',
            course_link TEXT DEFAULT '',
            course_provider TEXT DEFAULT '',
            challenge_question TEXT NOT NULL,
            challenge_options TEXT NOT NULL,
            correct_index INTEGER NOT NULL,
            is_unlocked INTEGER DEFAULT 0,
            is_completed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    # ترحيل الأعمدة الجديدة لو كان الجدول منشأ مسبقاً. لا نحذف أو نعيد إنشاء بيانات الطلاب.
    cur.execute("PRAGMA table_info(blueprint_nodes)")
    b_cols = [col[1] for col in cur.fetchall()]
    for col_name in ["timeframe", "course_title", "course_link", "course_provider", "learning_plan", "progress_json"]:
        if col_name not in b_cols:
            cur.execute(f"ALTER TABLE blueprint_nodes ADD COLUMN {col_name} TEXT DEFAULT ''")

    # Private admin/analytics tables. No passwords, API keys, or auth tokens are stored in clear text.
    cur.execute("PRAGMA table_info(users)")
    user_cols = [c[1] for c in cur.fetchall()]
    for col_name, col_def in [
        ("last_login_at", "TEXT DEFAULT ''"),
