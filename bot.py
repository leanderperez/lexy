import os
import sqlite3
import asyncio
import re
import html
from datetime import datetime, time
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from google import genai
from google.genai import types
import edge_tts

# 1. Cargar el archivo .env PRIMERO
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not all([GEMINI_API_KEY, TELEGRAM_TOKEN]):
    raise ValueError("ERROR: Faltan API Keys en el archivo .env (Gemini o Telegram).")

# Cliente de IA (Solo Gemini)
client_gemini = genai.Client(api_key=GEMINI_API_KEY)

# --- CONFIGURACIÓN DE BASE DE DATOS ---
DB_FILE = 'vocabulario.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS palabras (id INTEGER PRIMARY KEY AUTOINCREMENT, palabra TEXT UNIQUE, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (chat_id INTEGER PRIMARY KEY, ultimo_contacto TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS historial (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, rol TEXT, contenido TEXT, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def guardar_palabra(palabra):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.cursor().execute("INSERT INTO palabras (palabra) VALUES (?)", (palabra,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass 
    conn.close()

def registrar_interaccion(chat_id, rol, contenido):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO usuarios (chat_id, ultimo_contacto) VALUES (?, CURRENT_TIMESTAMP)", (chat_id,))
    cursor.execute("INSERT INTO historial (chat_id, rol, contenido) VALUES (?, ?, ?)", (chat_id, rol, contenido))
    conn.commit()
    conn.close()

def obtener_palabras_recientes(limite=5):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT palabra FROM palabras ORDER BY fecha DESC LIMIT ?", (limite,))
    palabras = [fila[0] for fila in cursor.fetchall()]
    conn.close()
    return palabras

def recuperar_historial(chat_id, limite=20):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT rol, contenido FROM historial WHERE chat_id = ? ORDER BY fecha DESC LIMIT ?", (chat_id, limite))
    filas = cursor.fetchall()
    conn.close()
    
    historial = []
    for rol, contenido in reversed(filas):
        historial.append(types.Content(role=rol, parts=[types.Part.from_text(text=contenido)]))
    return historial

# --- GESTIÓN DE ESTADOS Y SESIONES ---
user_states = {}
user_sessions_gemini = {}

# --- PROMPTS ---
PROMPT_ENSENANZA="""Eres Lexy mi tutora nativa de chino mandarín y compañera de estudio. Mi objetivo es aprender caracteres de forma práctica y natural. 
REGLA VITAL: NO inicies la lección ni sugieras palabras por tu cuenta. ESPERA siempre a que yo te envíe la palabra o el carácter que quiero estudiar.
Una vez que yo te envíe la palabra, seguiremos este método estructurado:
1. Análisis del Carácter: Explica brevemente el significado del carácter, su componente visual o radical, y su lógica básica.
2. Regla de Tres (Usos Clave): Muestra entre 2 y 3 palabras compuestas o estructuras hipercomunes en las que este carácter sea protagonista en la vida diaria. Incluye caracteres, Pinyin y traducción.
3. El Reto de Chat (Práctica Activa): Plantéame un escenario cotidiano real y pídeme que redacte una frase corta usando la palabra nueva combinada con lo que ya sé. Dame pistas claras para guiar mi respuesta.
4. Feedback: Cuando yo responda al reto, valida mi frase. Si cometo un error sutil de gramática o naturalidad, corrígelo de forma directa y amable, explicando el porqué, y muestra cómo lo diría un nativo.
5. El Contador del Bloque: Mantén un registro visual al final de cada respuesta. Vamos a agrupar las palabras de 5 en 5. Cuando completemos un bloque de 5 palabras, detén el avance y hazme un examen/repaso general usando todas las palabras de ese bloque en un diálogo integrado.
"""

PROMPT_EVALUACION = """Eres Lexy, actua como mi profesora de chino mandarín nativo. 
Voy a escribir oraciones creadas por mí. No las traduzcas directamente. Evalúa si la gramática es correcta y si suena natural para un nativo. 
Si hay errores, corrígelos, muéstrame el Pinyin y explícame la regla gramatical en español de forma simple.
"""

