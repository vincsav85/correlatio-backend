"""
app.py — Backend licenze Correlatio

API Flask per la gestione delle licenze software.
Deployato su Railway.

Endpoints:
    POST /activate          → Attiva una nuova licenza
    POST /validate           → Valida una licenza esistente
    POST /trial               → Crea un trial di 30 giorni
    GET  /health             → Health check
    GET  /admin/licenses    → Lista tutte le licenze (protetto da ADMIN_KEY)
    POST /sync                → Riceve i dati dal Desktop per l'app mobile
    GET  /mobile/dashboard  → Dati dashboard per l'app mobile
    GET  /mobile/fatture     → Dati fatture per l'app mobile
    GET  /mobile/scadenze   → Dati scadenze per l'app mobile
    POST /mobile/scadenze   → Inserisce una nuova scadenza manuale da mobile
    PATCH /mobile/scadenze/<id> → Segna una scadenza manuale come pagata da mobile
    GET  /mobile/magazzino  → Dati magazzino per l'app mobile
"""

import os
import uuid
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
import sendgrid
from sendgrid.helpers.mail import Mail

app = Flask(__name__)

# Permette alla PWA (app.correlatio.it) di chiamare gli endpoint /mobile/*
# da un dominio diverso da quello del backend (web-production-...railway.app).
# Limitato solo a questi endpoint, non a tutto il backend.
CORS(app, resources={r"/mobile/*": {"origins": "*"}})

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "licenses.db")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "cambia-questa-chiave-in-produzione")
SECRET_KEY = os.environ.get("SECRET_KEY", "correlatio-secret-2026")

