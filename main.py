import json
import fdb
import bcrypt
import asyncio
from nicegui import ui, app  # Importa 'app' per accedere allo storage

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

# --- SYNC DB QUERY ---
def check_credentials_sync(username):
    try:
        conn = fdb.connect(
            host=config['host'],
            database=config['database'],
            user=config['user'],
            password=config['password'],
            port=config.get('port', 3050)
        )
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
        print(f"Errore DB: {e}")
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


# --- NUOVA PAGINA /creaattestati ---
@ui.page('/creaattestati')
def creaattestati_page():
    
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    # --- Contenuto Pagina Creazione Attestati ---
    with ui.column().classes('w-full items-center p-4'):
        
        # 1. Bottone Indietro (in alto)
        with ui.row().classes('w-full max-w-lg justify-start mb-4'):
            ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')

        # 2. Titolo
        ui.label('Creazione Attestati').classes('text-3xl font-bold mb-6 text-center')

        # 3. Card con i campi
        with ui.card().classes('w-full max-w-lg p-6'):
            with ui.column().classes('w-full gap-4'): # 'gap-4' aggiunge spazio tra gli elementi
                
                # Campo Ricerca Utenti
                ui.input(label='Cerca Utente', placeholder='Nome, Cognome o CF...') \
                    .props('outlined clearable') \
                    .classes('w-full') \
                    .on('keydown.enter', lambda e: ui.notify(f'Ricerca utente: {e.sender.value}'))

                # Mock data per i corsi
                corsi_options = [
                    'Corso Sicurezza Base (4 ore)',
                    'Corso Antincendio Rischio Basso (4 ore)',
                    'Corso Antincendio Rischio Medio (8 ore)',
                    'Corso Primo Soccorso (12 ore)',
                    'Corso RLS (32 ore)',
                    'Aggiornamento Sicurezza (6 ore)',
                    'Aggiornamento Primo Soccorso (4 ore)'
                ]
                
                # Campo Scrollabile/Ricercabile Corsi
                ui.select(label='Seleziona Corso', options=corsi_options, with_input=True) \
                    .props('outlined') \
                    .classes('w-full')

                # --- CORREZIONE: 'label' spostato dentro .props() ---
                ui.date() \
                    .props('outlined mask="####-##-##" today-btn label="Data del Corso"') \
                    .classes('w-full')
                
                # Bottone Creazione
                ui.button('Crea Attestato', on_click=lambda: ui.notify('Creazione attestato...')) \
                    .classes('w-full mt-4 py-3')

# --- NUOVA PAGINA /gestioneutenti ---
@ui.page('/gestioneutenti')
def gestioneutenti_page():
    
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    # --- Contenuto centrato ---
    with ui.column().classes('w-full items-center mt-8'):
        ui.label('Questa è la Pagina Gestione Utenti (Protetta)').classes('text-2xl mb-4')
        ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'))
        
# --- NUOVA PAGINA /gestioneenti ---
@ui.page('/gestioneenti')
def gestioneenti_page():
    
    # --- CONTROLLO ACCESSO ---
    if not app.storage.user.get('authenticated', False):
        ui.notify("Accesso negato. Effettua il login.", color="red")
        ui.navigate.to('/') 
        return
        
    # --- Contenuto centrato ---
    with ui.column().classes('w-full items-center mt-8'):
        ui.label('Questa è la Pagina Gestione Enti (Protetta)').classes('text-2xl mb-4')
        ui.button('Torna alla Dashboard', on_click=lambda: ui.navigate.to('/dashboard'))
    

if __name__ in {"__main__", "__mp_main__"}:
    print("Avvio WorkSafeManager")
    
    ui.run(
        title="WorkSafeManager",
        reload=True, 
        storage_secret='d5b2d2675cee4718e56b89aa0d471dd62cceee996581c913f43a361dbedd67eb'
    )