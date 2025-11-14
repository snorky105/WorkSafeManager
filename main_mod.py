import json
import fdb
import bcrypt
import asyncio
from nicegui import ui, app, run  # Importa 'app' e 'run'
import os  # Necessario per i percorsi dei file
from docx import Document  # Importa la libreria per i .docx
from datetime import datetime # Per formattare le date
import tempfile  # Per creare cartelle temporanee
import shutil  # Per eliminare le cartelle temporanee
import zipfile  # Per creare il file .zip
import re # Per pulire i nomi delle cartelle

# --- LOG DI AVVIO ---
print("--- DEBUG: AVVIO WorkSafeManager CON LOGGING ATTIVO ---")

# --- LOAD CONFIG ---
# Assicurati che i file config.json e queries.json siano presenti
try:
    with open('config.json', 'r') as f:
        config = json.load(f)

    with open('queries.json', 'r') as f:
        queries = json.load(f)
except FileNotFoundError:
    print("ERRORE: Assicurati che 'config.json' e 'queries.json' esistano.")
    exit()

# --- FUNZIONE HELPER PER CONNESSIONE DB ---
def get_db_connection():
    """Crea e restituisce una connessione fdb."""
    return fdb.connect(
        host=config['host'],
        database=config['database'],
        user=config['user'],
        password=config['password'],
        port=config.get('port', 3050),
        charset='UTF8' # Aggiunto charset per sicurezza
    )

