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
from openai import AsyncOpenAI
import edge_tts

# 1. Cargar el archivo .env PRIMERO
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not all([GEMINI_API_KEY, TELEGRAM_TOKEN, OPENROUTER_API_KEY]):
    raise ValueError("ERROR: Faltan API Keys en el archivo .env (Gemini, Telegram u OpenRouter).")

# Clientes de IA
client_gemini = genai.Client(api_key=GEMINI_API_KEY)
client_qwen = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)

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

def recuperar_historial(chat_id, limite=20, formato='gemini'):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT rol, contenido FROM historial WHERE chat_id = ? ORDER BY fecha DESC LIMIT ?", (chat_id, limite))
    filas = cursor.fetchall()
    conn.close()
    
    historial = []
    for rol, contenido in reversed(filas):
        if formato == 'gemini':
            historial.append(types.Content(role=rol, parts=[types.Part.from_text(text=contenido)]))
        else:
            # OpenRouter (OpenAI format) usa "user" y "assistant" (nuestro DB tiene 'user' y 'model')
            rol_or = "assistant" if rol == "model" else "user"
            historial.append({"role": rol_or, "content": contenido})
    return historial

# --- GESTIÓN DE ESTADOS Y SESIONES ---
user_states = {}
user_sessions_gemini = {} # Solo para sesiones activas de Gemini

# --- PROMPTS ---
PROMPT_ENSENANZA="""Eres Lexy mi tutora nativa de chino mandarín... [Mantén tu prompt original aquí]"""
PROMPT_EVALUACION = """Eres Lexy, actua como mi profesora... [Mantén tu prompt original aquí]"""

PROMPT_DIALOGO_BASE = """Eres Lexy mi compañera de intercambio de idiomas nativa de China. 
Hablar contigo me sirve para practicar vocabulario HSK1 y HSK2. Se proactiva.
Palabras a practicar hoy: {palabras_objetivo}.
Evalúa en mi respuesta si la gramática es correcta y suena natural. Si hay errores corrígelos amablemente.
REGLA DE FORMATO ESTRICTA: Tu respuesta debe tener SIEMPRE esta estructura exacta separada por saltos de línea:
<tts>Respuesta en caracteres chinos</tts>
Respuesta en Pinyin
Traducción al español"""

PROMPT_EXAMEN_HSK = """Eres un examinador de HSK. Realiza preguntas de lectura y gramática (opción múltiple o rellenar espacios). Haz UNA sola pregunta a la vez, espera mi respuesta, corrígela y haz la siguiente. NO uses <tts>."""

PROMPT_EXAMEN_HSKK = """Eres un examinador del test oral HSKK. Mi meta es certificarme. 
Tienes 3 etapas. Alterna entre ellas. Haz UNA SOLA actividad a la vez y evalúa mi pronunciación.
1. REPETIR AUDIO: Envíame la etiqueta <hskk_audio>frase en caracteres chinos</hskk_audio>.
2. LEER TEXTO: Envíame un texto y exígeme que lo lea en voz alta.
3. DESCRIBIR IMAGEN: Envíame la etiqueta <hskk_img>Una descripción de 3 palabras en inglés de un escenario común, ej: cat eating fish</hskk_img> y pídeme que te describa la imagen que me acabas de enviar en voz alta.
IMPORTANTE: Evalúa mis respuestas auditivas comparando mi pronunciación con el texto correcto."""

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    registrar_interaccion(chat_id, "user", "/start")
    mensaje = (
        "Usa /profesora para modo enseñanza (Gemini)\n"
        "Usa /evaluar para evaluar oraciones (Gemini)\n"
        "Usa /amiga para modo conversación libre (Qwen 72B)\n"
        "Usa /noticias para leer actualidad en HSK 2 (Gemini+Qwen)\n"
        "Usa /examen para simular prueba HSK/HSKK\n"
    )
    await update.message.reply_text(mensaje)

async def set_modo_ensenanza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'ensenanza'
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_ENSENANZA})
    await update.message.reply_text("📚 Modo Enseñanza (Gemini) activado. Esperando tus palabras...")

async def set_modo_evaluacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'evaluacion'
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_EVALUACION})
    await update.message.reply_text("📝 Modo Evaluación (Gemini) activado.")

async def set_modo_dialogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'dialogo'
    await update.message.reply_text("🗣️ Modo Conversación (Qwen 2.5 72B) activado. ¡Hablemos!")

async def set_modo_examen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'esperando_examen'
    await update.message.reply_text("📝 ¿Qué examen quieres practicar hoy? Responde con *HSK* (escrito) o *HSKK* (oral).", parse_mode='Markdown')