PIANI = {
    "trial": {"nome": "Trial 30 giorni", "giorni": 30},
    "starter": {"nome": "Starter", "giorni": 365},
    "pro": {"nome": "Pro", "giorni": 365},
    "enterprise": {"nome": "Enterprise", "giorni": 365},
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave TEXT UNIQUE NOT NULL,
            piano TEXT NOT NULL DEFAULT 'trial',
            email TEXT,
            nome_azienda TEXT,
            data_attivazione TEXT NOT NULL,
            data_scadenza TEXT NOT NULL,
            attiva INTEGER DEFAULT 1,
            trial INTEGER DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS validazioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave TEXT NOT NULL,
            ip TEXT,
            esito TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    # TABELLA — sincronizzazione dati per l'app mobile.
    # Una riga per ogni combinazione (chiave licenza + azienda), perché
    # una licenza Enterprise può gestire più aziende: ognuna ha i suoi
    # dati separati, mai mischiati tra loro.
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_dati (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave TEXT NOT NULL,
            azienda TEXT NOT NULL,
            dashboard TEXT,
            fatture TEXT,
            scadenze TEXT,
            magazzino TEXT,
            ultima_sync TEXT NOT NULL,
            UNIQUE(chiave, azienda)
        )
    """)

    # NUOVA TABELLA — coda delle modifiche fatte da mobile (scadenze manuali),
    # in attesa che il Desktop le scarichi e le applichi al DB locale.
    c.execute("""
        CREATE TABLE IF NOT EXISTS mobile_pending_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chiave TEXT NOT NULL,
            azienda TEXT NOT NULL,
            tipo TEXT NOT NULL,
            payload TEXT NOT NULL,
            creato_il TEXT NOT NULL,
            sincronizzato INTEGER DEFAULT 0
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


def _licenza_valida_e_attiva(chiave: str) -> bool:
    """
    NUOVA FUNZIONE — verifica rapida per gli endpoint mobile:
    la chiave esiste, è attiva e non è scaduta.
    """
    if not chiave:
        return False
    conn = get_db()
    lic = conn.execute(
        "SELECT * FROM licenze WHERE chiave=?", (chiave,)
    ).fetchone()
    conn.close()
    if not lic or not lic["attiva"]:
        return False
    try:
        scadenza = datetime.fromisoformat(lic["data_scadenza"])
    except ValueError:
        return False
    return scadenza >= datetime.now()


def richiede_licenza_valida(f):
    """
    NUOVA FUNZIONE — decorator per gli endpoint /mobile/*.
    La chiave licenza va passata nell'header X-License-Key.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        chiave = request.headers.get("X-License-Key", "").strip().upper()
        if not _licenza_valida_e_attiva(chiave):
            return jsonify({"errore": "Chiave licenza non valida o non attiva"}), 401
        request.chiave_licenza = chiave
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
    oggi = datetime.now()
    scad = oggi + timedelta(days=30)

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
        "successo": True,
        "chiave": chiave,
        "piano": "trial",
        "nome_piano": "Trial 30 giorni",
        "scadenza": scad.date().isoformat(),
        "giorni": 30,
        "funzionalita": FUNZIONALITA["trial"],
        "messaggio": "Trial attivato! Hai 30 giorni per esplorare Correlatio."
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
    dati = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()
    ip = request.remote_addr

    if not chiave:
        return jsonify({"errore": "Chiave obbligatoria"}), 400

    conn = get_db()
    lic = conn.execute(
        "SELECT * FROM licenze WHERE chiave=?", (chiave,)
    ).fetchone()
    conn.close()

    if not lic:
        log_validazione(chiave, ip, "non_trovata")
        return jsonify({
            "valida": False,
            "errore": "Chiave non trovata",
            "messaggio": "Chiave licenza non valida."
        }), 404

    if not lic["attiva"]:
        log_validazione(chiave, ip, "disattivata")
        return jsonify({
            "valida": False,
            "errore": "Licenza disattivata",
            "messaggio": "Questa licenza è stata disattivata."
        }), 403

    scadenza = datetime.fromisoformat(lic["data_scadenza"])
    oggi = datetime.now()
    giorni_rimasti = (scadenza - oggi).days

    if giorni_rimasti < 0:
        log_validazione(chiave, ip, "scaduta")
        return jsonify({
            "valida": False,
            "errore": "Licenza scaduta",
            "messaggio": f"La licenza è scaduta il {lic['data_scadenza']}.",
            "scadenza": lic["data_scadenza"],
            "piano": lic["piano"],
            "trial": bool(lic["trial"]),
        }), 403

    piano = lic["piano"]
    log_validazione(chiave, ip, "valida")

    return jsonify({
        "valida": True,
        "piano": piano,
        "nome_piano": PIANI.get(piano, {}).get("nome", piano),
        "scadenza": lic["data_scadenza"],
        "giorni": max(0, giorni_rimasti),
        "trial": bool(lic["trial"]),
        "email": lic["email"],
        "nome_azienda": lic["nome_azienda"],
        "funzionalita": FUNZIONALITA.get(piano, []),
        "messaggio": "Licenza valida."
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
        chiave   : Nuova chiave licenza
        piano    : Piano attivato
        scadenza : Data scadenza (1 anno)
    """
    dati = request.get_json() or {}
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
    oggi = datetime.now()
    scad = oggi + timedelta(days=365)

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
        "successo": True,
        "chiave": chiave,
        "piano": piano,
        "nome_piano": PIANI[piano]["nome"],
        "email": email,
        "scadenza": scad.date().isoformat(),
        "giorni": 365,
        "funzionalita": FUNZIONALITA[piano],
        "messaggio": f"Licenza {PIANI[piano]['nome']} attivata con successo."
    }), 201