# --- SYNC DB QUERY ---
def check_credentials_sync(username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(queries['ProcLogIn'], (username,))
        row = cur.fetchone()
        conn.close()
        if row:
            print(f"Password dal DB per {username}: '{row[0]}'")
        else:
            print(f"Nessuna password trovata per {username}")
        return row
    except Exception as e:
        print(f"Errore DB (check_credentials_sync): {e}")
        return None

# --- ASYNC WRAPPER ---
async def check_credentials(username, password):
    row = await asyncio.to_thread(check_credentials_sync, username)
    if not row:
        return False
    stored_password = row[0]
    print(f"Password inserita: '{password}'")

    # Provo confronto in chiaro (solo per debug)
    if password == stored_password:
        print("Password corretta (confronto in chiaro)")
        return True

    # Provo confronto bcrypt
    try:
        if isinstance(stored_password, str):
            stored_hash = stored_password.encode('utf-8')
        else:
            stored_hash = stored_password 
            
        if bcrypt.checkpw(password.encode('utf-8'), stored_hash):
            print("Password corretta (confronto bcrypt)")
            return True
    except Exception as e:
        print(f"Errore nel confronto bcrypt: {e}")

    return False

# --- FUNZIONE RICERCA UTENTE (REALE) ---
def get_user_details_from_db_sync(search_term: str):
    """
    Esegue una ricerca REALE nel DB per i dettagli dell'utente.
    """
    print(f"DEBUG: get_user_details_from_db_sync() - Ricerca DB reale per: {search_term}")
    search_param = f"{search_term.upper()}%" 
    sql = """
        SELECT 
            s.CODICE_FISCALE, s.COGNOME, s.NOME, s.DATA_NASCITA, s.LUOGO_NASCITA, e.DESCRIZIONE AS SOCIETA
        FROM T_SOGGETTI s
        LEFT JOIN T_ENTI e ON s.ID_ENTE_FK = e.ID_ENTE
        WHERE 
            (UPPER(s.COGNOME) STARTING WITH ?) OR 
            (UPPER(s.NOME) STARTING WITH ?) OR 
            (UPPER(s.CODICE_FISCALE) = ?)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (search_param, search_param, search_term.upper()))
        row = cur.fetchone()
        conn.close()
        
        if row:
            print(f"DEBUG: get_user_details_from_db_sync() - Trovato utente: {row}")
            return {
                "CODICE_FISCALE": row[0],
                "COGNOME": row[1],
                "NOME": row[2],
                "DATA_NASCITA": row[3], 
                "LUOGO_NASCITA": row[4],
                "SOCIETA": row[5] if row[5] else "" 
            }
        else:
            print("DEBUG: get_user_details_from_db_sync() - Utente non trovato.")
            return None
    except Exception as e:
        print(f"Errore DB (get_user_details_from_db_sync): {e}")
        return None

# --- FUNZIONE: CARICAMENTO CORSI (REALE) ---
def get_corsi_from_db_sync():
    """
    Carica la lista di tutti i corsi (ID, Nome, Ore) dalla tabella T_CORSI.
    """
    print("DEBUG: get_corsi_from_db_sync() - Caricamento lista corsi dal DB...")
    # Seleziona anche ORE_DURATA
    sql = "SELECT ID_CORSO, NOME_CORSO, ORE_DURATA FROM T_CORSI ORDER BY NOME_CORSO"
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        conn.close()
        print(f"DEBUG: get_corsi_from_db_sync() - Trovati {len(rows)} corsi.")
        # Restituisce una lista di dizionari (ID, Nome, Ore)
        return [{"id": row[0], "nome": row[1], "ore": row[2]} for row in rows]
    except Exception as e:
        print(f"Errore DB (get_corsi_from_db_sync): {e}")
        return []

# --- FUNZIONE: SALVATAGGIO ATTESTATO (REALE) ---
def save_attestato_to_db_sync(cf_soggetto, id_corso, data_svolgimento):
    """
    Salva il nuovo attestato generato nella tabella T_ATTESTATI.
    """
    print(f"DEBUG: save_attestato_to_db_sync() - Salvataggio attestato nel DB per {cf_soggetto}...")
    sql = """
        INSERT INTO T_ATTESTATI (ID_SOGGETTO_FK, ID_CORSO_FK, DATA_SVOLGIMENTO)
        VALUES (?, ?, ?)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        data_obj = datetime.strptime(data_svolgimento, '%Y-%m-%d').date()
        cur.execute(sql, (cf_soggetto, id_corso, data_obj))
        conn.commit()
        conn.close()
        print("DEBUG: save_attestato_to_db_sync() - Salvataggio riuscito.")
        return True
    except Exception as e:
        print(f"Errore DB (save_attestato_to_db_sync): {e}")
        conn.rollback()
        conn.close()
        return False

# --- FUNZIONE: GENERAZIONE DOCUMENTO ---
def generate_certificate_sync(data_map: dict, template_file="modello.docx", output_dir=None) -> str:
    """Genera un certificato da un modello .docx."""
    
    if not os.path.exists(template_file):
        print(f"ERRORE: File modello non trovato in {template_file}")
        raise FileNotFoundError(f"File modello non trovato: {template_file}") 
    
    doc = Document(template_file)
    
    # Copia locale della mappa per non modificare l'originale
    local_data_map = data_map.copy()
    
    # Formattiamo la data di nascita se esiste
    data_nascita_val = local_data_map.get("{{DATA NASCITA}}")
    if isinstance(data_nascita_val, (datetime, datetime.date)):
        local_data_map["{{DATA NASCITA}}"] = data_nascita_val.strftime('%d/%m/%Y')
        
    # Formattiamo la data del corso
    data_corso_val = local_data_map.get("{{PERIODO SVOLGIMENTO}}")
    if isinstance(data_corso_val, str) and len(data_corso_val) == 10: # 'YYYY-MM-DD'
        local_data_map["{{PERIODO SVOLGIMENTO}}"] = datetime.strptime(data_corso_val, '%Y-%m-%d').strftime('%d/%m/%Y')


    # Sostituisce i segnaposto nei paragrafi
    for p in doc.paragraphs:
        for key, value in local_data_map.items():
            if key in p.text:
                p.text = p.text.replace(key, str(value if value is not None else ''))
    
    # Sostituisce i segnaposto nelle tabelle
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for key, value in local_data_map.items():
                        if key in p.text:
                            p.text = p.text.replace(key, str(value if value is not None else ''))

    # Salva il nuovo documento
    # Pulisce Cognome e Nome per usarli nel nome file
    safe_cognome = re.sub(r'[^\w-]', '', local_data_map.get('{{COGNOME}}', 'user'))
    safe_nome = re.sub(r'[^\w-]', '', local_data_map.get('{{NOME}}', 'user'))
    output_filename = f"attestato_{safe_cognome}_{safe_nome}.docx"
    
    # Se è specificata una cartella di output (per il batch), la usa
    if output_dir:
        output_path = os.path.join(output_dir, output_filename)
    else:
        output_path = output_filename
        
    doc.save(output_path)
    print(f"Documento generato: {output_path}")
    return output_path

# --- FUNZIONE CREAZIONE ZIP (accetta base_dir) ---
def generate_zip_sync(files_to_zip: list, base_dir: str, zip_filename="attestati.zip") -> str:
    """
    Crea un file .zip contenente tutti i file specificati, 
    preservando la struttura delle cartelle relative a base_dir.
    """
    print(f"Creazione file zip: {zip_filename}")
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in files_to_zip:
            # Calcola il percorso relativo dalla cartella base
            arcname = os.path.relpath(file_path, base_dir)
            zipf.write(file_path, arcname=arcname)
    print("File zip creato.")
    return zip_filename

# --- NICEGUI APP ---

@ui.page('/')
def login_page():
    # --- CONTROLLO ACCESSO ---
    if app.storage.user.get('authenticated', False):
        ui.navigate.to('/dashboard') 
        return
        
    # --- CENTRA IL CONTENUTO ---
    with ui.column().classes('absolute-center w-full max-w-sm items-center'):
        ui.label("Benvenuto in WorkSafeManager").classes("text-3xl font-bold mb-8 text-center")
        with ui.card().style("padding: 40px;").classes("w-full"):
            username_input = ui.input("Utente").props("outlined").classes("w-full mb-2")
            password_input = ui.input("Password").props("outlined type=password").classes("w-full mb-4")

            async def on_login_click():
                user = username_input.value.strip()
                pwd = password_input.value.strip()
                print(f"Tentativo login utente: {user}")

                if not user or not pwd:
                    ui.notify("Inserisci username e password!", color="red")
                    return

                valid = await check_credentials(user, pwd)
                if valid:
                    print(f"Login riuscito per utente: {user}")
                    app.storage.user['authenticated'] = True
                    app.storage.user['username'] = user 
                    ui.notify("Login riuscito!", color="green")
                    ui.navigate.to('/dashboard')
                else:
                    print(f"Login fallito per utente: {user}")
                    ui.notify("Credenziali errate", color="red")

            ui.button("Entra", on_click=on_login_click).classes("w-full mt-4")

@ui.page('/dashboard')
def dashboard_page():
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return 

    username = app.storage.user.get('username', 'Utente')
    ui.label(f"Dashboard - Benvenuto, {username}").classes("text-3xl font-bold mb-8 text-center")
    
    # --- Sezioni Espandibili ---
    with ui.expansion('Creazione Attestati', icon='explore').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la creazione degli attestati.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/creaattestati')).classes('m-4')
    
    with ui.expansion('Gestione Utenti', icon='people').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la gestione Utenti.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/gestioneutenti')).classes('m-4')
        
    with ui.expansion('Gestione Enti', icon='home').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la gestione Enti.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/gestioneenti')).classes('m-4')

    # Bottone logout
    def logout_click():
        print(f"Logout per utente: {app.storage.user.get('username')}")
        app.storage.user['authenticated'] = False
        app.storage.user.pop('username', None) 
        ui.notify("Logout effettuato", color="blue")
        ui.navigate.to('/')
        
    ui.button("Logout", on_click=logout_click).classes("bg-red-500 text-white mx-auto block mt-8 mb-8")


# --- PAGINA /creaattestati (LOGICA ADDITIVA CORRETTA) ---
@ui.page('/creaattestati')
def creaattestati_page():
    
    print("--- DEBUG: Caricamento pagina /creaattestati ---")
    
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    # --- Caricamento dati iniziali (Corsi) ---
    corsi_dal_db = get_corsi_from_db_sync()
    # Mappa {id: nome} per il select
    corsi_options_map = {corso["id"]: corso["nome"] for corso in corsi_dal_db}
    # Mappa {id: ore} per il label dinamico
    corsi_ore_map = {corso["id"]: corso["ore"] for corso in corsi_dal_db}
    print(f"--- DEBUG: Caricati {len(corsi_options_map)} corsi nelle mappe ---")

    # --- Lista per memorizzare i soggetti da formare (in memoria) ---
    # { 'cf_utente': {'item': item_data, 'row': ui_element} }
    soggetti_da_formare = {} 

    # --- Contenuto Pagina ---
    with ui.column().classes('w-full items-center p-4'):
        
        # 1. Bottone Indietro e Titolo
        with ui.row().classes('w-full max-w-5xl items-center'): # Allargato a max-w-5xl
            ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')
            ui.label('Creazione Attestati Massiva').classes('text-3xl font-bold ml-4')

        # 2. Card per Aggiungere Utenti
        with ui.card().classes('w-full max-w-5xl p-4 mt-4'):
            ui.label('1. Aggiungi Soggetti').classes('text-lg font-semibold')
            with ui.row().classes('w-full items-baseline gap-2'):
                # --- (A) DEFINIZIONE ELEMENTI UI ---
                user_search_input = ui.input(label='Cerca Soggetto', placeholder='Nome, Cognome o CF...') \
                    .props('outlined dense').classes('flex-grow')
                
                # Creiamo i bottoni SENZA il gestore on_click
                aggiungi_button = ui.button('Aggiungi').props('color=primary')
                svuota_button = ui.button('Svuota Lista').props('color=red-5 flat')

        # 3. Card per la GRIGLIA INTERATTIVA
        with ui.card().classes('w-full max-w-5xl p-4 mt-4'):
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('2. Compila Corsi e Date').classes('text-lg font-semibold')
                # --- (A) DEFINIZIONE ELEMENTI UI ---
                conteggio_label = ui.label(f"Soggetti in lista: 0").classes('text-sm text-gray-600')

            # Intestazioni Fisse (create una sola volta)
            with ui.grid(columns=7).classes('w-full gap-x-4 gap-y-2 p-2 border-b border-gray-300 items-center'):
                ui.label('Cognome').classes('font-bold')
                ui.label('Nome').classes('font-bold')
                ui.label('Codice Fiscale').classes('font-bold')
                ui.label('Corso').classes('font-bold')
                ui.label('Data').classes('font-bold')
                ui.label('Ore').classes('font-bold')
                ui.label('Azione').classes('font-bold')

            # --- (A) DEFINIZIONE ELEMENTI UI ---
            lista_soggetti_container = ui.column().classes('w-full')
            empty_list_label = ui.label("Nessun soggetto aggiunto.").classes('text-sm italic p-2')

            # --- (B) DEFINIZIONE FUNZIONI ---
            # Ora queste funzioni sono definite prima di essere usate

            def aggiorna_conteggio():
                count = len(soggetti_da_formare)
                print(f"DEBUG: aggiorna_conteggio() chiamato. Conteggio trovato: {count}")
                conteggio_label.set_text(f"Soggetti in lista: {count}")
                empty_list_label.set_visibility(count == 0)

            async def aggiungi_soggetto():
                search_term = user_search_input.value
                print(f"DEBUG: aggiungi_soggetto() chiamato. Termine: '{search_term}'")
                if not search_term:
                    ui.notify("Inserisci un termine di ricerca", color='orange')
                    return

                ui.notify(f"Ricerca di '{search_term}'...", spinner=True)
                user_data = await asyncio.to_thread(get_user_details_from_db_sync, search_term)
                
                if user_data:
                    print(f"DEBUG: Utente Trovato: {user_data['NOME']}")
                    cf = user_data['CODICE_FISCALE']
                    if cf in soggetti_da_formare:
                        print(f"DEBUG: Utente {cf} già in lista.")
                        ui.notify(f"{user_data['NOME']} {user_data['COGNOME']} è già in lista.", color='blue')
                    else:
                        print(f"DEBUG: Aggiunta utente {cf} alla lista.")
                        item_data = {
                            'user': user_data,
                            'course_id': None,
                            'date': None
                        }
                        
                        with lista_soggetti_container:
                            with ui.grid(columns=7).classes('w-full gap-x-4 gap-y-2 py-2 border-b items-center') as riga:
                                ui.label(item_data['user']['COGNOME'])
                                ui.label(item_data['user']['NOME'])
                                ui.label(item_data['user']['CODICE_FISCALE'])
                                
                                # --- CORREZIONE QUI ---
                                # Rimosso 'placeholder' e aggiunto 'label' in .props()
                                ui.select(options=corsi_options_map) \
                                    .bind_value(item_data, 'course_id') \
                                    .props('outlined dense options-dense label="Scegli..."') \
                                    .classes('w-full min-w-[200px]')
                                    
                                ui.date().bind_value(item_data, 'date') \
                                    .props('outlined dense') \
                                    .classes('w-full min-w-[150px]')

                                ui.label() \
                                    .bind_text_from(item_data, 'course_id', 
                                                    lambda course_id: corsi_ore_map.get(course_id, '') if course_id else '') \
                                    .classes('text-center')

                                # Passiamo l'item E la riga (griglia) alla funzione di rimozione
                                ui.button(icon='delete', on_click=lambda i=item_data, r=riga: rimuovi_soggetto(i, r)) \
                                    .props('flat round dense color=red')
                        
                        soggetti_da_formare[cf] = {'item': item_data, 'row': riga}
                        
                        ui.notify(f"Aggiunto: {user_data['NOME']} {user_data['COGNOME']}", color='green')
                        aggiorna_conteggio() 
                    
                    user_search_input.set_value('') 
                else:
                    print(f"DEBUG: Utente non trovato.")
                    ui.notify(f"Nessun utente trovato per '{search_term}'", color='red')
            
            def rimuovi_soggetto(item_to_remove, row_to_remove):
                cf = item_to_remove['user']['CODICE_FISCALE']
                print(f"DEBUG: rimuovi_soggetto() chiamato per {cf}")
                if cf in soggetti_da_formare:
                    row_to_remove.delete()
                    del soggetti_da_formare[cf]
                    ui.notify(f"Rimosso: {item_to_remove['user']['NOME']} {item_to_remove['user']['COGNOME']}", color='orange')
                    aggiorna_conteggio()
                else:
                    print(f"DEBUG: ERRORE RIMOZIONE - {cf} non trovato in dizionario.")

            def svuota_lista():
                print("DEBUG: svuota_lista() chiamata.")
                lista_soggetti_container.clear()
                soggetti_da_formare.clear()
                ui.notify("Lista svuotata", color='blue')
                aggiorna_conteggio()
            
            # --- (C) COLLEGARE GLI EVENTI ---
            # Ora che le funzioni esistono, le colleghiamo ai bottoni
            aggiungi_button.on_click(aggiungi_soggetto)
            svuota_button.on_click(svuota_lista)

            # Aggiorna il conteggio all'inizio
            aggiorna_conteggio()

        # 4. Bottone Generazione Massiva
        async def handle_create_batch_certificate():
            print("DEBUG: handle_create_batch_certificate() CHIAMATO")
            
            if not soggetti_da_formare:
                print("DEBUG: Generazione fallita - 'soggetti_da_formare' è vuoto.")
                ui.notify('Aggiungi almeno un soggetto alla lista!', color='red')
                return
            
            lista_items = [value['item'] for value in soggetti_da_formare.values()]
            print(f"DEBUG: Trovati {len(lista_items)} items da processare.")
            
            if any(not item['course_id'] or not item['date'] for item in lista_items):
                print("DEBUG: Generazione fallita - campi 'course_id' o 'date' mancanti.")
                for i, item in enumerate(lista_items):
                    if not item['course_id']: print(f"  > Item {i} ({item['user']['COGNOME']}) non ha course_id")
                    if not item['date']: print(f"  > Item {i} ({item['user']['COGNOME']}) non ha date")
                ui.notify('Compila tutti i campi "Corso" e "Data" nella griglia!', color='red')
                return
            
            num_soggetti = len(lista_items)
            ui.notify(f'Avvio generazione di {num_soggetti} attestati...', spinner=True, timeout=10000)
            
            generated_files = []
            temp_dir = ""
            
            try:
                temp_dir = tempfile.mkdtemp()
                print(f"Creata cartella temporanea: {temp_dir}")
                
                for item in lista_items:
                    user_data = item['user']
                    course_id = item['course_id']
                    course_name = corsi_options_map[course_id] 
                    course_date = item['date']
                    course_hours = corsi_ore_map.get(course_id, 0) 

                    year_str = datetime.strptime(course_date, '%Y-%m-%d').strftime('%Y')
                    safe_course_name = re.sub(r'[^\w-]', '_', course_name) 

                    data_map = {
                        "{{CODICE}}": user_data.get("CODICE_FISCALE", ""),
                        "{{COGNOME}}": user_data.get("COGNOME", ""),
                        "{{NOME}}": user_data.get("NOME", ""),
                        "{{DATA NASCITA}}": user_data.get("DATA_NASCITA"),
                        "{{LUOGO NASCITA}}": user_data.get("LUOGO_NASCITA", ""),
                        "{{SOCIETA}}": user_data.get("SOCIETA", ""),
                        "{{NOME CORSO}}": course_name,
                        "{{PERIODO SVOLGIMENTO}}": course_date,
                        "{{ORE CORSO}}": course_hours, 
                    }
                    
                    target_output_dir = os.path.join(temp_dir, year_str, safe_course_name)
                    await run.io_bound(os.makedirs, target_output_dir, exist_ok=True)
                    
                    output_file = await asyncio.to_thread(
                        generate_certificate_sync, data_map, "modello.docx", target_output_dir
                    )
                    generated_files.append(output_file) 
                    
                    cf_soggetto = user_data["CODICE_FISCALE"]
                    await asyncio.to_thread(
                        save_attestato_to_db_sync, cf_soggetto, course_id, course_date
                    )
                
                today_str = datetime.now().strftime('%Y-%m-%d')
                zip_filename = f"attestati_generati_{today_str}.zip"
                
                zip_path = await asyncio.to_thread(
                    generate_zip_sync, generated_files, temp_dir, zip_filename
                )
                
                ui.download(zip_path)
                ui.notify(f'Generato {zip_filename} con {num_soggetti} attestati!', color='green')
                
                svuota_lista()

            except FileNotFoundError:
                ui.notify('ERRORE: "modello.docx" non trovato!', color='red')
            except Exception as e:
                ui.notify(f'Errore sconosciuto: {e}', color='red')
                print(f"Errore generazione batch: {e}")
            finally:
                if temp_dir and os.path.exists(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                        print(f"Cartella temporanea {temp_dir} e sottocartelle eliminate.")
                    except Exception as e:
                        print(f"Errore eliminazione cartella temp: {e}")
                
                await run.io_bound(lambda: asyncio.sleep(10)) 
                if 'zip_path' in locals() and os.path.exists(zip_path):
                     print(f"Pulizia file zip: {zip_path}")
                     try:
                         os.remove(zip_path)
                     except Exception as e:
                         print(f"Errore pulizia file zip: {e}")


        ui.button(f'Genera Attestati', on_click=handle_create_batch_certificate) \
            .classes('w-full max-w-5xl mt-6 py-4 text-lg') \
            .props('color=primary icon=download') \
            .bind_text_from(conteggio_label, 'text', lambda c: f"Genera Attestati per ({c.split(': ')[1]}) persone")


# --- NUOVA PAGINA /gestioneutenti ---
@ui.page('/gestioneutenti')
def gestioneutenti_page():
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    with ui.column().classes('w-full items-center p-4'):
        ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')
        ui.label('Questa è la Pagina Gestione Utenti (Protetta)').classes('text-2xl')
        
# --- NUOVA PAGINA /gestioneenti ---
@ui.page('/gestioneenti')
def gestioneenti_page():
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    with ui.column().classes('w-full items-center p-4'):
        ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')
        ui.label('Questa è la Pagina Gestione Enti (Protetta)').classes('text-2xl')
    

if __name__ in {"__main__", "__mp_main__"}:
    print("Avvio WorkSafeManager")
    
    # CORREZIONE: Impostato reload=True per lo sviluppo
    ui.run(
        title="WorkSafeManager",
        reload=True, 
        storage_secret='d5b2d2675cee4718e56b89aa0d471dd62cceee996581c913f43a361dbedd67eb'
    )