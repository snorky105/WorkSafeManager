import json
import fdb
import bcrypt
import asyncio
from nicegui import ui, app, run  # Importa 'app' e 'run'
import os  # Necessario per i percorsi dei file
from docx import Document  # Importa la libreria per i .docx
from datetime import datetime, date # Per formattare le date
import tempfile  # Per creare cartelle temporanee
import shutil  # Per eliminare le cartelle temporanee
import zipfile  # Per creare il file .zip
import re # Per pulire i nomi delle cartelle

# --- LOG DI AVVIO ---
print("--- DEBUG: AVVIO WorkSafeManager (Versione POPUP RICERCA) ---")

# --- LOAD CONFIG ---
try:
    with open('config.json', 'r') as f:
        config = json.load(f)
    with open('queries.json', 'r') as f:
        queries = json.load(f)
except FileNotFoundError:
    print("ERRORE: Config file non trovati.")
    exit()

# --- DB HELPERS ---
def get_db_connection():
    return fdb.connect(
        host=config['host'], database=config['database'],
        user=config['user'], password=config['password'],
        port=config.get('port', 3050), charset='UTF8'
    )

def get_user_details_from_db_sync(search_term: str):
    print(f"DEBUG: Ricerca DB per: {search_term}")
    terms = search_term.upper().split()
    if not terms: return []

    sql = """
        SELECT s.CODICE_FISCALE, s.COGNOME, s.NOME, s.DATA_NASCITA, s.LUOGO_NASCITA, e.DESCRIZIONE AS SOCIETA
        FROM T_SOGGETTI s LEFT JOIN T_ENTI e ON s.ID_ENTE_FK = e.ID_ENTE
    """
    if len(terms) == 1:
        p = f"{terms[0]}"
        sql += " WHERE (UPPER(s.COGNOME) STARTING WITH ?) OR (UPPER(s.NOME) STARTING WITH ?) OR (UPPER(s.CODICE_FISCALE) STARTING WITH ?)"
        params = [p, p, p]
    else:
        p1, p2 = f"{terms[0]}", f"{terms[1]}"
        sql += " WHERE ((UPPER(s.COGNOME) STARTING WITH ? AND UPPER(s.NOME) STARTING WITH ?) OR (UPPER(s.COGNOME) STARTING WITH ? AND UPPER(s.NOME) STARTING WITH ?))"
        params = [p1, p2, p2, p1]
        
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
        print(f"Errore DB: {e}")
        return []

def get_corsi_from_db_sync():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID_CORSO, NOME_CORSO, ORE_DURATA FROM T_CORSI ORDER BY NOME_CORSO")
        rows = cur.fetchall()
        conn.close()
        return [{"id": r[0], "nome": r[1], "ore": r[2]} for r in rows]
    except Exception as e:
        print(f"Errore DB Corsi: {e}")
        return []

def save_attestato_to_db_sync(cf, id_corso, data_str):
    print(f"DEBUG: Salvataggio {cf}...")
    try:
        dt = None
        if re.search(r'\d{4}-\d{2}-\d{2}', data_str):
             dt = datetime.strptime(re.search(r'\d{4}-\d{2}-\d{2}', data_str).group(0), '%Y-%m-%d').date()
        elif re.search(r'\d{2}/\d{2}/\d{4}', data_str):
             dt = datetime.strptime(re.search(r'\d{2}/\d{2}/\d{4}', data_str).group(0), '%d/%m/%Y').date()
        
        data_val = dt if dt else date.today()
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO T_ATTESTATI (ID_SOGGETTO_FK, ID_CORSO_FK, DATA_SVOLGIMENTO) VALUES (?, ?, ?)", (cf, id_corso, data_val))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Err Salvataggio: {e}")
        return False

# --- DOCX GENERATION ---
def generate_certificate_sync(data_map, template_file="modello.docx", output_dir=None):
    if not os.path.exists(template_file): raise FileNotFoundError("Template mancante")
    doc = Document(template_file)
    local_map = data_map.copy()
    
    dob = local_map.get("{{DATA NASCITA}}")
    if isinstance(dob, (datetime, date)): local_map["{{DATA NASCITA}}"] = dob.strftime('%d/%m/%Y')

    for p in doc.paragraphs:
        for k, v in local_map.items():
            if k in p.text: p.text = p.text.replace(k, str(v if v else ''))
            
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                for p in c.paragraphs:
                    for k, v in local_map.items():
                         if k in p.text: p.text = p.text.replace(k, str(v if v else ''))
                         
    fname = f"attestato_{re.sub(r'\W', '', local_map.get('{{COGNOME}}',''))}_{re.sub(r'\W', '', local_map.get('{{NOME}}',''))}.docx"
    out_path = os.path.join(output_dir, fname) if output_dir else fname
    doc.save(out_path)
    return out_path

