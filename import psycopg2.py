import psycopg2
import bcrypt
import json

# Carica config
try:
    with open('config_postgres.json', 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print("File config_postgres.json non trovato!")
    exit()

conn = psycopg2.connect(**config)
cur = conn.cursor()

# --- DATI DA INSERIRE ---
username = "admin"
password_in_chiaro = "Occhiali28!"  # <--- CAMBIALA CON UNA PASSWORD FORTE!
# ------------------------

# Genera Hash
password_hash = bcrypt.hashpw(password_in_chiaro.encode('utf-8'), bcrypt.gensalt())

try:
    # Query aggiornata con T_AUTENTICAZIONE
    sql = "INSERT INTO T_AUTENTICAZIONE (USERNAME, PASSWORD_HASH, RUOLO) VALUES (%s, %s, %s)"
    cur.execute(sql, (username, password_hash.decode('utf-8'), 'admin'))
    
    conn.commit()
    print(f"Utente '{username}' creato con successo in T_AUTENTICAZIONE!")
except Exception as e:
    print(f"Errore inserimento: {e}")

conn.close()