@app.route("/admin/licenses", methods=["GET"])
@richiede_admin
def lista_licenze():
    """Lista tutte le licenze — solo admin."""
    conn = get_db()
    lics = conn.execute(
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
    dati = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()

    if not chiave:
        return jsonify({"errore": "Chiave obbligatoria"}), 400

    conn = get_db()
    conn.execute("UPDATE licenze SET attiva=0 WHERE chiave=?", (chiave,))
    conn.commit()
    conn.close()

    return jsonify({"successo": True, "messaggio": f"Licenza {chiave} revocata."})


# =============================================================================
# NUOVI ENDPOINT — Sincronizzazione mobile (Fase 2)
# =============================================================================

@app.route("/sync", methods=["POST"])
def sync_dati_mobile():
    """
    Riceve dal Desktop il pacchetto calcolato da sync_mobile.py e lo salva,
    sovrascrivendo il record precedente per la stessa combinazione di
    chiave licenza + azienda (una licenza può gestire più aziende).

    Body JSON:
        chiave    : chiave licenza (obbligatoria)
        azienda   : nome dell'azienda sincronizzata (obbligatorio)
        dashboard : dict
        fatture   : dict
        scadenze  : dict
        magazzino : dict
    """
    dati = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()
    azienda = dati.get("azienda", "").strip()

    if not _licenza_valida_e_attiva(chiave):
        return jsonify({"errore": "Chiave licenza non valida o non attiva"}), 401

    if not azienda:
        return jsonify({"errore": "Nome azienda obbligatorio"}), 400

    ora = datetime.now().isoformat()

    conn = get_db()
    conn.execute("""
        INSERT INTO sync_dati (chiave, azienda, dashboard, fatture, scadenze, magazzino, ultima_sync)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(chiave, azienda) DO UPDATE SET
            dashboard=excluded.dashboard,
            fatture=excluded.fatture,
            scadenze=excluded.scadenze,
            magazzino=excluded.magazzino,
            ultima_sync=excluded.ultima_sync
    """, (
        chiave,
        azienda,
        json.dumps(dati.get("dashboard", {})),
        json.dumps(dati.get("fatture", {})),
        json.dumps(dati.get("scadenze", {})),
        json.dumps(dati.get("magazzino", {})),
        ora,
    ))
    conn.commit()
    conn.close()

    return jsonify({"successo": True, "azienda": azienda, "ultima_sync": ora}), 200


@app.route("/mobile/aziende", methods=["GET"])
@richiede_licenza_valida
def mobile_aziende():
    """
    Restituisce l'elenco delle aziende sincronizzate disponibili per
    questa licenza — serve all'app mobile per mostrare il selettore
    azienda prima di entrare nelle viste vere e proprie.
    """
    conn = get_db()
    righe = conn.execute("""
        SELECT azienda, ultima_sync FROM sync_dati
        WHERE chiave = ?
        ORDER BY azienda ASC
    """, (request.chiave_licenza,)).fetchall()
    conn.close()

    aziende = [{"nome": r["azienda"], "ultima_sync": r["ultima_sync"]} for r in righe]
    return jsonify({"aziende": aziende})


def _leggi_sezione_sync(chiave: str, azienda: str, colonna: str):
    """
    Legge una singola sezione (dashboard/fatture/scadenze/magazzino) per
    la combinazione chiave + azienda data.
    Restituisce (dati, ultima_sync) oppure (None, None) se non è mai
    stata fatta una sync per questa combinazione.
    """
    conn = get_db()
    riga = conn.execute(
        f"SELECT {colonna}, ultima_sync FROM sync_dati WHERE chiave=? AND azienda=?",
        (chiave, azienda)
    ).fetchone()
    conn.close()

    if not riga:
        return None, None

    valore = json.loads(riga[colonna]) if riga[colonna] else {}
    return valore, riga["ultima_sync"]


def _azienda_richiesta():
    """Legge il nome azienda dalla query string (?azienda=...), obbligatorio."""
    azienda = request.args.get("azienda", "").strip()
    return azienda


@app.route("/mobile/dashboard", methods=["GET"])
@richiede_licenza_valida
def mobile_dashboard():
    """Restituisce l'ultimo riepilogo dashboard sincronizzato per l'azienda scelta."""
    azienda = _azienda_richiesta()
    if not azienda:
        return jsonify({"errore": "Parametro azienda obbligatorio"}), 400
    dati, ultima_sync = _leggi_sezione_sync(request.chiave_licenza, azienda, "dashboard")
    if dati is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404
    return jsonify({**dati, "azienda": azienda, "ultima_sync": ultima_sync})


@app.route("/mobile/fatture", methods=["GET"])
@richiede_licenza_valida
def mobile_fatture():
    """Restituisce le fatture attive e passive sincronizzate per l'azienda scelta."""
    azienda = _azienda_richiesta()
    if not azienda:
        return jsonify({"errore": "Parametro azienda obbligatorio"}), 400
    dati, ultima_sync = _leggi_sezione_sync(request.chiave_licenza, azienda, "fatture")
    if dati is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404
    return jsonify({**dati, "azienda": azienda, "ultima_sync": ultima_sync})


@app.route("/mobile/scadenze", methods=["GET"])
@richiede_licenza_valida
def mobile_scadenze():
    """Restituisce le scadenze urgenti e prossime sincronizzate per l'azienda scelta."""
    azienda = _azienda_richiesta()
    if not azienda:
        return jsonify({"errore": "Parametro azienda obbligatorio"}), 400
    dati, ultima_sync = _leggi_sezione_sync(request.chiave_licenza, azienda, "scadenze")
    if dati is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404
    return jsonify({**dati, "azienda": azienda, "ultima_sync": ultima_sync})


@app.route("/mobile/magazzino", methods=["GET"])
@richiede_licenza_valida
def mobile_magazzino():
    """Restituisce la situazione magazzino informativa sincronizzata per l'azienda scelta."""
    azienda = _azienda_richiesta()
    if not azienda:
        return jsonify({"errore": "Parametro azienda obbligatorio"}), 400
    dati, ultima_sync = _leggi_sezione_sync(request.chiave_licenza, azienda, "magazzino")
    if dati is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404
    return jsonify({**dati, "azienda": azienda, "ultima_sync": ultima_sync})


# =============================================================================
# NUOVI ENDPOINT — Scrittura scadenze manuali da mobile (Fase 3)
# =============================================================================
# Solo le scadenze manuali sono scrivibili da mobile. Le scadenze generate
# da fatture (fatture_attive/fatture_passive) restano di sola lettura: per
# segnarle pagate bisogna cambiare lo stato della fattura, cosa che tocca
# IVA/SDI e va fatta dal desktop con tutto il contesto.

def _aggiorna_blob_scadenze(chiave, azienda, modifica):
    """Legge il blob scadenze attuale, applica `modifica` (funzione), lo risalva."""
    dati, _ = _leggi_sezione_sync(chiave, azienda, "scadenze")
    if dati is None:
        return None
    dati = modifica(dati)
    conn = get_db()
    conn.execute(
        "UPDATE sync_dati SET scadenze=? WHERE chiave=? AND azienda=?",
        (json.dumps(dati), chiave, azienda)
    )
    conn.commit()
    conn.close()
    return dati


@app.route("/mobile/scadenze", methods=["POST"])
@richiede_licenza_valida
def crea_scadenza_manuale_da_mobile():
    """Inserisce una nuova scadenza manuale (solo questo tipo è scrivibile da mobile)."""
    dati = request.get_json() or {}
    azienda = dati.get("azienda", "").strip()
    descrizione = dati.get("descrizione", "").strip()
    importo = dati.get("importo")
    data_scadenza = dati.get("data_scadenza", "").strip()

    if not azienda or not descrizione or importo is None or not data_scadenza:
        return jsonify({"errore": "azienda, descrizione, importo e data_scadenza sono obbligatori"}), 400

    mobile_uuid = str(uuid.uuid4())
    voce = {"descrizione": descrizione, "data": data_scadenza, "importo": round(float(importo), 2),
            "origine": "manuale", "id": mobile_uuid}

    soglia_urgenza = (datetime.now() + timedelta(days=7)).date().isoformat()

    def aggiungi(blob):
        lista = "urgenti" if data_scadenza <= soglia_urgenza else "prossime"
        blob.setdefault(lista, []).append(voce)
        blob[lista].sort(key=lambda x: x["data"])
        return blob

    aggiornato = _aggiorna_blob_scadenze(request.chiave_licenza, azienda, aggiungi)
    if aggiornato is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404

    conn = get_db()
    conn.execute(
        "INSERT INTO mobile_pending_changes (chiave, azienda, tipo, payload, creato_il) VALUES (?,?,?,?,?)",
        (request.chiave_licenza, azienda, "nuova_scadenza_manuale",
         json.dumps({"mobile_uuid": mobile_uuid, "descrizione": descrizione,
                     "importo": round(float(importo), 2), "data_scadenza": data_scadenza}),
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    return jsonify({"successo": True, "id": mobile_uuid}), 201


@app.route("/mobile/scadenze/<identificativo>", methods=["PATCH"])
@richiede_licenza_valida
def segna_scadenza_manuale_pagata(identificativo):
    """Segna come pagata una scadenza manuale (rimossa dal blob, come fa il desktop)."""
    dati = request.get_json() or {}
    azienda = dati.get("azienda", "").strip()
    data_pagamento = dati.get("data_pagamento") or datetime.now().date().isoformat()

    if not azienda:
        return jsonify({"errore": "azienda obbligatoria"}), 400

    trovata = {"ok": False}

    def rimuovi(blob):
        for lista in ("urgenti", "prossime"):
            originale = blob.get(lista, [])
            filtrata = [v for v in originale if v.get("id") != identificativo]
            if len(filtrata) != len(originale):
                trovata["ok"] = True
            blob[lista] = filtrata
        return blob

    aggiornato = _aggiorna_blob_scadenze(request.chiave_licenza, azienda, rimuovi)
    if aggiornato is None:
        return jsonify({"errore": "Nessuna sincronizzazione disponibile ancora per questa azienda"}), 404
    if not trovata["ok"]:
        return jsonify({"errore": "Scadenza non trovata (forse non è una scadenza manuale?)"}), 404

    conn = get_db()
    conn.execute(
        "INSERT INTO mobile_pending_changes (chiave, azienda, tipo, payload, creato_il) VALUES (?,?,?,?,?)",
        (request.chiave_licenza, azienda, "scadenza_manuale_pagata",
         json.dumps({"identificativo": identificativo, "data_pagamento": data_pagamento}),
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    return jsonify({"successo": True}), 200


@app.route("/sync/pending-changes", methods=["GET"])
def pending_changes():
    """Il Desktop chiama questo per scaricare le modifiche fatte da mobile."""
    chiave = request.args.get("chiave", "").strip().upper()
    azienda = request.args.get("azienda", "").strip()

    if not _licenza_valida_e_attiva(chiave):
        return jsonify({"errore": "Chiave licenza non valida o non attiva"}), 401
    if not azienda:
        return jsonify({"errore": "Parametro azienda obbligatorio"}), 400

    conn = get_db()
    righe = conn.execute(
        """SELECT id, tipo, payload, creato_il FROM mobile_pending_changes
           WHERE chiave = ? AND azienda = ? AND sincronizzato = 0
           ORDER BY id""",
        (chiave, azienda)
    ).fetchall()
    conn.close()

    return jsonify([
        {"id": r["id"], "tipo": r["tipo"], "payload": json.loads(r["payload"]), "creato_il": r["creato_il"]}
        for r in righe
    ])


@app.route("/sync/ack", methods=["POST"])
def ack_changes():
    """Il Desktop chiama questo dopo aver applicato le modifiche, per non riceverle due volte."""
    dati = request.get_json() or {}
    chiave = dati.get("chiave", "").strip().upper()
    ids = dati.get("ids", [])

    if not _licenza_valida_e_attiva(chiave):
        return jsonify({"errore": "Chiave licenza non valida o non attiva"}), 401
    if not ids:
        return jsonify({"successo": True, "aggiornati": 0})

    conn = get_db()
    conn.executemany(
        "UPDATE mobile_pending_changes SET sincronizzato = 1 WHERE id = ? AND chiave = ?",
        [(i, chiave) for i in ids]
    )
    conn.commit()
    conn.close()
    return jsonify({"successo": True, "aggiornati": len(ids)})


# ---------------------------------------------------------------------------
# Avvio
# ---------------------------------------------------------------------------

# Inizializza DB all'avvio (funziona anche con Gunicorn)
init_db()

import hmac

LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")


@app.route("/webhook/lemonsqueezy", methods=["POST"])
def webhook_lemonsqueezy():
    """
    Riceve eventi da LemonSqueezy e genera licenze automaticamente.
    Eventi gestiti: order_created, subscription_created
    """
    # Verifica firma
    signature = request.headers.get("X-Signature", "")
    body = request.get_data()
    expected = hmac.new(
        LEMONSQUEEZY_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"errore": "Firma non valida"}), 401

    evento = request.json
    tipo = evento.get("meta", {}).get("event_name", "")

    if tipo in ("subscription_cancelled", "subscription_expired",
                "subscription_payment_failed", "subscription_payment_recovered"):
        dati = evento.get("data", {}).get("attributes", {})
        email = dati.get("user_email", "") or dati.get("customer_email", "")

        if email:
            conn = get_db()
            if tipo in ("subscription_cancelled", "subscription_expired",
                        "subscription_payment_failed"):
                conn.execute(
                    "UPDATE licenze SET attiva=0 WHERE email=? AND trial=0 AND attiva=1",
                    (email,))
                print(f"Licenza disattivata per {email} — evento: {tipo}")
            elif tipo == "subscription_payment_recovered":
                # Pagamento recuperato — riattiva la licenza
                conn.execute(
                    "UPDATE licenze SET attiva=1 WHERE email=? AND trial=0",
                    (email,))
                print(f"Licenza riattivata per {email} — pagamento recuperato")
            conn.commit()
            conn.close()

    if tipo in ("order_created", "subscription_created"):
        dati = evento.get("data", {}).get("attributes", {})
        email = dati.get("user_email", "")
        prodotto = dati.get("product_name", "").lower()

        # Determina il piano
        if "enterprise" in prodotto:
            piano = "enterprise"
        elif "pro" in prodotto:
            piano = "pro"
        else:
            piano = "starter"

        if email:
            chiave = genera_chiave(email, piano)
            oggi = datetime.now()
            scad = oggi + timedelta(days=365)

            conn = get_db()
            try:
                # Disattiva licenze precedenti
                conn.execute(
                    "UPDATE licenze SET attiva=0 WHERE email=? AND attiva=1", (email,))
                conn.execute("""
                    INSERT INTO licenze
                    (chiave, piano, email, data_attivazione, data_scadenza, attiva, trial, created_at)
                    VALUES (?,?,?,?,?,1,0,?)
                """, (chiave, piano, email,
                      oggi.date().isoformat(),
                      scad.date().isoformat(),
                      oggi.isoformat()))
                conn.commit()
            finally:
                conn.close()

            # Invia email con la chiave al cliente
            _invia_email_licenza(email, chiave, piano)

    return jsonify({"status": "ok"}), 200


def _invia_email_licenza(email: str, chiave: str, piano: str):
    """Invia la chiave licenza via email al cliente tramite SendGrid."""
    try:
        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get("SENDGRID_API_KEY"))

        nomi_piani = {
            "starter": "Starter — €29/mese",
            "pro": "Pro — €79/mese",
            "enterprise": "Enterprise — €129/mese",
        }
        nome_piano = nomi_piani.get(piano, piano.capitalize())

        contenuto = f"""
Benvenuto in Correlatio!

Il tuo acquisto è confermato. Ecco la tua chiave licenza:

{chiave}

Piano attivato: {nome_piano}
Durata: 12 mesi

Come usare la chiave:
1. Apri Correlatio
2. Se è la tua prima installazione, inserisci la tua email per il trial
3. Clicca "Ho già una chiave licenza"
4. Inserisci la chiave qui sopra

Per assistenza: info@correlatio.it

Grazie per aver scelto Correlatio.
Dove le esigenze incontrano le soluzioni.
""".strip()

        messaggio = Mail(
            from_email="noreply@correlatio.it",
            to_emails=email,
            subject="La tua licenza Correlatio",
            plain_text_content=contenuto
        )
        sg.send(messaggio)
        print(f"Email inviata a {email}")
    except Exception as e:
        print(f"Errore invio email: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


# ---------------------------------------------------------------------------
# Webhook Stripe
# ---------------------------------------------------------------------------

STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')


@app.route('/webhook', methods=['POST'])
def webhook_stripe():
    """Riceve eventi da Stripe e gestisce licenze automaticamente."""
    try:
        import stripe
        payload = request.get_data()
        sig_header = request.headers.get('Stripe-Signature', '')
        evento = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print(f'Webhook Stripe errore firma: {e}')
        return jsonify({'errore': 'Firma non valida'}), 400

    tipo = evento.get('type', '')
    print(f'Stripe evento: {tipo}')

    if tipo == 'checkout.session.completed':
        session = evento['data']['object']
        email = (session.get('customer_details') or {}).get('email', '') or session.get('customer_email', '')
        metadata = session.get('metadata', {})
        piano = metadata.get('piano', 'starter')

        if email:
            chiave = genera_chiave(email, piano)
            oggi = datetime.now()
            scad = oggi + timedelta(days=365)

            conn = get_db()
            try:
                conn.execute('UPDATE licenze SET attiva=0 WHERE email=? AND attiva=1', (email,))
                conn.execute(
                    'INSERT INTO licenze (chiave, piano, email, data_attivazione, data_scadenza, attiva, trial, created_at) VALUES (?,?,?,?,?,1,0,?)',
                    (chiave, piano, email, oggi.date().isoformat(), scad.date().isoformat(), oggi.isoformat())
                )
                conn.commit()
            finally:
                conn.close()

            _invia_email_licenza(email, chiave, piano)
            print(f'Licenza {piano} creata per {email}: {chiave}')

    elif tipo == 'customer.subscription.deleted':
        sub = evento['data']['object']
        email = (sub.get('metadata') or {}).get('email', '')

        if not email:
            try:
                import stripe as _stripe
                _stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
                customer = _stripe.Customer.retrieve(sub.get('customer', ''))
                email = customer.get('email', '')
            except Exception:
                pass

        if email:
            conn = get_db()
            conn.execute('UPDATE licenze SET attiva=0 WHERE email=? AND trial=0 AND attiva=1', (email,))
            conn.commit()
            conn.close()
            print(f'Licenza disattivata per {email}')

    elif tipo == 'invoice.payment_failed':
        invoice = evento['data']['object']
        email = invoice.get('customer_email', '')

        if email:
            conn = get_db()
            conn.execute('UPDATE licenze SET attiva=0 WHERE email=? AND trial=0 AND attiva=1', (email,))
            conn.commit()
            conn.close()
            print(f'Licenza disattivata per {email} — pagamento fallito')

    elif tipo == 'invoice.payment_succeeded':
        invoice = evento['data']['object']
        email = invoice.get('customer_email', '')

        if email and invoice.get('billing_reason') == 'subscription_cycle':
            conn = get_db()
            conn.execute('UPDATE licenze SET attiva=1 WHERE email=? AND trial=0', (email,))
            conn.commit()
            conn.close()
            print(f'Licenza riattivata per {email} — rinnovo')

    return jsonify({'status': 'ok'}), 200
