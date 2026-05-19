"""
app.py — Backend licenze Correlatio

API Flask per la gestione delle licenze software.
Deployato su Railway.

Endpoints:
    POST /activate      → Attiva una nuova licenza
    POST /validate      → Valida una licenza esistente
    POST /trial         → Crea un trial di 30 giorni
    GET  /health        → Health check
    GET  /admin/licenses → Lista tutte le licenze (protetto da ADMIN_KEY)
"""

import os
import uuid
import hashlib
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

DB_PATH    = os.environ.get("DB_PATH", "licenses.db")
ADMIN_KEY  = os.environ.get("ADMIN_KEY", "cambia-questa-chiave-in-produzione")
SECRET_KEY = os.environ.get("SECRET_KEY", "correlatio-secret-2026")

PIANI = {
    "trial":      {"nome": "Trial 30 giorni", "giorni": 30},
    "starter":    {"nome": "Starter",         "giorni": 365},
    "pro":        {"nome": "Pro",             "giorni": 365},
    "enterprise": {"nome": "Enterprise",      "giorni": 365},
}

# Funzionalità per piano
FUNZIONALITA = {
    "trial": [
        "dashboard", "fatture_attive", "fatture_passive", "sdi",
        "scadenzario", "prima_nota", "magazzino", "liquidazione_iva",
        "riconciliazione", "multi_azienda"
    ],
    "starter": [
        "dashboard", "fatture_attive", "fatture_passive", "sdi",
        "scadenzario", "prima_nota"
    ],
    "pro": [
        "dashboard", "fatture_attive", "fatture_passive", "sdi",
        "scadenzario", "prima_nota", "magazzino", "liquidazione_iva",
        "riconciliazione", "multi_azienda"
    ],
    "enterprise": [
        "dashboard", "fatture_attive", "fatture_passive", "sdi",
        "scadenzario", "prima_nota", "magazzino", "liquidazione_iva",
        "riconciliazione", "multi_azienda", "utenti_illimitati",
        "supporto_prioritario"
    ],
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS licenze (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave          TEXT UNIQUE NOT NULL,
            piano           TEXT NOT NULL DEFAULT 'trial',
            email           TEXT,
            nome_azienda    TEXT,
            data_attivazione TEXT NOT NULL,
            data_scadenza   TEXT NOT NULL,
            attiva          INTEGER DEFAULT 1,
            trial           INTEGER DEFAULT 0,
            note            TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS validazioni (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave      TEXT NOT NULL,
            ip          TEXT,
            esito       TEXT,
            timestamp   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("Database inizializzato.")

# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def genera_chiave(email: str, piano: str) -> str:
    """Genera una chiave licenza univoca."""
    raw = f"{email}-{piano}-{uuid.uuid4()}-{SECRET_KEY}"
    hash_val = hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    # Formato: CORR-XXXX-XXXX-XXXX
    return f"CORR-{hash_val[:4]}-{hash_val[4:8]}-{hash_val[8:12]}"

def log_validazione(chiave: str, ip: str, esito: str):
    """Registra ogni tentativo di validazione."""
    conn = get_db()
    conn.execute(
        "INSERT INTO validazioni (chiave, ip, esito, timestamp) VALUES (?,?,?,?)",
        (chiave, ip, esito, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def richiede_admin(f):
    """Decorator per proteggere gli endpoint admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        if key != ADMIN_KEY:
            return jsonify({"errore": "Non autorizzato"}), 401
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Health check — usato da Railway per verificare che l'app sia up."""
    return jsonify({
        "status": "ok",
        "servizio": "Correlatio License Server",
        "versione": "1.0.0",
        "timestamp": datetime.now().isoformat()
    })


@app.route("/trial", methods=["POST"])
def crea_trial():
    """
    Crea un trial gratuito di 30 giorni per una nuova installazione.

    Body JSON:
        email        : Email dell'utente (obbligatoria)
        nome_azienda : Nome azienda (opzionale)

    Returns:
        chiave       : Chiave licenza trial
        piano        : "trial"
        scadenza     : Data scadenza (30 giorni da oggi)
        giorni       : Giorni rimanenti
        funzionalita : Lista funzionalità abilitate
    """
    dati = request.get_json() or {}
    email = dati.get("email", "").strip().lower()

    if not email:
        return jsonify({"errore": "Email obbligatoria"}), 400

    conn = get_db()

    # Controlla se esiste già un trial per questa email
    esistente = conn.execute(
        "SELECT * FROM licenze WHERE email=? AND trial=1", (email,)
    ).fetchone()

    if esistente:
        conn.close()
        return jsonify({"errore": "Trial già attivato per questa email"}), 409

    chiave = genera_chiave(email, "trial")
    oggi   = datetime.now()
    scad   = oggi + timedelta(days=30)

    conn.execute("""
        INSERT INTO licenze
        (chiave, piano, email, nome_azienda, data_attivazione, data_scadenza, attiva, trial, created_at)
        VALUES (?,?,?,?,?,?,1,1,?)
    """, (
        chiave, "trial", email,
        dati.get("nome_azienda", ""),
        oggi.date().isoformat(),
        scad.date().isoformat(),
        oggi.isoformat()
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "successo":     True,
        "chiave":       chiave,
        "piano":        "trial",
        "nome_piano":   "Trial 30 giorni",
        "scadenza":     scad.date().isoformat(),
        "giorni":       30,
        "funzionalita": FUNZIONALITA["trial"],
        "messaggio":    "Trial attivato! Hai 30 giorni per esplorare Correlatio."
    }), 201


@app.route("/validate", methods=["POST"])
def valida():
    """
    Valida una chiave licenza.

    Body JSON:
        chiave : Chiave licenza da validare

    Returns:
        valida       : True/False
        piano        : Piano attivo
        giorni       : Giorni rimanenti alla scadenza
        scadenza     : Data scadenza
        funzionalita : Lista funzionalità abilitate
        trial        : True se è un trial
    """
    dati   = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()
    ip     = request.remote_addr

    if not chiave:
        return jsonify({"errore": "Chiave obbligatoria"}), 400

    conn   = get_db()
    lic    = conn.execute(
        "SELECT * FROM licenze WHERE chiave=?", (chiave,)
    ).fetchone()
    conn.close()

    if not lic:
        log_validazione(chiave, ip, "non_trovata")
        return jsonify({
            "valida":   False,
            "errore":   "Chiave non trovata",
            "messaggio": "Chiave licenza non valida."
        }), 404

    if not lic["attiva"]:
        log_validazione(chiave, ip, "disattivata")
        return jsonify({
            "valida":   False,
            "errore":   "Licenza disattivata",
            "messaggio": "Questa licenza è stata disattivata."
        }), 403

    scadenza     = datetime.fromisoformat(lic["data_scadenza"])
    oggi         = datetime.now()
    giorni_rimasti = (scadenza - oggi).days

    if giorni_rimasti < 0:
        log_validazione(chiave, ip, "scaduta")
        return jsonify({
            "valida":    False,
            "errore":    "Licenza scaduta",
            "messaggio": f"La licenza è scaduta il {lic['data_scadenza']}.",
            "scadenza":  lic["data_scadenza"],
            "piano":     lic["piano"],
            "trial":     bool(lic["trial"]),
        }), 403

    piano = lic["piano"]
    log_validazione(chiave, ip, "valida")

    return jsonify({
        "valida":        True,
        "piano":         piano,
        "nome_piano":    PIANI.get(piano, {}).get("nome", piano),
        "scadenza":      lic["data_scadenza"],
        "giorni":        max(0, giorni_rimasti),
        "trial":         bool(lic["trial"]),
        "email":         lic["email"],
        "nome_azienda":  lic["nome_azienda"],
        "funzionalita":  FUNZIONALITA.get(piano, []),
        "messaggio":     "Licenza valida."
    })


@app.route("/activate", methods=["POST"])
def attiva():
    """
    Attiva una nuova licenza a pagamento.

    Body JSON:
        email        : Email cliente
        piano        : starter / pro / enterprise
        nome_azienda : Nome azienda
        admin_key    : Chiave admin (obbligatoria per creare licenze)

    Returns:
        chiave       : Nuova chiave licenza
        piano        : Piano attivato
        scadenza     : Data scadenza (1 anno)
    """
    dati      = request.get_json() or {}
    admin_key = dati.get("admin_key", "")

    if admin_key != ADMIN_KEY:
        return jsonify({"errore": "Non autorizzato"}), 401

    email = dati.get("email", "").strip().lower()
    piano = dati.get("piano", "starter").lower()

    if not email:
        return jsonify({"errore": "Email obbligatoria"}), 400

    if piano not in PIANI:
        return jsonify({"errore": f"Piano non valido. Scegli tra: {', '.join(PIANI.keys())}"}), 400

    if piano == "trial":
        return jsonify({"errore": "Usa /trial per creare un trial"}), 400

    chiave = genera_chiave(email, piano)
    oggi   = datetime.now()
    scad   = oggi + timedelta(days=365)

    conn = get_db()

    # Disattiva eventuali licenze precedenti per questa email
    conn.execute(
        "UPDATE licenze SET attiva=0 WHERE email=? AND attiva=1", (email,)
    )

    conn.execute("""
        INSERT INTO licenze
        (chiave, piano, email, nome_azienda, data_attivazione, data_scadenza, attiva, trial, created_at)
        VALUES (?,?,?,?,?,?,1,0,?)
    """, (
        chiave, piano, email,
        dati.get("nome_azienda", ""),
        oggi.date().isoformat(),
        scad.date().isoformat(),
        oggi.isoformat()
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "successo":    True,
        "chiave":      chiave,
        "piano":       piano,
        "nome_piano":  PIANI[piano]["nome"],
        "email":       email,
        "scadenza":    scad.date().isoformat(),
        "giorni":      365,
        "funzionalita": FUNZIONALITA[piano],
        "messaggio":   f"Licenza {PIANI[piano]['nome']} attivata con successo."
    }), 201


@app.route("/admin/licenses", methods=["GET"])
@richiede_admin
def lista_licenze():
    """Lista tutte le licenze — solo admin."""
    conn  = get_db()
    lics  = conn.execute(
        "SELECT * FROM licenze ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    return jsonify({
        "totale": len(lics),
        "licenze": [dict(l) for l in lics]
    })


@app.route("/admin/revoke", methods=["POST"])
@richiede_admin
def revoca():
    """Revoca una licenza — solo admin."""
    dati   = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()

    if not chiave:
        return jsonify({"errore": "Chiave obbligatoria"}), 400

    conn = get_db()
    conn.execute("UPDATE licenze SET attiva=0 WHERE chiave=?", (chiave,))
    conn.commit()
    conn.close()

    return jsonify({"successo": True, "messaggio": f"Licenza {chiave} revocata."})


# ---------------------------------------------------------------------------
# Avvio
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