PROMPT_DIALOGO_BASE = """Eres Lexy mi compañera de intercambio de idiomas nativa de China. 
Hablar contigo me sirve para aprender y practicar vocabulario del nivel HSK1 3.0 y HSK2 3.0 inicial. Sé proactiva y busca enseñarme palabras nuevas de forma natural.
Uso para estudiar Hello Chinese, asi que puedes buscar temas de conversación relacionados con la vida diaria, comida, cultura, viajes, gustos, etc.
Palabras a practicar hoy: {palabras_objetivo}.
Evalúa en mi respuesta si la gramática es correcta y si suena natural para un nativo. Si hay errores corrígelos de forma directa y explícame cómo lo diría un nativo.

REGLA DE FORMATO ESTRICTA Y OBLIGATORIA: 
Tu respuesta debe tener SIEMPRE esta estructura exacta separada por saltos de línea (nunca añadas introducciones antes):
<tts>Respuesta en caracteres chinos (solo caracteres y puntuación)</tts>
Respuesta en Pinyin
Traducción al español
"""

PROMPT_EXAMEN_HSK = """Eres un examinador oficial de las pruebas HSK. Mi meta es certificarme en HSK 3.
Estamos en una simulación de examen oficial (Mock Test). Revisa el historial para NO repetir preguntas que ya me hayas hecho.
Realiza preguntas de lectura y gramática (opción múltiple o rellenar espacios en blanco). 
Haz UNA sola pregunta a la vez, espera mi respuesta, corrígela y haz la siguiente. NO uses etiquetas <tts>.
"""

PROMPT_EXAMEN_HSKK = """Eres un examinador oficial del test oral HSKK. Mi meta es certificarme. 
Tienes 3 etapas. Alterna entre ellas. Haz UNA SOLA actividad a la vez y evalúa mi pronunciación.
1. REPETIR AUDIO: Envíame la etiqueta <hskk_audio>frase en caracteres chinos</hskk_audio>.
2. LEER TEXTO: Envíame un texto (solo en caracteres chinos) y exígeme que lo lea en voz alta.
3. DESCRIBIR IMAGEN: Envíame la etiqueta <hskk_img>Una descripción de 3 palabras en inglés de un escenario común, ej: cat eating fish</hskk_img> y pídeme que te describa la imagen que me acabas de enviar en voz alta.
IMPORTANTE: Evalúa mis respuestas auditivas (Mensaje de Voz HSKK) comparando mi pronunciación con el texto correcto o evaluando si la descripción de la imagen es coherente.
"""

PROMPT_NOTICIAS = """Busca en Google ÚNICAMENTE los titulares de las 2 noticias más importantes de HOY en China. 
REGLA DE AHORRO DE TOKENS: NO abras ni analices artículos completos. Limítate a leer los resúmenes cortos (snippets) de la página de resultados de búsqueda.
Adapta esos 2 titulares para un estudiante de chino, usando EXCLUSIVAMENTE vocabulario muy básico (HSK 1 y 2).

Usa el formato estricto (separa cada noticia por saltos de línea):
<tts>Caracteres chinos del titular</tts>
Pinyin
Traducción al español"""

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    registrar_interaccion(chat_id, "user", "/start")
    mensaje = (
        "Usa /profesora para modo enseñanza\n"
        "Usa /evaluar para evaluar oraciones\n"
        "Usa /amiga para modo conversación libre\n"
        "Usa /noticias para leer actualidad en HSK 2\n"
        "Usa /examen para simular prueba HSK/HSKK\n"
        "Usa /reiniciar para borrar el historial de la conversación\n"
    )
    await update.message.reply_text(mensaje)

async def reiniciar_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historial WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    
    if user_id in user_sessions_gemini:
        del user_sessions_gemini[user_id]
        
    if user_id in user_states:
        user_states[user_id] = 'dialogo'
        
    await update.message.reply_text("🧹 <b>¡Historial borrado!</b>\n\nHe limpiado mi memoria de nuestra conversación anterior. ¿De qué quieres que hablemos ahora?", parse_mode='HTML')

async def set_modo_ensenanza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'ensenanza'
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_ENSENANZA})
    await update.message.reply_text("📚 Modo Enseñanza activado. Esperando tus palabras...")

async def set_modo_evaluacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'evaluacion'
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_EVALUACION})
    await update.message.reply_text("📝 Modo Evaluación activado.")

async def set_modo_dialogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_states[user_id] = 'dialogo'
    
    prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo=", ".join(obtener_palabras_recientes(5)))
    historial = recuperar_historial(chat_id, limite=10)
    
    user_sessions_gemini[user_id] = client_gemini.chats.create(
        model='gemini-3.5-flash-lite',
        history=historial,
        config={'system_instruction': prompt_dinamico}
    )
    await update.message.reply_text("🗣️ Modo Conversación activado. ¡Hablemos!")