async def enviar_noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    await update.message.reply_text("📰 Buscando noticias con Gemini y traduciendo con Qwen 72B...")

    try:
        # 1. Gemini busca en internet
        resp_busqueda = client_gemini.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents="Busca en internet las 2 noticias más importantes de HOY en China y devuélveme el texto crudo con los hechos reales.",
            config=types.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        noticias_crudas = resp_busqueda.text

        # 2. Qwen resume y formatea en HSK 2
        prompt_qwen_noticias = f"Eres un experto en chino. Resume estas noticias reales usando EXCLUSIVAMENTE vocabulario HSK 1 y 2. Usa el formato estricto: <tts>Caracteres</tts> \n Pinyin \n Español. Noticias:\n{noticias_crudas}"
        
        completion = await client_qwen.chat.completions.create(
            model="qwen/qwen-2.5-72b-instruct:free",
            messages=[{"role": "user", "content": prompt_qwen_noticias}]
        )
        texto_salida = completion.choices[0].message.content
        registrar_interaccion(chat_id, "model", texto_salida)

        # 3. Audio y Envío
        await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
        output_audio_path = f"news_{chat_id}.mp3"
        matches = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        texto_para_audio = " ".join(matches).replace('*', '').strip() if matches else texto_salida.replace('*', '')
        
        tts = edge_tts.Communicate(texto_para_audio, voice="zh-CN-XiaoxiaoNeural", rate="-25%")
        await tts.save(output_audio_path)
        with open(output_audio_path, "rb") as audio:
            await context.bot.send_voice(chat_id=chat_id, voice=audio)
        os.remove(output_audio_path)

        texto_seguro = html.escape(texto_salida.replace('<tts>', '').replace('</tts>', '').strip())
        await context.bot.send_message(chat_id=chat_id, text=texto_seguro, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"Hubo un error con las noticias: {str(e)}")

# --- PROCESAMIENTO CENTRAL ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False, texto_original=""):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_mode = user_states.get(user_id, 'dialogo')
    
    # Manejo de selección de examen
    if current_mode == 'esperando_examen':
        eleccion = texto_original.strip().upper()
        if eleccion == 'HSK':
            user_states[user_id] = 'examen_hsk'
            await update.message.reply_text("Iniciando simulacro HSK con Qwen...")
            input_data = "¡Empecemos el examen HSK!"
            current_mode = 'examen_hsk'
        elif eleccion == 'HSKK':
            user_states[user_id] = 'examen_hskk'
            historial = recuperar_historial(chat_id, limite=10, formato='gemini')
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': PROMPT_EXAMEN_HSKK})
            await update.message.reply_text("Iniciando simulacro HSKK con Gemini...")
            input_data = "¡Empecemos el examen HSKK!"
            current_mode = 'examen_hskk'
        else:
            await update.message.reply_text("Por favor, responde solo 'HSK' o 'HSKK'.")
            return

    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    texto_salida = ""

    try:
        # Pre-procesamiento de AUDIO para modelos de TEXTO (Qwen)
        if is_audio:
            if current_mode in ['dialogo', 'examen_hsk']:
                # Transcribir con Gemini antes de dárselo a Qwen
                resp_trans = client_gemini.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=[input_data, "Transcribe este audio a chino. Devuelve SOLO el texto, nada más."]
                )
                instruccion = resp_trans.text
                registrar_interaccion(chat_id, "user", f"[Audio transcrito]: {instruccion}")
            else:
                instruccion = [input_data, "Evalúa la pronunciación y responde al reto."]
                registrar_interaccion(chat_id, "user", "[Mensaje de Voz HSKK]")
        else:
            instruccion = input_data
            registrar_interaccion(chat_id, "user", texto_original)

        # --- RUTADO HACIA EL MODELO CORRECTO ---
        if current_mode in ['ensenanza', 'evaluacion', 'examen_hskk']:
            # Usar GEMINI
            if user_id not in user_sessions_gemini:
                 user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite')
            respuesta = user_sessions_gemini[user_id].send_message(instruccion)
            texto_salida = respuesta.text

        elif current_mode in ['dialogo', 'examen_hsk']:
            # Usar QWEN 72B (OpenRouter)
            historial_or = recuperar_historial(chat_id, formato='openai')
            
            prompt_sis = PROMPT_EXAMEN_HSK if current_mode == 'examen_hsk' else PROMPT_DIALOGO_BASE.format(palabras_objetivo=", ".join(obtener_palabras_recientes(5)))
            
            mensajes_qwen = [{"role": "system", "content": prompt_sis}] + historial_or
            mensajes_qwen.append({"role": "user", "content": instruccion if isinstance(instruccion, str) else texto_original})

            completion = await client_qwen.chat.completions.create(
                model="qwen/qwen-2.5-72b-instruct:free",
                messages=mensajes_qwen
            )
            texto_salida = completion.choices[0].message.content

        if not texto_salida:
            await update.message.reply_text("Corte de conexión. ¿Puedes repetirlo?")
            return
            
        registrar_interaccion(chat_id, "model", texto_salida)

        # --- POST-PROCESAMIENTO HSKK (Imágenes y Audios dinámicos) ---
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

        # --- GENERACIÓN DE AUDIO ESTÁNDAR (<tts>) o HSKK ---
        matches_tts = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        if matches_tts and not texto_para_audio:
             texto_para_audio = " ".join(matches_tts)
        elif current_mode == 'dialogo' and not texto_para_audio:
             texto_para_audio = texto_salida

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
    
    client_gemini.files.delete(name=audio_part.name)
    os.remove(input_audio_path)

async def configurar_menu(application: Application):
    await application.bot.set_my_commands([
        BotCommand("profesora", "📚 Enseñanza (Gemini)"),
        BotCommand("evaluar", "📝 Evaluar (Gemini)"),
        BotCommand("amiga", "🗣️ Conversación (Qwen 72B)"),
        BotCommand("noticias", "📰 Noticias (Gemini+Qwen)"),
        BotCommand("examen", "📝 Examen HSK/HSKK"),
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("Lexy (Multi-Model) Trabajando...")
    app.run_polling()

if __name__ == "__main__":
    main()