def generate_zip_sync(files, base, name="attestati.zip"):
    with zipfile.ZipFile(name, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files: z.write(f, arcname=os.path.relpath(f, base))
    return name

# --- APP ---
@ui.page('/')
def login_page():
    if app.storage.user.get('authenticated', False):
        ui.navigate.to('/dashboard') 
        return
        
    with ui.column().classes('absolute-center w-full max-w-sm items-center'):
        ui.label("Benvenuto in WorkSafeManager").classes("text-3xl font-bold mb-8 text-center")
        with ui.card().style("padding: 40px;").classes("w-full"):
            username_input = ui.input("Utente").props("outlined").classes("w-full mb-2")
            password_input = ui.input("Password").props("outlined type=password").classes("w-full mb-4")

            async def on_login_click():
                user = username_input.value.strip()
                pwd = password_input.value.strip()
                if not user or not pwd:
                    ui.notify("Inserisci username e password!", color="red")
                    return
                valid = await check_credentials(user, pwd)
                if valid:
                    app.storage.user['authenticated'] = True
                    app.storage.user['username'] = user 
                    ui.notify("Login riuscito!", color="green")
                    ui.navigate.to('/dashboard')
                else:
                    ui.notify("Credenziali errate", color="red")
            ui.button("Entra", on_click=on_login_click).classes("w-full mt-4")

@ui.page('/dashboard')
def dashboard_page():
    if not app.storage.user.get('authenticated', False):
        ui.navigate.to('/') 
        return 

    username = app.storage.user.get('username', 'Utente')
    ui.label(f"Dashboard - Benvenuto, {username}").classes("text-3xl font-bold mb-8 text-center")
    
    with ui.expansion('Creazione Attestati', icon='explore').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la creazione degli attestati.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/creaattestati')).classes('m-4')
    
    with ui.expansion('Gestione Utenti', icon='people').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la gestione Utenti.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/gestioneutenti')).classes('m-4')
        
    with ui.expansion('Gestione Enti', icon='home').classes('max-w-2xl mx-auto mb-4 shadow-md rounded-lg'):
        ui.label('Accedi alla sezione per la gestione Enti.').classes('p-4')
        ui.button('Entra', on_click=lambda: ui.navigate.to('/gestioneenti')).classes('m-4')

    def logout_click():
        app.storage.user['authenticated'] = False
        app.storage.user.pop('username', None) 
        ui.notify("Logout effettuato", color="blue")
        ui.navigate.to('/')
        
    ui.button("Logout", on_click=logout_click).classes("bg-red-500 text-white mx-auto block mt-8 mb-8")

@ui.page('/creaattestati')
def creaattestati_page():
    print("--- DEBUG: Page Load /creaattestati ---")
    
    if not app.storage.user.get('authenticated', False):
         ui.navigate.to('/')
         return

    # Caricamento dati
    corsi_raw = get_corsi_from_db_sync()
    corsi_opts = {c["id"]: c["nome"] for c in corsi_raw}
    corsi_ore = {c["id"]: c["ore"] for c in corsi_raw}
    
    soggetti = {} # Dizionario locale per lo stato

    # --- NUOVO DIALOGO DI RICERCA ---
    search_dialog = ui.dialog()
    with search_dialog, ui.card().classes('w-full max-w-lg'):
        ui.label('Cerca e Aggiungi Soggetto').classes('text-xl font-bold mb-2')
        
        with ui.row().classes('w-full gap-2'):
            # Nota: input definito qui dentro
            search_input = ui.input(label='Nome, Cognome o CF...').classes('flex-grow').props('outlined')
            # Il bottone Cerca triggera la ricerca
            search_btn = ui.button('Cerca').props('color=primary')
            
        # Area per i risultati (se omonimi)
        search_results_area = ui.column().classes('w-full mt-2')
        
        ui.button('Chiudi', on_click=search_dialog.close).props('flat color=grey').classes('ml-auto')


    with ui.column().classes('w-full items-center p-8'):
        
        with ui.row().classes('w-full items-center mb-4'): 
            ui.button('Torna', on_click=lambda: ui.navigate.to('/dashboard'), icon='arrow_back').props('flat round')
            ui.label('Creazione Attestati Massiva').classes('text-3xl ml-4')
        
        # --- FUNZIONE REFRESH GRIGLIA ---
        @ui.refreshable
        def render_lista_soggetti():
            if not soggetti:
                ui.label("Nessun soggetto in lista.").classes('text-sm italic p-4 text-gray-500')
                return
            
            # Loop sui dati
            for cf, item in soggetti.items():
                u_data = item['user']
                # Layout griglia proporzionato
                with ui.grid().style('grid-template-columns: 1fr 1fr 1fr 3fr 1.5fr 0.6fr 0.5fr; width: 100%; gap: 10px; align-items: center; border-bottom: 1px solid #eee; padding: 5px;'):
                    ui.label(u_data['COGNOME'])
                    ui.label(u_data['NOME'])
                    ui.label(u_data['CODICE_FISCALE']).classes('text-xs')
                    
                    ore_wdg = ui.number().props('outlined dense').bind_value(item, 'ore')
                    
                    def on_course_change(e, it=item, ow=ore_wdg):
                        it['ore'] = corsi_ore.get(e.value)
                        ow.update()

                    ui.select(options=corsi_opts, on_change=on_course_change).props('outlined dense label="Corso"').bind_value(item, 'cid').classes('w-full')
                    ui.input().props('outlined dense').bind_value(item, 'per').classes('w-full')
                    
                    # Ricrea il widget ore nella posizione corretta
                    ore_wdg.delete() 
                    ore_wdg = ui.number().props('outlined dense').bind_value(item, 'ore')

                    ui.button(icon='delete', on_click=lambda _, c=cf: rimuovi_soggetto(c)).props('flat round dense color=red')

        # --- LOGICA DI STATO ---
        def process_user_addition(u_data):
            cf = u_data['CODICE_FISCALE']
            if cf in soggetti:
                ui.notify("Utente già presente in lista!", color='orange')
                return

            item = {'user': u_data, 'cid': None, 'per': None, 'ore': None}
            soggetti[cf] = item
            
            render_lista_soggetti.refresh()
            count_label.set_text(f"Totale: {len(soggetti)}")
            ui.notify(f"Aggiunto: {u_data['COGNOME']}", color='green')

        def rimuovi_soggetto(cf):
            if cf in soggetti:
                del soggetti[cf]
                render_lista_soggetti.refresh()
                count_label.set_text(f"Totale: {len(soggetti)}")

        def svuota_lista():
            soggetti.clear()
            render_lista_soggetti.refresh()
            count_label.set_text("Totale: 0")
            ui.notify("Lista svuotata", color='blue')

        def open_search_ui():
            search_input.value = ""
            search_results_area.clear()
            search_dialog.open()

        async def perform_search():
            term = search_input.value
            if not term: return
            
            ui.notify("Ricerca...", color='orange', spinner=True)
            res = await asyncio.to_thread(get_user_details_from_db_sync, term)
            
            search_results_area.clear()
            
            if not res:
                ui.notify("Nessuno trovato.", color='red')
                with search_results_area:
                    ui.label("Nessun risultato.").classes('text-red italic')
                return
            
            if len(res) == 1:
                process_user_addition(res[0])
                search_dialog.close()
            else:
                # Più risultati: mostra lista nel dialog
                with search_results_area:
                    ui.label("Trovati più utenti:").classes('font-bold mt-2')
                    with ui.list().props('bordered separator dense'):
                        for u in res:
                            dob = u['DATA_NASCITA'].strftime('%d/%m/%Y') if u['DATA_NASCITA'] else "?"
                            lbl = f"{u['COGNOME']} {u['NOME']} ({dob})"
                            
                            with ui.item().props('clickable').on('click', lambda e, x=u: (process_user_addition(x), search_dialog.close())):
                                with ui.item_section():
                                    ui.item_label(lbl)
                                    ui.item_label(u['CODICE_FISCALE']).props('caption')

        # Colleghiamo il bottone Cerca dentro al dialog
        search_btn.on_click(perform_search)
        # Colleghiamo Enter sul campo input
        search_input.on('keydown.enter', perform_search)


        # --- BARRA COMANDI ---
        with ui.row().classes('w-full justify-between items-center mt-2 mb-2'):
             ui.label('Lista Destinatari').classes('text-xl font-bold')
             with ui.row():
                 ui.button('Aggiungi Soggetto', on_click=open_search_ui, icon='person_add').props('color=primary')
                 ui.button('Svuota Lista', on_click=svuota_lista, icon='delete_sweep').props('color=red flat')

        # --- GRIGLIA CONTAINER ---
        with ui.column().classes('w-full p-4 border rounded shadow-md'):
            count_label = ui.label("Totale: 0").classes('ml-auto text-sm text-gray-500')
            
            # Header
            with ui.grid().style('grid-template-columns: 1fr 1fr 1fr 3fr 1.5fr 0.6fr 0.5fr; width: 100%; font-weight: bold; border-bottom: 2px solid #ccc; padding-bottom: 5px;'):
                ui.label('Cognome'); ui.label('Nome'); ui.label('CF'); ui.label('Corso'); ui.label('Periodo'); ui.label('Ore'); ui.label('')
            
            # Render iniziale
            render_lista_soggetti()

        # --- GENERAZIONE ---
        async def on_generate():
            items = list(soggetti.values())
            if not items:
                ui.notify("Lista vuota", color='red')
                return
            if any(not x['cid'] or not x['per'] for x in items):
                ui.notify("Compila Corsi e Periodi!", color='red')
                return

            ui.notify("Generazione in corso...", spinner=True)
            try:
                tmp = tempfile.mkdtemp()
                files = []
                for it in items:
                    u = it['user']
                    c_name = corsi_opts[it['cid']]
                    safe_c = re.sub(r'\W', '_', c_name)
                    safe_az = re.sub(r'\W', '_', u.get('SOCIETA', 'NoAz'))
                    
                    t_path = os.path.join(tmp, safe_c, safe_az)
                    os.makedirs(t_path, exist_ok=True)
                    
                    d_map = {
                        "{{COGNOME}}": u['COGNOME'], "{{NOME}}": u['NOME'],
                        "{{CODICE}}": u['CODICE_FISCALE'],
                        "{{DATA NASCITA}}": u['DATA_NASCITA'],
                        "{{LUOGO NASCITA}}": u['LUOGO_NASCITA'],
                        "{{SOCIETA}}": u['SOCIETA'],
                        "{{NOME CORSO}}": c_name,
                        "{{PERIODO SVOLGIMENTO}}": it['per'],
                        "{{ORE CORSO}}": it['ore']
                    }
                    f = await asyncio.to_thread(generate_certificate_sync, d_map, "modello.docx", t_path)
                    files.append(f)
                    await asyncio.to_thread(save_attestato_to_db_sync, u['CODICE_FISCALE'], it['cid'], it['per'])

                z_name = f"attestati_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
                z_path = await asyncio.to_thread(generate_zip_sync, files, tmp, z_name)
                
                ui.download(z_path)
                ui.notify("Fatto!", color='green')
                soggetti.clear()
                render_lista_soggetti.refresh()
                count_label.set_text("Totale: 0")
                
            except Exception as e:
                ui.notify(f"Errore: {e}", color='red')
                print(f"ERR: {e}")
            finally:
                if os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)
                await asyncio.sleep(5)
                if 'z_path' in locals() and os.path.exists(z_path): os.remove(z_path)

        ui.button("Genera e Scarica", on_click=on_generate).classes('w-full mt-6').props('color=green size=lg')

@ui.page('/gestioneutenti')
def gestioneutenti_page():
    if not app.storage.user.get('authenticated', False):
        ui.navigate.to('/') 
        return
    with ui.column().classes('w-full items-center p-4'):
        ui.label('Gestione Utenti').classes('text-2xl')
        
@ui.page('/gestioneenti')
def gestioneenti_page():
    if not app.storage.user.get('authenticated', False):
        ui.navigate.to('/') 
        return
    with ui.column().classes('w-full items-center p-4'):
        ui.label('Gestione Enti').classes('text-2xl')

if __name__ in {"__main__", "__mp_main__"}:
    print("Avvio WorkSafeManager")
    ui.run(
        title="WorkSafeManager",
        reload=True, 
        storage_secret='d5b2d2675cee4718e56b89aa0d471dd62cceee996581c913f43a361dbedd67eb'
    )