async def set_modo_examen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'esperando_examen'
    await update.message.reply_text("📝 ¿Qué examen quieres practicar hoy? Responde con *HSK* (escrito) o *HSKK* (oral).", parse_mode='Markdown')

async def enviar_noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    await update.message.reply_text("📰 Buscando titulares de China (modo ahorro de tokens)...")

    try:
        # Petición a Gemini con búsqueda integrada (una sola llamada)
        resp_busqueda = client_gemini.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=PROMPT_NOTICIAS,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.3 # Temperatura baja para que sea directo y no divague
            )
        )
        texto_salida = resp_busqueda.text
        registrar_interaccion(chat_id, "model", texto_salida)

        # Audio y Envío
        await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
        output_audio_path = f"news_{chat_id}.mp3"
        matches = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        texto_para_audio = " ".join(matches).replace('*', '').strip() if matches else texto_salida.replace('*', '')
        
        if texto_para_audio:
            tts = edge_tts.Communicate(texto_para_audio, voice="zh-CN-XiaoxiaoNeural", rate="-25%")
            await tts.save(output_audio_path)
            with open(output_audio_path, "rb") as audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)
            os.remove(output_audio_path)

        # Limpiar texto para Telegram
        texto_seguro = html.escape(texto_salida.replace('<tts>', '').replace('</tts>', '').strip())
        await context.bot.send_message(chat_id=chat_id, text=texto_seguro, parse_mode='HTML')

    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
             await update.message.reply_text("⏳ <b>Límite alcanzado:</b> Google está procesando mucha información ahora mismo y hemos chocado con el límite de tokens gratuito por minuto. Por favor, espera unos 60 segundos y vuelve a intentarlo.", parse_mode='HTML')
        else:
             await update.message.reply_text(f"Hubo un error con las noticias: {error_msg}")

