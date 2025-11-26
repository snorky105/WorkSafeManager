import json
import fdb
import psycopg2
import bcrypt
import asyncio
from nicegui import ui, app, run
import os
from docx import Document
from datetime import datetime, date
import tempfile
import shutil
import zipfile
import re
import logging

#-- LOGGING --
logging.basicConfig(
    filename='WorkSafeManager.log',
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger()


# --- LOG DI AVVIO ---
logger.info("WorkSafeManager avviato!")

# --- CONFIGURAZIONE ---
try:
    with open('config_postgres.json', 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    logger.error("ATTENZIONE: File config_postgres.json non trovato. Uso parametri di default.")
    config = {
        'host': 'localhost', 
        'database': 'postgres',  
        'user': 'postgres',
        'password': 'abcd1234', 
        'port': 5432
    }

# --- DB CONNECTION ---
def get_db_connection():
    try:
        return psycopg2.connect(**config)
        
    except psycopg2.Error as e:
        logger.error(f"Errore durante la connessione a PostgreSQL: {e}")
        return None

# -- TEST CONNESSIONE -- 
if __name__ == "__main__":
    conn = get_db_connection()
    if conn:
        logger.info("Connessione a PostgresSQL riuscita")
        conn.close()

# --- HELPERS CALCOLO SESSIONI ---
def get_next_session_number_sync(id_corso, data_svolgimento: date):
    """
    Conta le sessioni (date distinte) per QUESTO specifico corso (id_corso)
    nello stesso mese/anno, precedenti alla data attuale.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        mese = data_svolgimento.month
        anno = data_svolgimento.year
        
        # Conta le date distinte per QUESTO corso in QUESTO mese
        sql = """
    SELECT COUNT(DISTINCT DATA_SVOLGIMENTO) 
    FROM T_ATTESTATI 
    WHERE ID_CORSO_FK = %s 
    AND EXTRACT(MONTH FROM DATA_SVOLGIMENTO) = %s 
    AND EXTRACT(YEAR FROM DATA_SVOLGIMENTO) = %s
    AND DATA_SVOLGIMENTO < %s
"""

# Nota importante: i parametri devono essere passati nell'ordine esatto dei %s
        cur.execute(sql, (id_corso, mese, anno, data_svolgimento))
        row = cur.fetchone()
        count_prev = row[0] if row else 0
        
        return count_prev + 1
        
    except Exception as e:
        logger.info(f"Errore calcolo sessione: {e}")
        return 1 
    finally:
        if conn: conn.close()

# --- REPOSITORY SOGGETTI ---
class UserRepo:
    @staticmethod
    def get_all(search_term='', solo_docenti=False):
        """
        Recupera utenti. Se solo_docenti=True, filtra solo chi ha IS_DOCENTE=1
        """
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Selezioniamo anche IS_DOCENTE
            sql = "SELECT CODICE_FISCALE, COGNOME, NOME, DATA_NASCITA, LUOGO_NASCITA, ID_ENTE_FK, IS_DOCENTE FROM T_SOGGETTI"
            
            conditions = []
            params = []
            
            # Filtro Ricerca Testuale
            if search_term:
                term = search_term.upper()
                conditions.append("(UPPER(COGNOME) ILIKE %s OR UPPER(NOME) ILIKE %s OR UPPER(CODICE_FISCALE) ILIKE %s)")
                params.extend([term, term, term])
            
            # Filtro Docente
            if solo_docenti:
                conditions.append("IS_DOCENTE = 1")
            
            # Assembla la query
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY COGNOME, NOME"
            
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            
            result = []
            for r in rows:
                d_nascita = r[3]
                if isinstance(d_nascita, (date, datetime)):
                    d_nascita = d_nascita.strftime('%Y-%m-%d')
                elif d_nascita is None:
                    d_nascita = ''
                
                result.append({
                    'CODICE_FISCALE': r[0], 
                    'COGNOME': r[1], 
                    'NOME': r[2],
                    'DATA_NASCITA': str(d_nascita), 
                    'LUOGO_NASCITA': r[4], 
                    'ID_ENTE_FK': r[5],
                    'IS_DOCENTE': bool(r[6]) # Convertiamo 0/1 in True/False
                })
            return result
        except Exception as e:
            print(f"Err UserRepo: {e}")
            return []
        finally:
            if conn: conn.close()

    @staticmethod
    def upsert(data, is_new=True):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # 1. FIX DATA NASCITA
            dt_nascita = None
            raw_data = str(data.get('DATA_NASCITA', '')).strip()
            if raw_data:
                try:
                    if '-' in raw_data:
                        dt_nascita = datetime.strptime(raw_data, '%Y-%m-%d').date()
                    elif '/' in raw_data:
                        dt_nascita = datetime.strptime(raw_data, '%d/%m/%Y').date()
                except ValueError: pass
            
            # 2. FIX ID ENTE (QUESTO È QUELLO CHE MANCAVA)
            # Se è vuoto, lo forziamo a None (NULL su DB)
            id_ente_val = str(data.get('ID_ENTE_FK', '')).strip()
            if not id_ente_val: 
                id_ente_val = None 

            # 3. GESTIONE DOCENTE
            is_doc = 1 if data.get('IS_DOCENTE') else 0

            if is_new:
                sql = """
                    INSERT INTO T_SOGGETTI (CODICE_FISCALE, COGNOME, NOME, DATA_NASCITA, LUOGO_NASCITA, ID_ENTE_FK, IS_DOCENTE) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                # Nota qui sotto: uso id_ente_val, NON data['ID_ENTE_FK']
                params = (data['CODICE_FISCALE'], data['COGNOME'], data['NOME'], dt_nascita, data['LUOGO_NASCITA'], id_ente_val, is_doc)
            else:
                sql = """
                    UPDATE T_SOGGETTI 
                    SET COGNOME=%s, NOME=%s, DATA_NASCITA=%s, LUOGO_NASCITA=%s, ID_ENTE_FK=%s, IS_DOCENTE=%s 
                    WHERE CODICE_FISCALE=%s
                """
                # Nota qui sotto: uso id_ente_val, NON data['ID_ENTE_FK']
                params = (data['COGNOME'], data['NOME'], dt_nascita, data['LUOGO_NASCITA'], id_ente_val, is_doc, data['CODICE_FISCALE'])
            
            cur.execute(sql, params)
            conn.commit()
            return True, "Salvataggio completato."
        except fdb.IntegrityError:
            return False, "Errore: Codice Fiscale esistente o dati invalidi."
        except Exception as e:
            return False, f"Errore DB: {str(e)}"
        finally:
            if conn: conn.close()
    
    # Delete rimane uguale...
    @staticmethod
    def delete(codice_fiscale):
        # ... (codice uguale a prima) ...
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM T_SOGGETTI WHERE CODICE_FISCALE = %s", (codice_fiscale,))
            conn.commit()
            return True
        except Exception: return False
        finally:
            if conn: conn.close()

# --- REPOSITORY AUTENTICAZIONE ---
class AuthRepo:
    @staticmethod
    def get_all_users():
        """Recupera tutti gli utenti di sistema (senza password hash)"""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT USERNAME, RUOLO FROM T_AUTENTICAZIONE ORDER BY USERNAME")
            rows = cur.fetchall()
            return [{'USERNAME': r[0], 'RUOLO': r[1]} for r in rows]
        except Exception as e:
            print(f"Err AuthRepo.get_all: {e}")
            return []
        finally:
            if conn: conn.close()

    @staticmethod
    def create_user(username, password_clear, role='user'):
        """Crea un nuovo utente con password criptata"""
        conn = None
        try:
            # 1. Cripta la password
            pwd_hash = bcrypt.hashpw(password_clear.encode('utf-8'), bcrypt.gensalt())
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            sql = "INSERT INTO T_AUTENTICAZIONE (USERNAME, PASSWORD_HASH, RUOLO) VALUES (%s, %s, %s)"
            cur.execute(sql, (username, pwd_hash.decode('utf-8'), role))
            conn.commit()
            return True, "Utente creato con successo."
        except psycopg2.IntegrityError:
            return False, "Errore: Username già esistente."
            logger.info("Errore: Username già esistente")
        except Exception as e:
            return False, f"Errore DB: {str(e)}"
        finally:
            if conn: conn.close()

    @staticmethod
    def delete_user(username):
        """Elimina un utente"""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM T_AUTENTICAZIONE WHERE USERNAME = %s", (username,))
            conn.commit()
            return True
        except Exception: return False
        finally:
            if conn: conn.close()

class CorsoRepo:
    @staticmethod
    def get_all(search_term=''):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # --- AGGIUNTO TEMPLATE_FILE ---
            sql = "SELECT ID_CORSO, NOME_CORSO, ORE_DURATA, CODICE_BREVE, PROGRAMMA, TEMPLATE_FILE FROM T_CORSI"
            params = []
            
            if search_term:
                term = search_term.strip() + '%'
                sql += " WHERE (NOME_CORSO ILIKE %s) OR (CODICE_BREVE ILIKE %s)"
                params = [term, term]
            
            sql += " ORDER BY NOME_CORSO"
            
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            
            return [{
                'ID_CORSO': r[0], 
                'NOME_CORSO': r[1], 
                'ORE_DURATA': r[2], 
                'CODICE_BREVE': r[3],
                'PROGRAMMA': r[4] if r[4] else '',
                'TEMPLATE_FILE': r[5] if r[5] else '' # <--- NUOVO
            } for r in rows]
        except Exception as e:
            print(f"Err CorsoRepo: {e}")
            logger.error(f"Errore critico durante la lettura del CorsoRepo: {e}")
            logger.error("Eccezione completa:", exc_info=True)
            return []
        finally:
            if conn: conn.close()

    @staticmethod
    def get_next_id():
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT MAX(ID_CORSO) FROM T_CORSI")
            row = cur.fetchone()
            return (row[0] + 1) if row and row[0] else 1
        except Exception: return 1
        finally:
            if conn: conn.close()

    @staticmethod
    def upsert(data, is_new=True):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            params = (
                data['NOME_CORSO'], 
                data['ORE_DURATA'], 
                data['CODICE_BREVE'], 
                data['PROGRAMMA'], 
                data['TEMPLATE_FILE'], # <--- NUOVO
                data['ID_CORSO']
            )

            if is_new:
                sql = "INSERT INTO T_CORSI (NOME_CORSO, ORE_DURATA, CODICE_BREVE, PROGRAMMA, TEMPLATE_FILE, ID_CORSO) VALUES (%s, %s, %s, %s, %s, %s)"
            else:
                sql = "UPDATE T_CORSI SET NOME_CORSO=%s, ORE_DURATA=%s, CODICE_BREVE=%s, PROGRAMMA=%s, TEMPLATE_FILE=%s WHERE ID_CORSO=%s"
            
            cur.execute(sql, params)
            conn.commit()
            return True, "Salvataggio completato."
        except Exception as e:
            return False, f"Errore DB: {str(e)}"
        finally:
            if conn: conn.close()

    @staticmethod
    def delete(id_corso):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM T_CORSI WHERE ID_CORSO = %s", (id_corso,))
            conn.commit()
            return True, "Corso eliminato."
        except psycopg2.IntegrityError:
            return False, "Impossibile eliminare: Esistono attestati collegati!"
        except Exception as e:
            return False, f"Err: {e}"
        finally:
            if conn: conn.close()

# --- REPOSITORY ENTI (COMPLETA) ---
class EnteRepo:
    @staticmethod
    def get_all(search_term=''):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            sql = "SELECT ID_ENTE, DESCRIZIONE, P_IVA FROM T_ENTI"
            params = []
            
            if search_term:
                term = search_term.upper()
                sql += " WHERE UPPER(DESCRIZIONE) ILIKE %s OR UPPER(P_IVA) ILIKE %s OR CAST(ID_ENTE AS VARCHAR(50)) ILIKE %s"
                params = [term, term, term]
            
            sql += " ORDER BY DESCRIZIONE"
            
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            
            # Restituisce la lista di dizionari
            return [{'ID_ENTE': r[0], 'DESCRIZIONE': r[1], 'P_IVA': r[2]} for r in rows]
            
        except Exception as e:
            print(f"Errore EnteRepo.get_all: {e}")
            return []
        finally:
            if conn: conn.close()

    @staticmethod
    def get_next_id():
        """Calcola il prossimo ID disponibile (MAX + 1)"""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT MAX(ID_ENTE) FROM T_ENTI")
            row = cur.fetchone()
            max_id = row[0] if row and row[0] is not None else 0
            return max_id + 1
        except Exception as e:
            print(f"Errore calcolo ID Ente: {e}")
            return 1 
        finally:
            if conn: conn.close()

    @staticmethod
    def upsert(data, is_new=True):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            if is_new:
                sql = "INSERT INTO T_ENTI (ID_ENTE, DESCRIZIONE, P_IVA) VALUES (%s, %s, %s)"
                params = (data['ID_ENTE'], data['DESCRIZIONE'], data['P_IVA'])
            else:
                sql = "UPDATE T_ENTI SET DESCRIZIONE=%s, P_IVA=%s WHERE ID_ENTE=%s"
                params = (data['DESCRIZIONE'], data['P_IVA'], data['ID_ENTE'])
            
            cur.execute(sql, params)
            conn.commit()
            return True, "Salvataggio completato."
            
        except Exception as e:
            return False, f"Errore DB: {str(e)}"
        finally:
            if conn: conn.close()

    @staticmethod
    def delete(id_ente):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM T_ENTI WHERE ID_ENTE = %s", (id_ente,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            if conn: conn.close()

    @staticmethod
    def upsert(data, is_new=True):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            if is_new:
                sql = "INSERT INTO T_ENTI (ID_ENTE, DESCRIZIONE, P_IVA) VALUES (%s, %s, %s)"
                params = (data['ID_ENTE'], data['DESCRIZIONE'], data['P_IVA'])
            else:
                sql = "UPDATE T_ENTI SET DESCRIZIONE=%s, P_IVA=%s WHERE ID_ENTE=%s"
                params = (data['DESCRIZIONE'], data['P_IVA'], data['ID_ENTE'])
            cur.execute(sql, params)
            conn.commit()
            return True, "Salvataggio completato."
        except Exception as e:
            return False, f"Errore DB: {str(e)}"
        finally:
            if conn: conn.close()

    @staticmethod
    def delete(id_ente):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM T_ENTI WHERE ID_ENTE = %s", (id_ente,))
            conn.commit()
            return True
        except Exception: return False
        finally:
            if conn: conn.close()

# --- HELPERS RICERCA E DATI ---
def get_user_details_from_db_sync(search_term: str):
    search_term = search_term.strip()
    if not search_term: return []
    
    # Prepariamo la query base
    sql = """
        SELECT s.CODICE_FISCALE, s.COGNOME, s.NOME, s.DATA_NASCITA, s.LUOGO_NASCITA, e.DESCRIZIONE 
        FROM T_SOGGETTI s 
        LEFT JOIN T_ENTI e ON s.ID_ENTE_FK = e.ID_ENTE
        WHERE 
    """
    
    params = []
    conditions = []

    # 1. CERCA LA FRASE INTERA (Risolve il caso "Di Marco")
    # Cerca se l'intera stringa digitata corrisponde all'inizio di un Cognome, Nome o CF
    full_pattern = search_term + '%'
    conditions.append("(s.COGNOME ILIKE %s OR s.NOME ILIKE %s OR s.CODICE_FISCALE ILIKE %s)")
    params.extend([full_pattern, full_pattern, full_pattern])

    # 2. CERCA LE PAROLE SPEZZATE (Risolve il caso "Rossi Mario")
    # Solo se ci sono almeno due parole separate da spazio
    parts = search_term.split()
    if len(parts) >= 2:
        p1 = parts[0] + '%'
        p2 = parts[1] + '%' 
        # Cerca: (Cognome=Parola1 E Nome=Parola2) OPPURE (Cognome=Parola2 E Nome=Parola1)
        conditions.append("((s.COGNOME ILIKE %s AND s.NOME ILIKE %s) OR (s.COGNOME ILIKE %s AND s.NOME ILIKE %s))")
        params.extend([p1, p2, p2, p1])

    # Unisce le due logiche con "OR" (trova o l'uno o l'altro)
    sql += " (" + " OR ".join(conditions) + ")"
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        conn.close()
        return [{
            "CODICE_FISCALE": r[0], "COGNOME": r[1], "NOME": r[2],
            "DATA_NASCITA": r[3], "LUOGO_NASCITA": r[4], "SOCIETA": r[5] if r[5] else ""
        } for r in rows]
    except Exception as e:
        print(f"Errore ricerca: {e}")
        return []

def get_corsi_from_db_sync():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Recuperiamo TUTTI i campi, incluso TEMPLATE_FILE (che è il 6° campo, indice 5)
        cur.execute("SELECT ID_CORSO, NOME_CORSO, ORE_DURATA, CODICE_BREVE, PROGRAMMA, TEMPLATE_FILE FROM T_CORSI ORDER BY NOME_CORSO")
        rows = cur.fetchall()
        conn.close()
        
        return [{
            "id": r[0], 
            "nome": r[1], 
            "ore": r[2], 
            "codice": r[3], 
            "programma": r[4] if r[4] else "",
            "template": r[5] if r[5] else "modello.docx" 
        } for r in rows]
        
    except Exception as e:
        print(f"Err Get Corsi: {e}")
        logger.error(f"Errore critico durante la lettura del CorsoRepo: {e}")
        return []

def save_attestato_to_db_sync(cf, id_corso, data_str):
    try:
        dt = None
        if isinstance(data_str, (date, datetime)):
            dt = data_str
        elif re.search(r'\d{4}-\d{2}-\d{2}', str(data_str)):
             dt = datetime.strptime(re.search(r'\d{4}-\d{2}-\d{2}', str(data_str)).group(0), '%Y-%m-%d').date()
        elif re.search(r'\d{2}/\d{2}/\d{4}', str(data_str)):
             dt = datetime.strptime(re.search(r'\d{2}/\d{2}/\d{4}', str(data_str)).group(0), '%d/%m/%Y').date()
        data_val = dt if dt else date.today()
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO T_ATTESTATI (ID_SOGGETTO_FK, ID_CORSO_FK, DATA_SVOLGIMENTO) VALUES (%s, %s, %s)", (cf, id_corso, data_val))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Errore durante il salvataggio dell'Attestato: {e}", exc_info=True)
        return False

def get_count_attestati_oggi_sync():
    """Conta gli attestati GENERATI oggi (Data Creazione)"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # --- MODIFICA QUI: Usiamo DATA_CREAZIONE invece di DATA_SVOLGIMENTO ---
        cur.execute("SELECT COUNT(*) FROM T_ATTESTATI WHERE DATA_CREAZIONE = CURRENT_DATE")
        
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception as e:
        print(f"Err Count Oggi: {e}")
        return 0
    finally:
        if conn: conn.close()

def generate_certificate_sync(data_map, template_file="modello.docx", output_dir=None):
    if not os.path.exists(template_file): raise FileNotFoundError("Template mancante")
    doc = Document(template_file)
    local_map = data_map.copy()
    
    # --- FIX FORMATO DATA NASCITA ---
    dob = local_map.get("{{DATA_NASCITA}}")
    
    # Caso 1: È un oggetto Data
    if isinstance(dob, (datetime, date)): 
        local_map["{{DATA_NASCITA}}"] = dob.strftime('%d/%m/%Y')
    
    # Caso 2: È una Stringa tipo "1980-05-20"
    elif isinstance(dob, str) and '-' in dob:
        try:
            dt_obj = datetime.strptime(dob.strip(), '%Y-%m-%d')
            local_map["{{DATA_NASCITA}}"] = dt_obj.strftime('%d/%m/%Y')
        except ValueError:
            pass 
    # --------------------------------

    # Helper per sostituzione nel Word
    def replace_in_p(p, m):
        for k, v in m.items():
            if k in p.text: p.text = p.text.replace(k, str(v if v else ''))

    for p in doc.paragraphs: replace_in_p(p, local_map)
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                for p in c.paragraphs: replace_in_p(p, local_map)
                         
    fname = f"{re.sub(r'\W', '', str(local_map.get('{{COGNOME}}','')))}_{re.sub(r'\W', '', str(local_map.get('{{NOME}}','')))}_{re.sub(r'\W', '', str(local_map.get('{{CODICE}}','')))}_{re.sub(r'\W', '', str(local_map.get('{{SOCIETA}}','')))}.docx"
    out_path = os.path.join(output_dir, fname) if output_dir else fname
    doc.save(out_path)
    return out_path

def generate_zip_sync(files, base, name="attestati.zip"):
    with zipfile.ZipFile(name, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files: z.write(f, arcname=os.path.relpath(f, base))
    return name

def check_user_credentials_sync(username, plain_password):
    print(f"--- DEBUG LOGIN: Tento accesso per utente '{username}' ---")
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            print("--- DEBUG: Impossibile connettersi al DB! ---")
            return False
            
        cur = conn.cursor()
        logger.info(" Connessione al DB OK. Cerco utente")
        
        # Cerca l'hash
        cur.execute("SELECT PASSWORD_HASH FROM T_AUTENTICAZIONE WHERE USERNAME = %s", (username,))
        row = cur.fetchone()
        
        if row:
            logger.info("Utente TROVATO nel database")
            stored_hash = row[0]
            
            # Debug dell'hash (stampiamo solo i primi caratteri per sicurezza)
            logger.debug(f"Hash nel DB (primi 10 car): {str(stored_hash)[:10]}...")
        
            # Conversione se necessario
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode('utf-8')
            
            # Verifica
            if bcrypt.checkpw(plain_password.encode('utf-8'), stored_hash):
                logger.info("Accesso consentito")
                return True
            else:
                logger.warning("PASSWORD ERRATA! Il controllo bcrypt ha fallito")
        else:
            logger.info(f"Utente : '{username}' NON TROVATO nella tabella T_AUTENTICAZIONE")
                
        return False
        
    except Exception as e:
        logger.warning(" Errore grave!!")
        return False
    finally:
        if conn: conn.close()       

# --- PAGES ---
@ui.page('/')
def login_page():
    # Se l'utente è già loggato, va alla dashboard
    if app.storage.user.get('authenticated', False):
        ui.navigate.to('/dashboard') 
        return
    
    with ui.column().classes('absolute-center w-full max-w-sm items-center'):
        ui.label("WorkSafeManager").classes("text-3xl font-bold mb-8 text-center text-slate-700")
        
        with ui.card().style("padding: 40px;").classes("w-full shadow-xl"):
            username_input = ui.input("Utente").props("outlined").classes("w-full mb-2")
            password_input = ui.input("Password").props("outlined type=password").classes("w-full mb-4")
            
            # --- PUNTO CRITICO: La funzione deve essere async ---
            async def on_login_click():
                user = username_input.value.strip()
                pwd = password_input.value.strip()
                
                # Controllo base: campi vuoti?
                if not user or not pwd:
                    ui.notify("Inserisci utente e password", color="orange")
                    return

                # --- LA VERA VERIFICA DI SICUREZZA ---
                # Chiama la funzione che controlla nel DB (T_AUTENTICAZIONE)
                # Se questa restituisce False, NON entri.
                is_valid = await asyncio.to_thread(check_user_credentials_sync, user, pwd)

                if is_valid:
                    app.storage.user['authenticated'] = True
                    app.storage.user['username'] = user 
                    ui.notify("Login riuscito!", color="green")
                    logger.info("Login riuscito!")
                    ui.navigate.to('/dashboard')
                else:
                    # Se arriva qui, user o password sono sbagliati
                    ui.notify("Credenziali errate o utente non trovato", color="red")
                    logger.error("Credenziali errate o utente non trovato")
            
            # Gestione tasto Invio (per comodità)
            password_input.on('keydown.enter', on_login_click)
            username_input.on('keydown.enter', on_login_click)
            
            ui.button("Entra", on_click=on_login_click).classes("w-full mt-4 bg-primary text-white")

@ui.page('/gestione_accessi')
def gestione_accessi_page():
    # Sicurezza: Se non sei loggato, via!
    if not app.storage.user.get('authenticated', False): 
        ui.navigate.to('/'); return

    # --- 1. INIZIALIZZAZIONE VARIABILI (A NONE) ---
    # Non creiamo ancora gli input, altrimenti appaiono a caso nella pagina
    username_new = None 
    password_new = None
    role_new = None
    
    table_ref = None
    dialog_ref = None

    # --- 2. LOGICA ---
    async def refresh_table():
        rows = await asyncio.to_thread(AuthRepo.get_all_users)
        if table_ref: 
            table_ref.rows = rows
            table_ref.update()

    async def add_new_user():
        # Recuperiamo i valori dagli input che verranno creati dopo
        u = username_new.value.strip()
        p = password_new.value.strip()
        r = role_new.value
        
        if not u or not p:
            ui.notify("Username e Password obbligatori!", type='warning')
            return

        success, msg = await asyncio.to_thread(AuthRepo.create_user, u, p, r)
        if success:
            ui.notify(msg, type='positive')
            dialog_ref.close()
            await refresh_table()
        else:
            ui.notify(msg, type='negative')

    async def delete_user_click(row):
        if row['USERNAME'] == 'admin':
            ui.notify("Non puoi cancellare l'utente admin principale!", type='warning')
            return
            
        await asyncio.to_thread(AuthRepo.delete_user, row['USERNAME'])
        ui.notify(f"Utente {row['USERNAME']} eliminato.", type='info')
        await refresh_table()

    # --- 3. LAYOUT PAGINA ---
    with ui.column().classes('w-full items-center p-8 max-w-screen-lg mx-auto bg-slate-50 min-h-screen'):
        
        # Header
        with ui.row().classes('w-full items-center mb-6 justify-between'):
            ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/dashboard')).props('flat round dense')
            ui.label('Gestione Accessi al Software').classes('text-3xl font-bold text-slate-800')
            # Il bottone apre il dialogo (che definiremo sotto)
            ui.button('Nuovo Utente', icon='add', on_click=lambda: dialog_ref.open()).props('unelevated color=primary')

        ui.label("Attenzione: questi sono gli utenti che possono fare LOGIN nel programma.").classes("text-sm text-gray-500 mb-4")

        # Tabella
        cols = [
            {'name': 'USERNAME', 'label': 'Username', 'field': 'USERNAME', 'align': 'left', 'sortable': True},
            {'name': 'RUOLO', 'label': 'Ruolo', 'field': 'RUOLO', 'align': 'center'},
            {'name': 'azioni', 'label': 'Azioni', 'field': 'azioni', 'align': 'right'},
        ]
        
        table_ref = ui.table(columns=cols, rows=[], row_key='USERNAME').classes('w-full shadow-md bg-white')
        
        # Slot Azioni
        table_ref.add_slot('body-cell-azioni', r'''
            <q-td key="azioni" :props="props">
                <q-btn icon="delete" size="sm" round flat color="red" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        table_ref.on('delete', lambda e: delete_user_click(e.args))
        
        ui.timer(0.1, refresh_table, once=True)

    # --- 4. DIALOGO (Dove creiamo gli input VERI) ---
    with ui.dialog() as dialog_ref, ui.card().classes('min-w-[400px] p-6'):
        ui.label('Nuovo Utente Sistema').classes('text-xl font-bold mb-4')
        
        # ORA creiamo gli input grafici e li assegniamo alle variabili
        username_new = ui.input('Username').props('outlined').classes('w-full mb-2')
        password_new = ui.input('Password').props('outlined type=password').classes('w-full mb-4')
        role_new = ui.select(options=['admin', 'user', 'segreteria'], label='Ruolo', value='user').props('outlined').classes('w-full mb-6')
        
        with ui.row().classes('w-full justify-end'):
            ui.button('Annulla', on_click=dialog_ref.close).props('flat color=grey')
            ui.button('Crea Utente', on_click=add_new_user).props('unelevated color=primary')

@ui.page('/gestionecorsi')
def gestionecorsi_page():
    # Check Autenticazione
    if not app.storage.user.get('authenticated', False): 
        ui.navigate.to('/')
        return

    # Stato locale della pagina
    state = {'is_new': True, 'search': ''}

    # --- 1. CONFIGURAZIONE E VARIABILI ---
    ABSOLUTE_PATH_TO_TEMPLATES = '/home/ubuntu/app/WorkSafeManager/templates'
    
    # Riferimenti ai componenti UI
    id_input = None
    nome_input = None
    ore_input = None
    codice_input = None
    programma_input = None
    template_select = None 
    
    dialog_ref = None
    dialog_label = None
    table_ref = None

    # --- 2. HELPER FUNCTIONS ---

    def get_template_files():
        """Scansiona la cartella e restituisce la lista dei file .docx"""
        if not os.path.exists(ABSOLUTE_PATH_TO_TEMPLATES):
            try:
                os.makedirs(ABSOLUTE_PATH_TO_TEMPLATES)
            except OSError:
                return []
        return [f for f in os.listdir(ABSOLUTE_PATH_TO_TEMPLATES) if f.endswith('.docx') and not f.startswith('~$')]

    async def handle_template_upload(e):
        """Gestisce l'upload recuperando correttamente il nome file"""
        
        # --- 1. RECUPERO NOME FILE (CORRETTO) ---
        # Accediamo direttamente a e.name. NiceGUI garantisce che esista.
        original_filename = e.name
        
        # Sicurezza extra: se per miracolo fosse vuoto
        if not original_filename:
            original_filename = "documento_senza_nome.docx"
            
        logger.info(f"--- INIZIO UPLOAD: {original_filename} ---")
        
        # Pulisci il nome file da percorsi assoluti (es. C:\fakepath\...) o caratteri strani
        original_filename = os.path.basename(original_filename)
        
        # --- 2. PERCORSO TARGET ---
        target_path = os.path.join(ABSOLUTE_PATH_TO_TEMPLATES, original_filename)
        logger.info(f"Salvataggio in: '{target_path}'")

        # Creazione cartella se non esiste
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
        except Exception as create_err:
            logger.error(f"Errore creazione cartella: {create_err}")
            ui.notify("Errore accesso cartella server", color='red')
            return

        # --- 3. SCRITTURA FILE ---
        try:
            # Recupera il contenuto binario
            content_to_write = e.content
            
            # Reset del puntatore (importante per file riutilizzati)
            if hasattr(content_to_write, 'seek'): 
                content_to_write.seek(0)

            # Lettura dati
            read_result = content_to_write.read()
            # Gestione asincrona se necessario
            if asyncio.iscoroutine(read_result):
                data = await read_result
            else:
                data = read_result
            
            # Scrittura su disco
            with open(target_path, 'wb') as f:
                f.write(data)
            
            # Notifica successo
            ui.notify(f"Caricato: {original_filename}", color='green')
            
            # Aggiorna la select nel dialog se è aperto
            if dialog_ref.value: # Se il dialog è visibile
                 # Aggiorna le opzioni della select "template_select"
                 # Nota: dobbiamo richiamare la logica per ricaricare i file
                 files_disponibili = get_template_files()
                 if template_select:
                     template_select.options = files_disponibili
                     template_select.update()

        except Exception as err:
            ui.notify(f"Errore salvataggio: {err}", color='red')
            logger.error(f"Eccezione durante scrittura: {err}", exc_info=True)

    # --- 3. LOGICA OPERATIVA (CRUD) ---

    async def refresh_table():
        """Ricarica i dati della tabella dal DB"""
        rows = await asyncio.to_thread(CorsoRepo.get_all, state['search'])
        if table_ref: 
            table_ref.rows = rows
            table_ref.update()

    async def open_dialog(row=None):
        """Apre il dialog per Creazione o Modifica"""
        files_disponibili = get_template_files()
        if template_select:
            template_select.options = files_disponibili
            template_select.update()

        dialog_ref.open()
        
        if row:
            state['is_new'] = False
            dialog_label.text = "Modifica Corso"
            id_input.value = row['ID_CORSO']
            nome_input.value = row['NOME_CORSO']
            ore_input.value = row['ORE_DURATA']
            codice_input.value = row['CODICE_BREVE']
            programma_input.value = row['PROGRAMMA']
            
            saved_template = row.get('TEMPLATE_FILE')
            template_select.value = saved_template if saved_template in files_disponibili else None
        else:
            state['is_new'] = True
            dialog_label.text = "Nuovo Corso"
            next_id = await asyncio.to_thread(CorsoRepo.get_next_id)
            id_input.value = next_id
            nome_input.value = ''
            ore_input.value = 8
            codice_input.value = ''
            programma_input.value = ''
            template_select.value = None

    async def save_corso():
        """Salva i dati nel DB"""
        if not nome_input.value: 
            ui.notify('Nome corso obbligatorio!', type='warning')
            return

        data = {
            'ID_CORSO': int(id_input.value),
            'NOME_CORSO': nome_input.value.strip(),
            'ORE_DURATA': float(ore_input.value) if ore_input.value else 0,
            'CODICE_BREVE': codice_input.value.strip() if codice_input.value else '',
            'PROGRAMMA': programma_input.value.strip() if programma_input.value else '',
            'TEMPLATE_FILE': template_select.value
        }
        
        success, msg = await asyncio.to_thread(CorsoRepo.upsert, data, state['is_new'])
        
        if success:
            ui.notify(msg, type='positive')
            dialog_ref.close()
            await refresh_table()
        else:
            ui.notify(msg, type='negative')

    async def delete_corso(row):
        """Elimina il corso"""
        success, msg = await asyncio.to_thread(CorsoRepo.delete, row['ID_CORSO'])
        if success:
            ui.notify(msg, type='positive')
            await refresh_table()
        else:
            ui.notify(msg, type='negative', close_button=True, multi_line=True)

    # --- 4. INTERFACCIA UTENTE (LAYOUT) ---
    
    with ui.column().classes('w-full items-center p-8 max-w-screen-xl mx-auto bg-slate-50 min-h-screen'):
        
        with ui.row().classes('w-full items-center mb-6 justify-between'):
            
            with ui.row().classes('items-center gap-4'):
                ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/dashboard')).props('flat round dense')
                ui.label('Gestione Corsi').classes('text-3xl font-bold text-slate-800')
            
            with ui.row().classes('items-center gap-4'):
                # Bottone Upload
                ui.upload(
                    on_upload=handle_template_upload,
                    label='Carica Modelli (.docx)',
                    auto_upload=True,
                    multiple=True
                ).props('accept=".docx" flat color=blue-7').classes('text-sm')
                
                ui.button('Nuovo Corso', icon='add', on_click=lambda: open_dialog(None)).props('unelevated color=primary')

        with ui.card().classes('w-full p-2 mb-4 flex flex-row items-center gap-4'):
            ui.icon('search').classes('text-grey ml-2')
            ui.input(placeholder='Cerca corso...').classes('flex-grow').props('borderless').bind_value(state, 'search').on('keydown.enter', refresh_table)
            ui.button('Cerca', on_click=refresh_table).props('flat color=primary')

        cols = [
            {'name': 'ID_CORSO', 'label': 'ID', 'field': 'ID_CORSO', 'align': 'left', 'sortable': True},
            {'name': 'NOME_CORSO', 'label': 'Nome', 'field': 'NOME_CORSO', 'align': 'left', 'sortable': True},
            {'name': 'TEMPLATE_FILE', 'label': 'Modello Attestato', 'field': 'TEMPLATE_FILE', 'align': 'left', 'classes': 'text-gray-500 italic'},
            {'name': 'azioni', 'label': '', 'field': 'azioni', 'align': 'right'},
        ]
        
        table_ref = ui.table(columns=cols, rows=[], row_key='ID_CORSO').classes('w-full shadow-md bg-white')
        
        table_ref.add_slot('body-cell-azioni', r'''
            <q-td key="azioni" :props="props">
                <q-btn icon="edit" size="sm" round flat color="grey-8" @click="$parent.$emit('edit', props.row)" />
                <q-btn icon="delete" size="sm" round flat color="red" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        
        table_ref.on('edit', lambda e: open_dialog(e.args))
        table_ref.on('delete', lambda e: delete_corso(e.args))
        
        ui.timer(0.1, refresh_table, once=True)

    # --- 5. DIALOG (MODALE) ---
    with ui.dialog() as dialog_ref, ui.card().classes('w-full max-w-2xl p-6 gap-4'):
        
        dialog_label = ui.label('Corso').classes('text-xl font-bold mb-2')
        
        with ui.row().classes('w-full gap-4'):
            id_input = ui.input('ID').props('outlined dense readonly').classes('w-24')
            nome_input = ui.input('Nome Corso').props('outlined dense').classes('flex-grow')
            
        with ui.row().classes('w-full gap-4'):
            codice_input = ui.input('Codice Breve').props('outlined dense uppercase').classes('w-1/2')
            ore_input = ui.number('Ore').props('outlined dense').classes('w-1/3')

        ui.separator()
        
        # Solo selettore, upload è fuori
        template_select = ui.select(
            options=[], 
            label='Seleziona Modello Word (.docx)'
        ).props('outlined dense options-dense').classes('w-full')
        
        ui.label('Programma Corso').classes('text-sm text-gray-500 mt-2')
        programma_input = ui.textarea('Programma').props('outlined dense rows=10').classes('w-full')

        with ui.row().classes('w-full justify-end mt-4'):
            ui.button('Annulla', on_click=dialog_ref.close).props('flat color=grey')
            ui.button('Salva', on_click=save_corso).props('unelevated color=primary')

@ui.page('/dashboard')
def dashboard_page():
    if not app.storage.user.get('authenticated', False):
        ui.navigate.to('/') 
        return 
    
    username = app.storage.user.get('username', 'Utente')

    # --- 1. LAYOUT PAGINA ---
    with ui.column().classes('w-full items-center p-8'):
        
        # TITOLO
        ui.label(f"Dashboard").classes("text-3xl font-bold mb-8 text-slate-800")
        
        # --- BANNER ---
        with ui.card().classes('w-full max-w-4xl bg-blue-100 border-l-8 border-blue-600 p-6 mb-8 flex flex-row items-center justify-center gap-12 shadow-sm'):
            
            # Parte Sinistra: Icona e Testo
            with ui.row().classes('items-center gap-4'):
                ui.icon('verified', size='lg').classes('text-blue-600')
                with ui.column().classes('gap-0'):
                    ui.label('Attestati emessi oggi').classes('text-lg text-blue-900 font-medium')
                    ui.label('Monitoraggio giornaliero').classes('text-sm text-blue-700 opacity-80')
            
            # Parte Destra: Il Numero (Box Bianco)
            with ui.column().classes('items-center bg-white rounded-lg px-6 py-2 shadow-sm'):
                # --- CORREZIONE: Creiamo l'etichetta QUI, direttamente al suo posto ---
                count_label = ui.label('...').classes('text-2xl font-bold text-blue-800')
                ui.label('Totale').classes('text-xs text-gray-500 uppercase tracking-wider')

        # --- PULSANTI DI NAVIGAZIONE ---
        with ui.row().classes('w-full justify-center gap-6 flex-wrap'):
            
            # Crea Attestati
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/creaattestati')):
                 ui.icon('explore', size='3em').classes('mb-2 text-indigo-600')
                 ui.label('Crea Attestati').classes('text-center text-lg font-bold w-full text-slate-700')
            
            # Gestione Utenti
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/gestioneutenti')):
                 ui.icon('people', size='3em').classes('mb-2 text-emerald-600')
                 ui.label('Gestione Utenti').classes('text-center text-lg font-bold w-full text-slate-700')
            
            # Gestione Enti
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/gestioneenti')):
                 ui.icon('business', size='3em').classes('mb-2 text-amber-600')
                 ui.label('Gestione Enti').classes('text-center text-lg font-bold w-full text-slate-700')
            
            # Gestione Docenti
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/gestionedocenti')):
                 ui.icon('school', size='3em').classes('mb-2 text-rose-600')
                 ui.label('Gestione Docenti').classes('text-center text-lg font-bold w-full text-slate-700')

            # Gestione Corsi
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/gestionecorsi')):
                 ui.icon('menu_book', size='3em').classes('mb-2 text-purple-600')
                 ui.label('Gestione Corsi').classes('text-center text-lg font-bold w-full text-slate-700')
            
            # Gestione Accessi (Login)
            with ui.card().classes('w-64 h-40 flex flex-col items-center justify-center cursor-pointer hover:shadow-xl transition-all hover:scale-105').on('click', lambda: ui.navigate.to('/gestione_accessi')):
                 ui.icon('lock_person', size='3em').classes('mb-2 text-gray-700')
                 ui.label('Gestione Accessi').classes('text-center text-lg font-bold w-full text-slate-700')

        # LOGOUT
        def logout_click():
            app.storage.user['authenticated'] = False
            ui.navigate.to('/')
        
        ui.button("Logout", on_click=logout_click, icon='logout').classes("bg-red-500 text-white mt-12 px-8 py-2 rounded-full shadow-lg hover:bg-red-600")

    # --- 2. LOGICA DI AGGIORNAMENTO (DEFINITA ALLA FINE) ---
    # Ora che count_label esiste nel layout, possiamo aggiornarlo.
    
    async def update_counter():
        n = await asyncio.to_thread(get_count_attestati_oggi_sync)
        count_label.set_text(f"{n}")
    
    # Avvia i timer
    ui.timer(0.1, update_counter, once=True) 
    ui.timer(10.0, update_counter)

@ui.page('/creaattestati')
def creaattestati_page():
    if not app.storage.user.get('authenticated', False):
         ui.navigate.to('/')
         return

    # --- CARICAMENTO DATI ---
    corsi_raw = get_corsi_from_db_sync()
    corsi_opts = {c["id"]: c["nome"] for c in corsi_raw}
    corsi_ore = {c["id"]: c["ore"] for c in corsi_raw}
    corsi_codici = {c["id"]: (c["codice"].strip() if c["codice"] else "GEN") for c in corsi_raw}
    
    corsi_programmi = {c["id"]: c["programma"] for c in corsi_raw}
    corsi_templates = {c["id"]: c["template"] for c in corsi_raw}
    
    # --- NOVITÀ: CARICAMENTO DOCENTI ---
    # Recuperiamo solo i docenti per popolare la select
    docenti_list = UserRepo.get_all(solo_docenti=True)
    docenti_opts = {d['CODICE_FISCALE']: f"{d['COGNOME']} {d['NOME']}" for d in docenti_list}

    soggetti = {} 

    # Dialogo Ricerca
    search_dialog = ui.dialog()
    with search_dialog, ui.card().classes('w-full max-w-lg'):
        ui.label('Cerca Soggetto').classes('text-xl font-bold mb-2')
        with ui.row().classes('w-full gap-2'):
            search_input = ui.input(label='Cerca...').classes('flex-grow').props('outlined')
            search_btn = ui.button('Cerca').props('color=primary')
        search_results_area = ui.column().classes('w-full mt-2')
        ui.button('Chiudi', on_click=search_dialog.close).props('flat color=grey').classes('ml-auto')

    with ui.column().classes('w-full items-center p-8'):
        with ui.row().classes('w-full items-center mb-4'): 
            ui.button('Torna', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')
            ui.label('Generazione Attestati Massiva').classes('text-3xl ml-4')
        
        @ui.refreshable
        def render_lista_soggetti():
            if not soggetti:
                ui.label("Nessun soggetto selezionato.").classes('text-sm italic p-4 text-gray-500')
                return

            grid_style = 'grid-template-columns: 0.9fr 0.9fr 0.8fr 2fr 1.2fr 0.9fr 2fr 0.4fr 0.3fr; width: 100%; gap: 8px; align-items: center;'
            
            # Header
            with ui.grid().style(grid_style + 'font-weight: bold; border-bottom: 2px solid #ccc; padding-bottom: 5px;'):
                ui.label('Cognome'); ui.label('Nome'); ui.label('Codice Fiscale'); ui.label('Corso'); ui.label('Docente'); ui.label('Data Rilascio'); ui.label('Note Date'); ui.label('Ore'); ui.label('')

            for cf, item in soggetti.items():
                u_data = item['user']
                if 'date_extra' not in item: item['date_extra'] = ''
                if 'docente_cf' not in item: item['docente_cf'] = None # Init docente

                with ui.grid().style(grid_style + 'border-bottom: 1px solid #eee; padding: 5px;'):
                    ui.label(u_data['COGNOME']).classes('text-sm truncate')
                    ui.label(u_data['NOME']).classes('text-sm truncate')
                    ui.label(u_data['CODICE_FISCALE']).classes('text-xs truncate')
                    
                    def on_course_change(e, it=item):
                        it['ore'] = corsi_ore.get(e.value)
                    
                    # Select Corso
                    ui.select(options=corsi_opts, on_change=on_course_change).props('outlined dense options-dense').bind_value(item, 'cid').classes('w-full')
                    
                    # --- NUOVO: SELECT DOCENTE ---
                    ui.select(options=docenti_opts).props('outlined dense options-dense').bind_value(item, 'docente_cf').classes('w-full')
                    
                    # Data
                    with ui.input().props('outlined dense').bind_value(item, 'per').classes('w-full') as date_inp:
                        with date_inp.add_slot('append'):
                            ui.icon('event').classes('cursor-pointer text-xs').on('click', lambda: menu.open())
                            with ui.menu() as menu:
                                ui.date().bind_value(item, 'per').on('update:model-value', lambda: menu.close())
                    
                    # Note Extra
                    ui.input(placeholder='Es: 28/06').props('outlined dense').bind_value(item, 'date_extra').classes('w-full')
                    
                    # Ore
                    ui.number().props('outlined dense').bind_value(item, 'ore')
                    
                    # Delete
                    ui.button(icon='delete', on_click=lambda _, c=cf: rimuovi_soggetto(c)).props('flat round dense color=red size=sm')

        def process_user_addition(u_data):
            cf = u_data['CODICE_FISCALE']
            if cf in soggetti:
                ui.notify("Presente!", color='orange'); return
            # Inizializziamo anche il docente_cf a None
            soggetti[cf] = {'user': u_data, 'cid': None, 'docente_cf': None, 'per': None, 'date_extra': '', 'ore': None}
            render_lista_soggetti.refresh()
            count_label.set_text(f"Totale: {len(soggetti)}")
            ui.notify(f"Aggiunto: {u_data['COGNOME']}", color='green')
            logger.info(f"Utente: {u_data['COGNOME']} {u_data['NOME']} aggiunto alla griglia!")

        def rimuovi_soggetto(cf):
            if cf in soggetti: del soggetti[cf]; render_lista_soggetti.refresh(); count_label.set_text(f"Totale: {len(soggetti)}")
        
        def svuota_lista():
            soggetti.clear(); render_lista_soggetti.refresh(); count_label.set_text("Totale: 0")

        def open_search_ui():
            search_input.value = ""; search_results_area.clear(); search_dialog.open()

        async def perform_search():
            term = search_input.value
            if not term: return
            res = await asyncio.to_thread(get_user_details_from_db_sync, term)
            search_results_area.clear()
            if not res:
                with search_results_area: ui.label("Nessun risultato.").classes('text-red italic')
                return
            if len(res) == 1:
                process_user_addition(res[0]); search_dialog.close()
            else:
                with search_results_area:
                    with ui.list().props('bordered separator dense'):
                        for u in res:
                            dob = u['DATA_NASCITA'].strftime('%d/%m/%Y') if u['DATA_NASCITA'] else "%s"
                            lbl = f"{u['COGNOME']} {u['NOME']} ({dob})"
                            with ui.item().props('clickable').on('click', lambda e, x=u: (process_user_addition(x), search_dialog.close())):
                                with ui.item_section():
                                    ui.item_label(lbl)
                                    ui.item_label(u['CODICE_FISCALE']).props('caption')
        search_btn.on_click(perform_search)
        search_input.on('keydown.enter', perform_search)

        with ui.row().classes('w-full justify-between items-center mt-2 mb-2'):
             ui.label('Lista Destinatari').classes('text-xl font-bold')
             with ui.row():
                 ui.button('Aggiungi', on_click=open_search_ui, icon='person_add').props('color=primary')
                 ui.button('Svuota', on_click=svuota_lista, icon='delete_sweep').props('color=red flat')

        with ui.column().classes('w-full p-4 border rounded shadow-md bg-white'):
            count_label = ui.label("Totale: 0").classes('ml-auto text-sm text-gray-500')
            render_lista_soggetti()

        # --- GENERAZIONE PDF/ZIP ---
        async def on_generate():
            items = list(soggetti.values())
            if not items: 
                ui.notify("Lista vuota", color='red')
                return
            
            # Controllo preliminare dati mancanti
            if any(not x['cid'] or not x['per'] for x in items): 
                ui.notify("Dati mancanti (Corso o Data) per alcuni utenti!", color='red')
                return

            ui.notify("Generazione in corso...", spinner=True)
            
            z_path = None
            tmp = None

            try:
                # Creazione cartella temporanea
                tmp = tempfile.mkdtemp()
                files_to_zip = []
                
                # Raggruppamento per Corso (cid) e Data (dt_obj)
                grouped_items = {}
                for it in items:
                    raw_date = it['per']
                    dt_obj = date.today()
                    # Parsing della data flessibile
                    try:
                        if re.search(r'\d{4}-\d{2}-\d{2}', str(raw_date)):
                             dt_obj = datetime.strptime(re.search(r'\d{4}-\d{2}-\d{2}', str(raw_date)).group(0), '%Y-%m-%d').date()
                        elif re.search(r'\d{2}/\d{2}/\d{4}', str(raw_date)):
                             dt_obj = datetime.strptime(re.search(r'\d{2}/\d{2}/\d{4}', str(raw_date)).group(0), '%d/%m/%Y').date()
                    except: pass
                    
                    key = (it['cid'], dt_obj)
                    if key not in grouped_items: grouped_items[key] = []
                    grouped_items[key].append(it)

                # Ciclo principale di generazione
                for (cid, dt_val), group_list in grouped_items.items():
                    
                    codice_corso = corsi_codici.get(cid, "GEN")
                    n_sessione = await asyncio.to_thread(get_next_session_number_sync, cid, dt_val)
                    data_codice = dt_val.strftime('%d%m%Y')
                    
                    # Nome cartella output: es. 1_SIC_25112024
                    sigla_cartella = f"{n_sessione}{codice_corso}{data_codice}"
                    path_sigla = os.path.join(tmp, sigla_cartella)
                    os.makedirs(path_sigla, exist_ok=True)
                    
                    nome_corso_full = corsi_opts[cid]
                    
                    # --- 1. RECUPERO DATI DINAMICI (Template e Programma) ---
                    nome_template = corsi_templates.get(cid, 'modello.docx')
                    if not nome_template: nome_template = 'modello.docx' # Sicurezza extra
                    
                    programma_txt = corsi_programmi.get(cid, '')

                    # --- 2. SPIA DI DEBUG (Viola) ---
                    msg_debug = f"Corso ID {cid}: Uso file '{nome_template}'"
                    print(msg_debug)
                    ui.notify(msg_debug, color='purple', close_button=True)
                    # -------------------------------

                    # Percorso completo del file Word
                    path_template = os.path.join("templates", nome_template)
                    
                    # Controllo esistenza file
                    if not os.path.exists(path_template):
                        ui.notify(f"ERRORE GRAVE: Il file '{nome_template}' non esiste nella cartella 'templates'!", color='red', close_button=True, multi_line=True)
                        return # Interrompe tutto per evitare crash o file sbagliati

                    # --- 3. Generazione per ogni utente del gruppo ---
                    for it in group_list:
                        u = it['user']
                        # Cartella Azienda
                        safe_az = re.sub(r'\W', '_', u.get('SOCIETA', 'Privati'))
                        final_dir = os.path.join(path_sigla, safe_az)
                        os.makedirs(final_dir, exist_ok=True)
                        
                        data_inizio_str = dt_val.strftime('%d/%m/%Y')
                        periodo_completo = f"{data_inizio_str} {it['date_extra']}" if it.get('date_extra') else data_inizio_str
                        
                        nome_docente = docenti_opts.get(it.get('docente_cf'), '')

                        # Mappa Sostituzioni
                        d_map = {
                            "{{COGNOME}}": u['COGNOME'], 
                            "{{NOME}}": u['NOME'],
                            "{{CODICE}}": sigla_cartella,
                            "{{CF}}": u['CODICE_FISCALE'],
                            "{{DATA_NASCITA}}": u['DATA_NASCITA'],
                            "{{LUOGO_NASCITA}}": u['LUOGO_NASCITA'],
                            "{{SOCIETA}}": u['SOCIETA'],
                            "{{NOME_CORSO}}": nome_corso_full,
                            "{{DATA_SVOLGIMENTO}}": periodo_completo,
                            "{{ORE_DURATA}}": it['ore'],
                            "{{DATA_RILASCIOAT}}" : data_inizio_str,
                            "{{SIGLA}}": sigla_cartella,
                            "{{DOCENTE}}": nome_docente,
                            "{{PROGRAMMA}}": programma_txt  # <--- INSERISCE IL PROGRAMMA
                        }

                        # Chiamata generazione (passando il template specifico)
                        f = await asyncio.to_thread(generate_certificate_sync, d_map, path_template, final_dir)
                        files_to_zip.append(f)
                        
                        # Salvataggio storico nel DB
                        await asyncio.to_thread(save_attestato_to_db_sync, u['CODICE_FISCALE'], cid, dt_val)

                # Creazione ZIP finale
                z_name = f"Export_{datetime.now().strftime('%d%m%Y_%H%M%S')}.zip"
                z_path = await asyncio.to_thread(generate_zip_sync, files_to_zip, tmp, z_name)
                
                ui.download(z_path)
                ui.notify(f"Operazione completata! {len(files_to_zip)} attestati generati.", color='green')
                
                # Pulizia lista UI
                soggetti.clear()
                render_lista_soggetti.refresh()
                count_label.set_text("Totale: 0")

            except Exception as e:
                logger.error(f"Errore generazione: {e}")
                ui.notify(f"Errore durante la generazione: {e}", color='red', close_button=True, multi_line=True)
                print(f"ERR GEN: {e}")
            finally:
                # Pulizia file temporanei (dopo un po' per permettere il download)
                await asyncio.sleep(10)
                if z_path and os.path.exists(z_path): 
                    try: os.remove(z_path)
                    except: pass
                if tmp and os.path.exists(tmp): 
                    try: shutil.rmtree(tmp, ignore_errors=True)
                    except: pass

        ui.button("Genera attestati", on_click=on_generate).classes('w-full mt-6').props('color=blue size=lg')

@ui.page('/gestioneutenti')
def gestioneutenti_page():
    if not app.storage.user.get('authenticated', False): ui.navigate.to('/'); return
    
    state = {'is_new': True, 'search': ''}
    
    # --- 1. INIZIALIZZAZIONE VARIABILI (A NONE) ---
    # Fondamentale per evitare problemi di scope
    cf_input = None
    cognome_input = None
    nome_input = None
    data_input_field = None
    luogo_input = None
    ente_select = None
    
    dialog_label = None
    table_ref = None
    dialog_ref = None

    # Helper per le opzioni della Select
    async def get_enti_options():
        enti = await asyncio.to_thread(EnteRepo.get_all, '')
        return {e['ID_ENTE']: f"{e['DESCRIZIONE']} ({e['P_IVA']})" for e in enti}

    async def refresh_table():
        rows = await asyncio.to_thread(UserRepo.get_all, state['search'])
        
        # Formattazione data per la tabella (visualizzazione italiana)
        for r in rows:
            if r['DATA_NASCITA'] and '-' in r['DATA_NASCITA']:
                try:
                    anno, mese, giorno = r['DATA_NASCITA'].split('-')
                    r['DATA_DISPLAY'] = f"{giorno}-{mese}-{anno}"
                except:
                    r['DATA_DISPLAY'] = r['DATA_NASCITA']
            else:
                r['DATA_DISPLAY'] = ''

        if table_ref: table_ref.rows = rows; table_ref.update()

    async def open_dialog(row=None):
        # 1. Carichiamo gli enti aggiornati PRIMA di tutto
        opzioni_enti = await get_enti_options()
        # Assicuriamoci che ente_select esista
        if ente_select:
            ente_select.options = opzioni_enti
            ente_select.update()

        dialog_ref.open()
        
        if row:
            # --- MODALITÀ MODIFICA ---
            state['is_new'] = False
            
            # Popoliamo i campi (Ora le variabili esistono sicuramente)
            cf_input.value = row['CODICE_FISCALE']
            cf_input.props('readonly') # CF non si cambia in modifica
            
            cognome_input.value = row['COGNOME']
            nome_input.value = row['NOME']
            data_input_field.value = row['DATA_NASCITA'] # Formato YYYY-MM-DD dal DB
            luogo_input.value = row['LUOGO_NASCITA']
            
            # Impostiamo l'ente selezionato
            ente_select.value = row['ID_ENTE_FK']
            
            dialog_label.text = "Modifica Utente"
        else:
            # --- MODALITÀ NUOVO ---
            state['is_new'] = True
            cf_input.value = ''
            cf_input.props(remove='readonly') # CF scrivibile
            
            cognome_input.value = ''
            nome_input.value = ''
            data_input_field.value = ''
            luogo_input.value = ''
            ente_select.value = None
            
            dialog_label.text = "Nuovo Utente"

    async def save_user():
        if not cf_input.value or not cognome_input.value: 
            ui.notify('Campi obbligatori mancanti (CF, Cognome)!', type='warning')
            return
        
        ente_val = ente_select.value if ente_select.value is not None else ''

        data = {
            'CODICE_FISCALE': cf_input.value.upper().strip(), 
            'COGNOME': cognome_input.value.strip(),
            'NOME': nome_input.value.strip(), 
            'DATA_NASCITA': data_input_field.value,
            'LUOGO_NASCITA': luogo_input.value, 
            'ID_ENTE_FK': ente_val
        }
        
        success, msg = await asyncio.to_thread(UserRepo.upsert, data, state['is_new'])
        if success: 
            ui.notify(msg, type='positive')
            dialog_ref.close()
            await refresh_table()
        else: 
            ui.notify(msg, type='negative')

    async def delete_user(row):
        await asyncio.to_thread(UserRepo.delete, row['CODICE_FISCALE'])
        ui.notify("Eliminato", type='info'); await refresh_table()

    # --- 3. LAYOUT PAGINA ---
    with ui.column().classes('w-full items-center p-8 max-w-screen-xl mx-auto bg-slate-50 min-h-screen'):
        with ui.row().classes('w-full items-center mb-6 justify-between'):
            ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/dashboard')).props('flat round dense')
            ui.label('Gestione Utenti').classes('text-3xl font-bold text-slate-800')
            ui.button('Nuovo', icon='add', on_click=lambda: open_dialog(None)).props('unelevated color=primary')

        with ui.card().classes('w-full p-2 mb-4 flex flex-row items-center gap-4'):
            ui.icon('search').classes('text-grey ml-2')
            ui.input(placeholder='Cerca...').classes('flex-grow').props('borderless').bind_value(state, 'search').on('keydown.enter', refresh_table)
            ui.button('Cerca', on_click=refresh_table).props('flat color=primary')

        cols = [
            {'name': 'CODICE_FISCALE', 'label': 'CF', 'field': 'CODICE_FISCALE', 'align': 'left', 'sortable': True},
            {'name': 'COGNOME', 'label': 'Cognome', 'field': 'COGNOME', 'sortable': True, 'align': 'left'},
            {'name': 'NOME', 'label': 'Nome', 'field': 'NOME', 'align': 'left'},
            {'name': 'DATA_NASCITA', 'label': 'Data', 'field': 'DATA_DISPLAY', 'align': 'center'}, # Usiamo il campo formattato
            {'name': 'ID_ENTE_FK', 'label': 'ID Ente', 'field': 'ID_ENTE_FK', 'align': 'center'},
            {'name': 'azioni', 'label': '', 'field': 'azioni', 'align': 'right'},
        ]
        table_ref = ui.table(columns=cols, rows=[], row_key='CODICE_FISCALE').classes('w-full shadow-md bg-white')
        table_ref.add_slot('body-cell-azioni', r'''
            <q-td key="azioni" :props="props">
                <q-btn icon="edit" size="sm" round flat color="grey-8" @click="$parent.$emit('edit', props.row)" />
                <q-btn icon="delete" size="sm" round flat color="red" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        table_ref.on('edit', lambda e: open_dialog(e.args))
        table_ref.on('delete', lambda e: delete_user(e.args))
        ui.timer(0.1, refresh_table, once=True)

    # --- 4. DIALOGO E CREAZIONE INPUT ---
    with ui.dialog() as dialog_ref, ui.card().classes('w-full max-w-2xl p-0 rounded-xl overflow-hidden'):
        with ui.row().classes('w-full bg-primary text-white p-4 items-center justify-between'):
            dialog_label = ui.label('Utente').classes('text-lg font-bold')
            ui.button(icon='close', on_click=dialog_ref.close).props('flat round dense text-white')
        
        with ui.column().classes('w-full p-6 gap-4'):
            with ui.row().classes('w-full gap-4'):
                # Assegnazione alle variabili inizializzate all'inizio
                cf_input = ui.input('CF').props('outlined dense uppercase').classes('w-full md:w-1/2')
                ente_select = ui.select(options={}, with_input=True, label='Seleziona Ente') \
                    .props('outlined dense use-input input-debounce="0" behavior="menu"') \
                    .classes('w-full md:w-1/2')

            with ui.row().classes('w-full gap-4'):
                cognome_input = ui.input('Cognome').props('outlined dense').classes('w-full md:w-1/2')
                nome_input = ui.input('Nome').props('outlined dense').classes('w-full md:w-1/2')
            with ui.row().classes('w-full gap-4'):
                # Usiamo tipo 'date' per avere il calendario nativo
                data_input_field = ui.input('Data (YYYY-MM-DD)').props('outlined dense type=date').classes('w-full md:w-1/3')
                luogo_input = ui.input('Luogo Nascita').props('outlined dense').classes('w-full md:w-2/3')
            
            ui.button('Salva', on_click=save_user).props('unelevated color=primary w-full')

@ui.page('/gestioneenti')
def gestioneenti_page():
    if not app.storage.user.get('authenticated', False): ui.navigate.to('/'); return
    
    state = {'is_new': True, 'search': ''}
    
    # --- 1. INIZIALIZZIAMO LE VARIABILI QUI ---
    # Questo è fondamentale affinché open_dialog le veda, anche se sono vuote all'inizio
    # Usiamo un "contenitore" (lista o dict) o semplicemente dichiariamo le variabili
    # Ma il trucco migliore con NiceGUI è definire i riferimenti prima
    
    dialog_ref = None
    dialog_label = None
    table_ref = None
    
    # Definiamo gli input ma li creeremo dopo nella UI. 
    # Li inizializziamo a None per evitare il NameError, ma Python deve sapere che esistono.
    id_ente_input = None 
    desc_input = None 
    piva_input = None

    # --- 2. LOGICA ---

    async def refresh_table():
        rows = await asyncio.to_thread(EnteRepo.get_all, state['search'])
        if table_ref: table_ref.rows = rows; table_ref.update()

    async def open_dialog(row=None):
        # NOTA: Qui usiamo 'nonlocal' se dovessimo riassegnare l'oggetto input intero, 
        # ma dato che tocchiamo solo .value e .props, Python capisce il riferimento 
        # SE l'oggetto è stato creato prima della chiamata della funzione.
        
        dialog_ref.open()
        if row:
            # --- MODALITÀ MODIFICA ---
            state['is_new'] = False
            id_ente_input.value = row['ID_ENTE']
            id_ente_input.props('readonly') # Blocchiamo ID
            desc_input.value = row['DESCRIZIONE']
            piva_input.value = row['P_IVA']
            dialog_label.text = "Modifica Ente"
        else:
            # --- MODALITÀ NUOVO (AUTO-INCREMENT) ---
            state['is_new'] = True
            
            # Calcolo ID automatico
            next_id = await asyncio.to_thread(EnteRepo.get_next_id)
            
            id_ente_input.value = next_id     # Precompila
            id_ente_input.props('readonly')   # Blocca modifica
            
            desc_input.value = ''
            piva_input.value = ''
            dialog_label.text = "Nuovo Ente"

    async def save_ente():
        if not id_ente_input.value or not desc_input.value: 
            ui.notify('Dati mancanti!', type='warning')
            return
        
        id_val = str(id_ente_input.value).strip()
        desc_val = str(desc_input.value).strip()
        piva_val = str(piva_input.value).strip() if piva_input.value else ''

        data = {'ID_ENTE': id_val, 'DESCRIZIONE': desc_val, 'P_IVA': piva_val}

        success, msg = await asyncio.to_thread(EnteRepo.upsert, data, state['is_new'])
        if success: 
            ui.notify(msg, type='positive')
            dialog_ref.close()
            await refresh_table()
        else: 
            ui.notify(msg, type='negative')

    async def delete_ente(row):
        await asyncio.to_thread(EnteRepo.delete, row['ID_ENTE'])
        ui.notify("Eliminato", type='info'); await refresh_table()

    # --- 3. INTERFACCIA UTENTE (UI) ---
    
    with ui.column().classes('w-full items-center p-8 max-w-screen-xl mx-auto bg-slate-50 min-h-screen'):
        with ui.row().classes('w-full items-center mb-6 justify-between'):
            ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/dashboard')).props('flat round dense')
            ui.label('Enti').classes('text-3xl font-bold text-slate-800')
            # NOTA: Qui passiamo open_dialog, che ora è async ma NiceGUI gestisce lambda/async bene
            ui.button('Nuovo', icon='add', on_click=lambda: open_dialog(None)).props('unelevated color=primary')

        with ui.card().classes('w-full p-2 mb-4 flex flex-row items-center gap-4'):
            ui.icon('search').classes('text-grey ml-2')
            ui.input(placeholder='Cerca...').classes('flex-grow').props('borderless').bind_value(state, 'search').on('keydown.enter', refresh_table)
            ui.button('Cerca', on_click=refresh_table).props('flat color=primary')

        cols = [
            {'name': 'ID_ENTE', 'label': 'ID', 'field': 'ID_ENTE', 'align': 'left', 'sortable': True},
            {'name': 'DESCRIZIONE', 'label': 'Descrizione', 'field': 'DESCRIZIONE', 'sortable': True, 'align': 'left'},
            {'name': 'P_IVA', 'label': 'P.IVA', 'field': 'P_IVA', 'align': 'center'},
            {'name': 'azioni', 'label': '', 'field': 'azioni', 'align': 'right'},
        ]
        table_ref = ui.table(columns=cols, rows=[], row_key='ID_ENTE').classes('w-full shadow-md bg-white')
        table_ref.add_slot('body-cell-azioni', r'''
            <q-td key="azioni" :props="props">
                <q-btn icon="edit" size="sm" round flat color="grey-8" @click="$parent.$emit('edit', props.row)" />
                <q-btn icon="delete" size="sm" round flat color="red" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        table_ref.on('edit', lambda e: open_dialog(e.args))
        table_ref.on('delete', lambda e: delete_ente(e.args))
        ui.timer(0.1, refresh_table, once=True)

    # --- 4. CREAZIONE DIALOG E INPUT ---
    # Qui vengono effettivamente create le variabili id_ente_input, ecc.
    
    with ui.dialog() as dialog_ref, ui.card().classes('w-full max-w-lg p-0 rounded-xl overflow-hidden'):
        with ui.row().classes('w-full bg-primary text-white p-4 items-center justify-between'):
            dialog_label = ui.label('Ente').classes('text-lg font-bold')
            ui.button(icon='close', on_click=dialog_ref.close).props('flat round dense text-white')
        
        with ui.column().classes('w-full p-6 gap-4'):
            # --- PUNTO CRUCIALE: Assegniamo alle variabili inizializzate all'inizio ---
            id_ente_input = ui.input('ID Ente').props('outlined dense').classes('w-full')
            desc_input = ui.input('Ragione Sociale').props('outlined dense').classes('w-full')
            piva_input = ui.input('P.IVA').props('outlined dense').classes('w-full')
            
            ui.button('Salva', on_click=save_ente).props('unelevated color=primary w-full')
            
@ui.page('/gestionedocenti')
def gestionedocenti_page():
    # -- PROVA -- 
    if not app.storage.user.get('authenticated', False): ui.navigate.to('/'); return
    state = {'is_new': True, 'search': ''}
    # Variabili UI
    cf_input = None; cognome_input = None; nome_input = None
    data_input_field = None; luogo_input = None; ente_input = None
    dialog_ref = None; table_ref = None; dialog_label = None

    async def refresh_table():
        # --- QUI LA DIFFERENZA: solo_docenti=True ---
        rows = await asyncio.to_thread(UserRepo.get_all, state['search'], solo_docenti=True)
        if table_ref: table_ref.rows = rows; table_ref.update()

    def open_dialog(row=None):
        dialog_ref.open()
        if row:
            state['is_new'] = False
            cf_input.value = row['CODICE_FISCALE']; cf_input.props('readonly') 
            cognome_input.value = row['COGNOME']; nome_input.value = row['NOME']
            data_input_field.value = row['DATA_NASCITA']; luogo_input.value = row['LUOGO_NASCITA']
            ente_input.value = row['ID_ENTE_FK']
            dialog_label.text = "Modifica Docente"
        else:
            state['is_new'] = True
            cf_input.value = ''; cf_input.props(remove='readonly')
            cognome_input.value = ''; nome_input.value = ''
            data_input_field.value = ''; luogo_input.value = ''; ente_input.value = ''
            dialog_label.text = "Nuovo Docente"

    async def save_docente():
        if not cf_input.value or not cognome_input.value: ui.notify('Dati mancanti!', type='warning'); return
        
        data = {
            'CODICE_FISCALE': cf_input.value.upper().strip(),
            'COGNOME': cognome_input.value.strip(),
            'NOME': nome_input.value.strip(),
            'DATA_NASCITA': data_input_field.value,
            'LUOGO_NASCITA': luogo_input.value,
            'ID_ENTE_FK': ente_input.value,
            'IS_DOCENTE': True  # --- FORZIAMO CHE SIA UN DOCENTE ---
        }
        
        success, msg = await asyncio.to_thread(UserRepo.upsert, data, state['is_new'])
        if success: ui.notify(msg, type='positive'); dialog_ref.close(); await refresh_table()
        else: ui.notify(msg, type='negative')

    async def delete_docente(row):
        await asyncio.to_thread(UserRepo.delete, row['CODICE_FISCALE'])
        ui.notify("Eliminato", type='info'); await refresh_table()

    # --- UI IDENTICA A GESTIONE UTENTI MA TITOLI DIVERSI ---
    with ui.column().classes('w-full items-center p-8 max-w-screen-xl mx-auto bg-slate-50 min-h-screen'):
        with ui.row().classes('w-full items-center mb-6 justify-between'):
            ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/dashboard')).props('flat round dense')
            ui.label('Gestione Docenti').classes('text-3xl font-bold text-slate-800')
            ui.button('Nuovo Docente', icon='add', on_click=lambda: open_dialog(None)).props('unelevated color=primary')

        # ... SEARCH BAR ...
        with ui.card().classes('w-full p-2 mb-4 flex flex-row items-center gap-4'):
            ui.icon('search').classes('text-grey ml-2')
            ui.input(placeholder='Cerca Docente...').classes('flex-grow').props('borderless').bind_value(state, 'search').on('keydown.enter', refresh_table)
            ui.button('Cerca', on_click=refresh_table).props('flat color=primary')

        # ... TABLE ...
        cols = [
            {'name': 'CODICE_FISCALE', 'label': 'CF', 'field': 'CODICE_FISCALE', 'align': 'left', 'sortable': True},
            {'name': 'COGNOME', 'label': 'Cognome', 'field': 'COGNOME', 'sortable': True, 'align': 'left'},
            {'name': 'NOME', 'label': 'Nome', 'field': 'NOME', 'align': 'left'},
            {'name': 'azioni', 'label': '', 'field': 'azioni', 'align': 'right'},
        ]
        table_ref = ui.table(columns=cols, rows=[], row_key='CODICE_FISCALE').classes('w-full shadow-md bg-white')
        table_ref.add_slot('body-cell-azioni', r'''
            <q-td key="azioni" :props="props">
                <q-btn icon="edit" size="sm" round flat color="grey-8" @click="$parent.$emit('edit', props.row)" />
                <q-btn icon="delete" size="sm" round flat color="red" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        table_ref.on('edit', lambda e: open_dialog(e.args))
        table_ref.on('delete', lambda e: delete_docente(e.args))
        ui.timer(0.1, refresh_table, once=True)

    # --- DIALOGO (Identico a Gestione Utenti) ---
    with ui.dialog() as dialog_ref, ui.card().classes('w-full max-w-2xl p-0 rounded-xl overflow-hidden'):
        with ui.row().classes('w-full bg-primary text-white p-4 items-center justify-between'):
            dialog_label = ui.label('Docente').classes('text-lg font-bold')
            ui.button(icon='close', on_click=dialog_ref.close).props('flat round dense text-white')
        with ui.column().classes('w-full p-6 gap-4'):
            with ui.row().classes('w-full gap-4'):
                cf_input = ui.input('CF').props('outlined dense uppercase').classes('w-full md:w-1/2')
                ente_input = ui.input('ID Ente (Opzionale)').props('outlined dense').classes('w-full md:w-1/2') 
            with ui.row().classes('w-full gap-4'):
                cognome_input = ui.input('Cognome').props('outlined dense').classes('w-full md:w-1/2')
                nome_input = ui.input('Nome').props('outlined dense').classes('w-full md:w-1/2')
            with ui.row().classes('w-full gap-4'):
                data_input_field = ui.input('Data Nascita').props('outlined dense').classes('w-full md:w-1/3')
                luogo_input = ui.input('Luogo Nascita').props('outlined dense').classes('w-full md:w-2/3')
            ui.button('Salva Docente', on_click=save_docente).props('unelevated color=primary w-full')

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="WorkSafeManager", storage_secret='secret_key', reload=True ,port=8001)