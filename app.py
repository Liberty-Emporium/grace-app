import os, sqlite3, secrets, json, time
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g)
import requests as _req
import bcrypt as _bcrypt

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DATA_DIR = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '/data')
DB_PATH  = os.path.join(DATA_DIR, 'grace.db')
os.makedirs(DATA_DIR, exist_ok=True)

OPENROUTER_KEY  = os.environ.get('OPENROUTER_API_KEY', '')
CAREGIVER_PIN   = os.environ.get('CAREGIVER_PIN', '1234')
USER_NAME       = os.environ.get('USER_NAME', 'Mom')
GRACE_MODEL     = 'openai/gpt-4o-mini'

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS medications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            dose        TEXT DEFAULT '',
            times       TEXT NOT NULL DEFAULT '[]',
            color       TEXT DEFAULT '#6366f1',
            photo_url   TEXT DEFAULT '',
            active      INTEGER DEFAULT 1,
            notes       TEXT DEFAULT '',
            created     TEXT DEFAULT (datetime("now"))
        );
        CREATE TABLE IF NOT EXISTS med_logs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            med_id  INTEGER NOT NULL,
            taken   INTEGER DEFAULT 0,
            log_date TEXT NOT NULL,
            log_time TEXT NOT NULL,
            scheduled_time TEXT NOT NULL,
            FOREIGN KEY(med_id) REFERENCES medications(id)
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            doctor      TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            appt_date   TEXT NOT NULL,
            appt_time   TEXT NOT NULL,
            notes       TEXT DEFAULT '',
            remind_min  INTEGER DEFAULT 60,
            done        INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime("now"))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            due_date    TEXT DEFAULT '',
            due_time    TEXT DEFAULT '',
            done        INTEGER DEFAULT 0,
            remind      INTEGER DEFAULT 1,
            created     TEXT DEFAULT (datetime("now"))
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT NOT NULL,
            ref_id      INTEGER DEFAULT 0,
            message     TEXT NOT NULL,
            fire_at     TEXT NOT NULL,
            fired       INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime("now"))
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            role    TEXT NOT NULL,
            content TEXT NOT NULL,
            ts      TEXT DEFAULT (datetime("now"))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    ''')
    db.commit()
    # Default settings
    defaults = {
        'user_name': USER_NAME,
        'voice_enabled': '1',
        'large_text': '1',
        'reminder_sound': '1',
        'caregiver_pin': CAREGIVER_PIN,
    }
    for k, v in defaults.items():
        db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
    db.commit()
    db.close()

init_db()

def get_setting(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default

# ── Helpers ───────────────────────────────────────────────────────────────────

def today_str():
    return date.today().isoformat()

def now_str():
    return datetime.now().strftime('%H:%M')

def friendly_time(t):
    """Convert 24h time string to friendly AM/PM."""
    try:
        h, m = map(int, t.split(':'))
        suffix = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    except:
        return t

def friendly_date(d):
    """Convert YYYY-MM-DD to friendly string."""
    try:
        dt = datetime.strptime(d, '%Y-%m-%d')
        today = date.today()
        delta = (dt.date() - today).days
        if delta == 0: return 'Today'
        if delta == 1: return 'Tomorrow'
        if delta == -1: return 'Yesterday'
        return dt.strftime('%A, %B %d')
    except:
        return d

def get_todays_meds():
    db = get_db()
    meds = db.execute('SELECT * FROM medications WHERE active=1').fetchall()
    today = today_str()
    result = []
    for med in meds:
        times = json.loads(med['times'])
        for t in times:
            log = db.execute(
                'SELECT * FROM med_logs WHERE med_id=? AND log_date=? AND scheduled_time=?',
                (med['id'], today, t)
            ).fetchone()
            result.append({
                'med': dict(med),
                'time': t,
                'friendly_time': friendly_time(t),
                'taken': bool(log and log['taken']),
                'log_id': log['id'] if log else None
            })
    result.sort(key=lambda x: x['time'])
    return result

def get_todays_appointments():
    db = get_db()
    today = today_str()
    appts = db.execute(
        'SELECT * FROM appointments WHERE appt_date=? AND done=0 ORDER BY appt_time',
        (today,)
    ).fetchall()
    return [dict(a) for a in appts]

def get_upcoming_appointments(days=7):
    db = get_db()
    today = today_str()
    future = (date.today() + timedelta(days=days)).isoformat()
    appts = db.execute(
        'SELECT * FROM appointments WHERE appt_date >= ? AND appt_date <= ? AND done=0 ORDER BY appt_date, appt_time',
        (today, future)
    ).fetchall()
    return [dict(a) for a in appts]

def get_todays_tasks():
    db = get_db()
    today = today_str()
    tasks = db.execute(
        'SELECT * FROM tasks WHERE (due_date=? OR due_date="") AND done=0 ORDER BY due_time',
        (today,)
    ).fetchall()
    return [dict(t) for t in tasks]

def get_due_reminders():
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    reminders = db.execute(
        'SELECT * FROM reminders WHERE fired=0 AND fire_at <= ?',
        (now,)
    ).fetchall()
    return [dict(r) for r in reminders]

# ── Grace AI ──────────────────────────────────────────────────────────────────

GRACE_SYSTEM = """You are Grace, a warm and patient AI assistant for an elderly user named {name}.

Your personality:
- Speak in SHORT, clear sentences. Never more than 2-3 sentences.
- Be warm, patient, and encouraging.
- Use simple words. No technical terms.
- Always offer to help with the next step.
- If something is confusing, offer to explain again.
- You help with: medications, appointments, tasks, and reminders.

Current context:
{context}

Safety rules:
- NEVER give medical advice
- NEVER suggest changing medication doses
- If the user seems distressed, suggest calling a family member

When the user asks you to do something (add a med, set a reminder, add appointment), respond with a JSON action plus a friendly message.

Action format (include in your response when needed):
<ACTION>{"type":"add_med","name":"...","dose":"...","times":["08:00"]}</ACTION>
<ACTION>{"type":"add_appointment","title":"...","date":"YYYY-MM-DD","time":"HH:MM","doctor":"...","location":"..."}</ACTION>
<ACTION>{"type":"add_task","title":"...","due_date":"YYYY-MM-DD"}</ACTION>
<ACTION>{"type":"add_reminder","message":"...","fire_at":"YYYY-MM-DD HH:MM"}</ACTION>
<ACTION>{"type":"mark_med_taken","med_name":"..."}</ACTION>

Always confirm before taking action: "Should I add that for you?"
"""

def call_grace(user_message, context=''):
    if not OPENROUTER_KEY:
        return "I'm here! To activate my full abilities, please ask your caregiver to add the AI key in settings. 💙"

    db = get_db()
    name = get_setting('user_name', 'there')

    # Build conversation history
    history = db.execute(
        'SELECT role, content FROM chat_history ORDER BY id DESC LIMIT 20'
    ).fetchall()
    history = list(reversed(history))

    messages = [{
        'role': 'system',
        'content': GRACE_SYSTEM.format(name=name, context=context)
    }]
    for h in history:
        messages.append({'role': h['role'], 'content': h['content']})
    messages.append({'role': 'user', 'content': user_message})

    try:
        r = _req.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_KEY}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://grace-assistant.up.railway.app',
                'X-Title': 'Grace - Elderly Care Assistant'
            },
            json={'model': GRACE_MODEL, 'messages': messages, 'max_tokens': 400},
            timeout=20
        )
        reply = r.json()['choices'][0]['message']['content']
    except Exception as e:
        app.logger.error(f'Grace AI error: {e}')
        reply = "I'm sorry, I had trouble thinking of a response. Could you try again? 💙"

    # Save to history
    db.execute('INSERT INTO chat_history(role,content) VALUES(?,?)', ('user', user_message))
    db.execute('INSERT INTO chat_history(role,content) VALUES(?,?)', ('assistant', reply))
    db.commit()

    return reply

def parse_grace_action(reply, db):
    """Extract and execute any <ACTION> from Grace's reply."""
    import re
    actions_done = []
    matches = re.findall(r'<ACTION>(.*?)</ACTION>', reply, re.DOTALL)
    today = today_str()
    name = get_setting('user_name', 'User')

    for match in matches:
        try:
            action = json.loads(match.strip())
            atype = action.get('type')

            if atype == 'add_med':
                times = action.get('times', ['08:00'])
                db.execute(
                    'INSERT INTO medications(name,dose,times,notes) VALUES(?,?,?,?)',
                    (action.get('name',''), action.get('dose',''), json.dumps(times), action.get('notes',''))
                )
                db.commit()
                actions_done.append(f"Added medication: {action.get('name')}")

            elif atype == 'add_appointment':
                db.execute(
                    'INSERT INTO appointments(title,doctor,location,appt_date,appt_time,notes) VALUES(?,?,?,?,?,?)',
                    (action.get('title',''), action.get('doctor',''), action.get('location',''),
                     action.get('date', today), action.get('time','09:00'), action.get('notes',''))
                )
                db.commit()
                actions_done.append(f"Added appointment: {action.get('title')}")

            elif atype == 'add_task':
                db.execute(
                    'INSERT INTO tasks(title,due_date) VALUES(?,?)',
                    (action.get('title',''), action.get('due_date', today))
                )
                db.commit()
                actions_done.append(f"Added task: {action.get('title')}")

            elif atype == 'add_reminder':
                db.execute(
                    'INSERT INTO reminders(type,message,fire_at) VALUES(?,?,?)',
                    ('custom', action.get('message',''), action.get('fire_at', today + ' 09:00'))
                )
                db.commit()
                actions_done.append(f"Set reminder: {action.get('message')}")

            elif atype == 'mark_med_taken':
                med_name = action.get('med_name', '').lower()
                med = db.execute(
                    'SELECT * FROM medications WHERE lower(name) LIKE ? AND active=1',
                    (f'%{med_name}%',)
                ).fetchone()
                if med:
                    times = json.loads(med['times'])
                    closest = min(times, key=lambda t: abs(
                        int(t.split(':')[0]) * 60 + int(t.split(':')[1]) -
                        datetime.now().hour * 60 - datetime.now().minute
                    )) if times else now_str()
                    db.execute(
                        'INSERT OR REPLACE INTO med_logs(med_id,taken,log_date,log_time,scheduled_time) VALUES(?,?,?,?,?)',
                        (med['id'], 1, today, now_str(), closest)
                    )
                    db.commit()
                    actions_done.append(f"Marked {med['name']} as taken")

        except Exception as e:
            app.logger.error(f'Grace action error: {e}')

    return actions_done

# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def security_headers(res):
    res.headers['X-Content-Type-Options'] = 'nosniff'
    res.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return res

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        db = get_db()
        db.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'db': str(e)}), 500

@app.route('/')
def home():
    meds      = get_todays_meds()
    appts     = get_todays_appointments()
    tasks     = get_todays_tasks()
    upcoming  = get_upcoming_appointments(7)
    reminders = get_due_reminders()
    name      = get_setting('user_name', 'there')

    # Greet based on time of day
    hour = datetime.now().hour
    if hour < 12:   greeting = f"Good morning, {name}! ☀️"
    elif hour < 17: greeting = f"Good afternoon, {name}! 🌤️"
    else:           greeting = f"Good evening, {name}! 🌙"

    return render_template('home.html',
        meds=meds, appts=appts, tasks=tasks,
        upcoming=upcoming, reminders=reminders,
        greeting=greeting, today=friendly_date(today_str()),
        name=name
    )

@app.route('/meds')
def meds_page():
    db = get_db()
    all_meds  = db.execute('SELECT * FROM medications WHERE active=1 ORDER BY name').fetchall()
    todays    = get_todays_meds()
    return render_template('meds.html', meds=all_meds, todays=todays)

@app.route('/appointments')
def appointments_page():
    db = get_db()
    today = today_str()
    appts = db.execute(
        'SELECT * FROM appointments WHERE appt_date >= ? AND done=0 ORDER BY appt_date, appt_time LIMIT 20',
        (today,)
    ).fetchall()
    return render_template('appointments.html',
        appts=[dict(a) for a in appts],
        friendly_date=friendly_date, friendly_time=friendly_time)

@app.route('/tasks')
def tasks_page():
    db = get_db()
    today = today_str()
    tasks = db.execute(
        'SELECT * FROM tasks WHERE done=0 ORDER BY due_date, due_time, created'
    ).fetchall()
    return render_template('tasks.html', tasks=[dict(t) for t in tasks])

# ── Med Actions ───────────────────────────────────────────────────────────────

@app.route('/api/med/taken', methods=['POST'])
def med_taken():
    data     = request.get_json() or {}
    med_id   = data.get('med_id')
    sched_t  = data.get('scheduled_time', now_str())
    today    = today_str()
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO med_logs(med_id,taken,log_date,log_time,scheduled_time) VALUES(?,?,?,?,?)',
        (med_id, 1, today, now_str(), sched_t)
    )
    db.commit()
    med = db.execute('SELECT name FROM medications WHERE id=?', (med_id,)).fetchone()
    name = get_setting('user_name', 'there')
    return jsonify({'ok': True, 'message': f"Great job, {name}! {med['name']} is marked as taken. 💊✅"})

@app.route('/api/med/untaken', methods=['POST'])
def med_untaken():
    data    = request.get_json() or {}
    med_id  = data.get('med_id')
    sched_t = data.get('scheduled_time', '')
    today   = today_str()
    db = get_db()
    db.execute(
        'DELETE FROM med_logs WHERE med_id=? AND log_date=? AND scheduled_time=?',
        (med_id, today, sched_t)
    )
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/task/done', methods=['POST'])
def task_done():
    data    = request.get_json() or {}
    task_id = data.get('task_id')
    db = get_db()
    db.execute('UPDATE tasks SET done=1 WHERE id=?', (task_id,))
    db.commit()
    task = db.execute('SELECT title FROM tasks WHERE id=?', (task_id,)).fetchone()
    return jsonify({'ok': True, 'message': f"Done! ✅ Great job completing \"{task['title']}\"!"})

@app.route('/api/reminder/dismiss', methods=['POST'])
def reminder_dismiss():
    data = request.get_json() or {}
    r_id = data.get('reminder_id')
    db = get_db()
    db.execute('UPDATE reminders SET fired=1 WHERE id=?', (r_id,))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/reminders/due')
def reminders_due():
    return jsonify({'reminders': get_due_reminders()})

# ── Grace AI API ──────────────────────────────────────────────────────────────

@app.route('/api/grace', methods=['POST'])
def grace_api():
    data    = request.get_json() or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'No message'}), 400

    # Build context
    meds  = get_todays_meds()
    appts = get_todays_appointments()
    tasks = get_todays_tasks()
    name  = get_setting('user_name', 'there')

    med_summary  = ', '.join([f"{m['med']['name']} at {m['friendly_time']}" + (' (taken)' if m['taken'] else ' (not yet taken)') for m in meds]) or 'none today'
    appt_summary = ', '.join([f"{a['title']} at {friendly_time(a['appt_time'])}" for a in appts]) or 'none today'
    task_summary = ', '.join([t['title'] for t in tasks]) or 'none today'

    context = f"""Today is {friendly_date(today_str())}.
Medications today: {med_summary}
Appointments today: {appt_summary}
Tasks today: {task_summary}"""

    reply = call_grace(message, context)

    # Execute any actions in the reply
    db = get_db()
    actions = parse_grace_action(reply, db)

    # Strip action tags from displayed reply
    import re
    clean_reply = re.sub(r'<ACTION>.*?</ACTION>', '', reply, flags=re.DOTALL).strip()

    return jsonify({
        'reply': clean_reply,
        'actions': actions,
        'speak': clean_reply  # for TTS
    })

# ── Caregiver mode ────────────────────────────────────────────────────────────

@app.route('/caregiver', methods=['GET', 'POST'])
def caregiver():
    if request.method == 'POST':
        pin = request.form.get('pin', '')
        if pin == get_setting('caregiver_pin', CAREGIVER_PIN):
            session['caregiver'] = True
            return redirect(url_for('caregiver_dashboard'))
        flash('Wrong PIN. Please try again.', 'error')
    return render_template('caregiver_pin.html')

def caregiver_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('caregiver'):
            return redirect(url_for('caregiver'))
        return f(*args, **kwargs)
    return decorated

@app.route('/caregiver/dashboard')
@caregiver_required
def caregiver_dashboard():
    db = get_db()
    meds  = db.execute('SELECT * FROM medications WHERE active=1 ORDER BY name').fetchall()
    appts = db.execute('SELECT * FROM appointments WHERE done=0 ORDER BY appt_date, appt_time').fetchall()
    tasks = db.execute('SELECT * FROM tasks WHERE done=0 ORDER BY due_date').fetchall()
    name  = get_setting('user_name', USER_NAME)
    return render_template('caregiver_dashboard.html',
        meds=meds, appts=[dict(a) for a in appts],
        tasks=[dict(t) for t in tasks], name=name,
        friendly_date=friendly_date, friendly_time=friendly_time)

@app.route('/caregiver/logout')
def caregiver_logout():
    session.pop('caregiver', None)
    return redirect(url_for('home'))

# ── Caregiver CRUD ────────────────────────────────────────────────────────────

@app.route('/caregiver/med/add', methods=['POST'])
@caregiver_required
def caregiver_add_med():
    name   = request.form.get('name', '').strip()
    dose   = request.form.get('dose', '').strip()
    notes  = request.form.get('notes', '').strip()
    times_raw = request.form.getlist('times')
    times  = [t.strip() for t in times_raw if t.strip()]
    color  = request.form.get('color', '#6366f1')
    if not name or not times:
        flash('Medicine name and at least one time are required.', 'error')
        return redirect(url_for('caregiver_dashboard'))
    db = get_db()
    db.execute(
        'INSERT INTO medications(name,dose,times,color,notes) VALUES(?,?,?,?,?)',
        (name, dose, json.dumps(times), color, notes)
    )
    db.commit()
    flash(f'{name} added! ✅', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/med/delete/<int:med_id>', methods=['POST'])
@caregiver_required
def caregiver_delete_med(med_id):
    db = get_db()
    db.execute('UPDATE medications SET active=0 WHERE id=?', (med_id,))
    db.commit()
    flash('Medication removed.', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/appointment/add', methods=['POST'])
@caregiver_required
def caregiver_add_appt():
    title    = request.form.get('title', '').strip()
    doctor   = request.form.get('doctor', '').strip()
    location = request.form.get('location', '').strip()
    appt_date = request.form.get('appt_date', '').strip()
    appt_time = request.form.get('appt_time', '09:00').strip()
    notes    = request.form.get('notes', '').strip()
    remind   = int(request.form.get('remind_min', 60))
    if not title or not appt_date:
        flash('Appointment title and date are required.', 'error')
        return redirect(url_for('caregiver_dashboard'))
    db = get_db()
    db.execute(
        'INSERT INTO appointments(title,doctor,location,appt_date,appt_time,notes,remind_min) VALUES(?,?,?,?,?,?,?)',
        (title, doctor, location, appt_date, appt_time, notes, remind)
    )
    # Auto-create reminder
    fire_dt = datetime.strptime(f"{appt_date} {appt_time}", '%Y-%m-%d %H:%M') - timedelta(minutes=remind)
    if fire_dt > datetime.now():
        db.execute(
            'INSERT INTO reminders(type,message,fire_at) VALUES(?,?,?)',
            ('appointment', f"📅 Reminder: {title} is in {remind} minutes!", fire_dt.strftime('%Y-%m-%d %H:%M'))
        )
    db.commit()
    flash(f'Appointment added! ✅', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/appointment/delete/<int:appt_id>', methods=['POST'])
@caregiver_required
def caregiver_delete_appt(appt_id):
    db = get_db()
    db.execute('DELETE FROM appointments WHERE id=?', (appt_id,))
    db.commit()
    flash('Appointment removed.', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/task/add', methods=['POST'])
@caregiver_required
def caregiver_add_task():
    title    = request.form.get('title', '').strip()
    due_date = request.form.get('due_date', '').strip()
    due_time = request.form.get('due_time', '').strip()
    if not title:
        flash('Task title is required.', 'error')
        return redirect(url_for('caregiver_dashboard'))
    db = get_db()
    db.execute('INSERT INTO tasks(title,due_date,due_time) VALUES(?,?,?)', (title, due_date, due_time))
    db.commit()
    flash(f'Task added! ✅', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/task/delete/<int:task_id>', methods=['POST'])
@caregiver_required
def caregiver_delete_task(task_id):
    db = get_db()
    db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
    db.commit()
    flash('Task removed.', 'success')
    return redirect(url_for('caregiver_dashboard'))

@app.route('/caregiver/settings', methods=['POST'])
@caregiver_required
def caregiver_settings():
    name    = request.form.get('user_name', '').strip()
    new_pin = request.form.get('new_pin', '').strip()
    api_key = request.form.get('api_key', '').strip()
    db = get_db()
    if name:
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('user_name', name))
    if new_pin and len(new_pin) >= 4:
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('caregiver_pin', new_pin))
    if api_key:
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('openrouter_key', api_key))
        os.environ['OPENROUTER_API_KEY'] = api_key
    db.commit()
    flash('Settings saved! ✅', 'success')
    return redirect(url_for('caregiver_dashboard'))

# ── Jinja filters ────────────────────────────────────────────────────────────

@app.template_filter('from_json')
def from_json_filter(s):
    try:
        return json.loads(s)
    except:
        return []

import re as _re
@app.template_global()
def today_str_global():
    return today_str()

app.jinja_env.globals['today_str'] = today_str

if __name__ == '__main__':
    app.run(debug=True, port=5000)