# --- PROCESAMIENTO CENTRAL ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False, texto_original=""):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_mode = user_states.get(user_id, 'dialogo')
    
    if current_mode == 'esperando_examen':
        eleccion = texto_original.strip().upper()
        if eleccion == 'HSK':
            user_states[user_id] = 'examen_hsk'
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': PROMPT_EXAMEN_HSK})
            await update.message.reply_text("Iniciando simulacro HSK...")
            input_data = "¡Empecemos el examen HSK!"
            current_mode = 'examen_hsk'
        elif eleccion == 'HSKK':
            user_states[user_id] = 'examen_hskk'
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': PROMPT_EXAMEN_HSKK})
            await update.message.reply_text("Iniciando simulacro HSKK...")
            input_data = "¡Empecemos el examen HSKK!"
            current_mode = 'examen_hskk'
        else:
            await update.message.reply_text("Por favor, responde solo 'HSK' o 'HSKK'.")
            return

    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    texto_salida = ""

    try:
        # Pre-procesamiento de AUDIO
        if is_audio:
            if current_mode in ['dialogo', 'examen_hsk']:
                resp_trans = client_gemini.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=[input_data, "Transcribe este audio. Devuelve SOLO el texto transcrito en chino, sin explicaciones ni comillas."]
                )
                instruccion = resp_trans.text.strip()
            else:
                instruccion = [input_data, "Evalúa la pronunciación y responde al reto."]
        else:
            instruccion = input_data

        # Registrar el mensaje del usuario
        texto_a_guardar = instruccion if isinstance(instruccion, str) else "[Mensaje de Voz HSKK]"
        registrar_interaccion(chat_id, "user", texto_a_guardar)

        # Configurar sesión si no existe
        if user_id not in user_sessions_gemini:
            # Fallback a diálogo por defecto si se perdió la sesión
            prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo="")
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': prompt_dinamico})

        # Inyección de recordatorio para el modo diálogo
        if current_mode == 'dialogo' and isinstance(instruccion, str):
             instruccion += "\n\n(Regla obligatoria: Responde usando estrictamente el formato de 3 líneas empezando con <tts>Caracteres chinos</tts>)"

        # Enviar petición a Gemini
        respuesta = user_sessions_gemini[user_id].send_message(instruccion)
        texto_salida = respuesta.text

        if not texto_salida:
            await update.message.reply_text("Corte de conexión. ¿Puedes repetirlo?")
            return
            
        registrar_interaccion(chat_id, "model", texto_salida)

        # --- POST-PROCESAMIENTO HSKK (Imágenes y Audios) ---
        match_hskk_img = re.search(r'<hskk_img>(.*?)</hskk_img>', texto_salida)
        if match_hskk_img:
            prompt_img = match_hskk_img.group(1).replace(" ", "%20")
            url_img = f"https://image.pollinations.ai/prompt/{prompt_img}?width=800&height=600&nologo=true"
            await context.bot.send_photo(chat_id=chat_id, photo=url_img)
            texto_salida = re.sub(r'<hskk_img>.*?</hskk_img>', '', texto_salida)

        match_hskk_audio = re.search(r'<hskk_audio>(.*?)</hskk_audio>', texto_salida)
        texto_para_audio = None
        if match_hskk_audio:
            texto_para_audio = match_hskk_audio.group(1)
            texto_salida = re.sub(r'<hskk_audio>.*?</hskk_audio>', '', texto_salida)

        # --- GENERACIÓN DE AUDIO ESTÁNDAR Y FALLBACK ---
        matches_tts = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        if matches_tts and not texto_para_audio:
             texto_para_audio = " ".join(matches_tts)
        elif current_mode == 'dialogo' and not texto_para_audio:
             # SALVAVIDAS
             caracteres_chinos = re.findall(r'[\u4e00-\u9fa5，。！？、]+', texto_salida)
             if caracteres_chinos:
                 texto_para_audio = "".join(caracteres_chinos)
             else:
                 texto_para_audio = None

        if texto_para_audio:
            await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
            out_audio = f"resp_{user_id}.mp3"
            tts = edge_tts.Communicate(texto_para_audio.replace('*', ''), voice="zh-CN-XiaoxiaoNeural", rate="-25%")
            await tts.save(out_audio)
            with open(out_audio, "rb") as audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)
            os.remove(out_audio)

        # --- ENVÍO DE TEXTO Y SPOILERS ---
        texto_limpio = texto_salida.replace('<tts>', '').replace('</tts>', '').strip()
        texto_seguro = html.escape(texto_limpio)
        
        if current_mode == 'dialogo':
            await context.bot.send_message(chat_id=chat_id, text=f"<tg-spoiler>{texto_seguro}</tg-spoiler>", parse_mode='HTML')
        else:
            await context.bot.send_message(chat_id=chat_id, text=texto_seguro, parse_mode='HTML')
            
    except Exception as e:
        await update.message.reply_text(f"Hubo un error: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    if ',' in texto:
        palabras = [p.strip() for p in texto.split(',') if p.strip()]
        if len(palabras) > 1:
            for p in palabras: guardar_palabra(p)
            await update.message.reply_text(f"✅ {len(palabras)} palabras guardadas.")
            return

    current_mode = user_states.get(update.effective_user.id, 'dialogo')
    if current_mode in ['ensenanza', 'evaluacion']:
        guardar_palabra(texto)
        
    await process_interaction(update, context, texto, is_audio=False, texto_original=texto)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    input_audio_path = f"input_{user_id}.ogg"
    
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    await voice_file.download_to_drive(input_audio_path)
    
    audio_part = client_gemini.files.upload(file=input_audio_path, config={'mime_type': 'audio/ogg'})
    await process_interaction(update, context, audio_part, is_audio=True)
    
    # Se eliminó la función de borrado de archivos de gemini para evitar el error 403.
    os.remove(input_audio_path)

async def configurar_menu(application: Application):
    await application.bot.set_my_commands([
        BotCommand("profesora", "📚 Modo Enseñanza"),
        BotCommand("evaluar", "📝 Modo Evaluar"),
        BotCommand("amiga", "🗣️ Modo Conversación"),
        BotCommand("noticias", "📰 Leer Noticias"),
        BotCommand("examen", "📝 Examen HSK/HSKK"),
        BotCommand("reiniciar", "🧹 Borrar memoria"),
        BotCommand("start", "🔄 Inicio")
    ])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(configurar_menu).build()  
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profesora", set_modo_ensenanza))
    app.add_handler(CommandHandler("evaluar", set_modo_evaluacion))
    app.add_handler(CommandHandler("amiga", set_modo_dialogo))
    app.add_handler(CommandHandler("noticias", enviar_noticias))
    app.add_handler(CommandHandler("examen", set_modo_examen))
    app.add_handler(CommandHandler("reiniciar", reiniciar_historial)) 
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("Lexy (Gemini-Only) Trabajando...")
    app.run_polling()

if __name__ == "__main__":
    main()