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

if not GEMINI_API_KEY:
    raise ValueError("ERROR: No se encontró GEMINI_API_KEY. Revisa tu archivo .env")
if not TELEGRAM_TOKEN:
    raise ValueError("ERROR: No se encontró TELEGRAM_TOKEN. Revisa tu archivo .env")

client = genai.Client(api_key=GEMINI_API_KEY)

# --- CONFIGURACIÓN DE BASE DE DATOS ---
DB_FILE = 'vocabulario.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Tabla de vocabulario
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS palabras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            palabra TEXT UNIQUE,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Tabla para guardar a los usuarios (necesario para mensajes proactivos)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            chat_id INTEGER PRIMARY KEY,
            ultimo_contacto TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Tabla de historial para la memoria de Lexy
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            rol TEXT,
            contenido TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def guardar_palabra(palabra):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO palabras (palabra) VALUES (?)", (palabra,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass 
    conn.close()

def registrar_interaccion(chat_id, rol, contenido):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Actualizar la fecha del usuario
    cursor.execute("INSERT OR REPLACE INTO usuarios (chat_id, ultimo_contacto) VALUES (?, CURRENT_TIMESTAMP)", (chat_id,))
    # Guardar en el historial
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

def recuperar_historial(chat_id, limite=10):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT rol, contenido FROM historial WHERE chat_id = ? ORDER BY fecha DESC LIMIT ?", (chat_id, limite))
    filas = cursor.fetchall()
    conn.close()
    
    historial_gemini = []
    # Invertimos para que el orden cronológico sea correcto para Gemini
    for rol, contenido in reversed(filas):
        historial_gemini.append(
            types.Content(role=rol, parts=[types.Part.from_text(text=contenido)])
        )
    return historial_gemini

# --- GESTIÓN DE ESTADOS Y SESIONES ---
user_states = {}
user_sessions = {}

PROMPT_ENSENANZA="""Eres Lexy mi tutora nativa de chino mandarín y compañera de estudio. Mi objetivo es aprender caracteres de forma práctica y natural. 
REGLA VITAL: NO inicies la lección ni sugieras palabras por tu cuenta. ESPERA siempre a que yo te envíe la palabra o el carácter que quiero estudiar.
Una vez que yo te envíe la palabra, seguiremos este método estructurado:
1. Análisis del Carácter: Explica brevemente el significado del carácter, su componente visual o radical, y su lógica básica.
2. Regla de Tres (Usos Clave): Muestra entre 2 y 3 palabras compuestas o estructuras hipercomunes en las que este carácter sea protagonista en la vida diaria. Incluye caracteres, Pinyin y traducción.
3. El Reto de Chat (Práctica Activa): Plantéame un escenario cotidiano real y pídeme que redacte una frase corta usando la palabra nueva combinada con lo que ya sé. Dame pistas claras para guiar mi respuesta.
4. Feedback Inmediato y Natural: Cuando yo responda al reto, valida mi frase. Si cometo un error sutil de gramática o naturalidad, corrígelo de forma directa y amable, explicando el porqué, y muestra cómo lo diría un nativo.
5. El Contador del Bloque: Mantén un registro visual al final de cada respuesta. Vamos a agrupar las palabras de 5 en 5. Cuando completemos un bloque de 5 palabras, detén el avance y hazme un examen/repaso general usando todas las palabras de ese bloque en un diálogo integrado.
"""

PROMPT_EVALUACION = """Eres Lexy, actua como mi profesora de chino mandarín nativo. 
Voy a escribir oraciones creadas por mí. No las traduzcas directamente. 
Evalúa si la gramática es correcta y si suena natural para un nativo. 
Si hay errores, corrígelos, muéstrame el Pinyin y explícame la regla gramatical en español de forma simple.
"""

PROMPT_DIALOGO_BASE = """Eres Lexy mi compañera de intercambio de idiomas nativa de China. 
Hablar contigo me sirve para aprender bocabulario del nivel HSK1 3.0 y practcar.
REGLA DE FORMATO ESTRICTA: Tu respuesta debe tener SIEMPRE esta estructura exacta, separada por saltos de línea:
<tts>Respuesta en caracteres chinos</tts>
Respuesta en Pinyin
Traducción al español
"""

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    registrar_interaccion(chat_id, "user", "/start")
    mensaje = (
        "Usa /profesora para modo enseñanza\n"
        "Usa /evaluar para evaluar oraciones\n"
        "Usa /amiga para modo conversación libre\n"
    )
    await update.message.reply_text(mensaje)

async def set_modo_ensenanza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[update.effective_user.id] = 'ensenanza'
    user_sessions[update.effective_user.id] = client.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_ENSENANZA})
    await update.message.reply_text("📚 Modo Enseñanza activado. Esperando tus palabras...")

async def set_modo_evaluacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[update.effective_user.id] = 'evaluacion'
    user_sessions[update.effective_user.id] = client.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_EVALUACION})
    await update.message.reply_text("📝 Modo Evaluación activado.")

async def set_modo_dialogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[update.effective_user.id] = 'dialogo'
    
    palabras_a_practicar = ", ".join(obtener_palabras_recientes(5))
    prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo=palabras_a_practicar)
    
    # Al iniciar el diálogo, recuperamos la memoria
    historial = recuperar_historial(chat_id)
    user_sessions[update.effective_user.id] = client.chats.create(
        model='gemini-3.5-flash-lite', 
        history=historial,
        config={'system_instruction': prompt_dinamico}
    )
    await update.message.reply_text("🗣️ Modo Diálogo activado. ¡Hablemos!")

# --- TAREAS PROACTIVAS (2 VECES AL DÍA) ---
async def escribir_proactivamente(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM usuarios")
    usuarios = cursor.fetchall()
    conn.close()

    for (chat_id,) in usuarios:
        try:
            historial = recuperar_historial(chat_id, limite=5)
            prompt_proactivo = "Eres Lexy. El usuario no te ha hablado en un buen rato. Escríbele un mensaje corto para sacarle conversación. REGLA ESTRICTA DE FORMATO: Responde exactamente con 3 líneas: la primera con caracteres chinos envueltos en <tts></tts>, la segunda con el pinyin, y la tercera con español."
            
            sesion_temporal = client.chats.create(model='gemini-3.5-flash-lite', history=historial)
            respuesta = sesion_temporal.send_message(prompt_proactivo)
            texto_salida = respuesta.text
            
            registrar_interaccion(chat_id, "model", texto_salida)

            # --- 1. GENERAR EL AUDIO PROACTIVO ---
            output_audio_path = f"proactive_{chat_id}.mp3"
            
            matches = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
            texto_para_audio = " ".join(matches).replace('*', '').strip() if matches else texto_salida.replace('*', '')
            
            tts = edge_tts.Communicate(texto_para_audio, voice="zh-CN-XiaoxiaoNeural", rate="-25%")
            await tts.save(output_audio_path)
            
            with open(output_audio_path, "rb") as audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)
            os.remove(output_audio_path)

            # --- 2. LIMPIAR Y OCULTAR EL TEXTO ---
            texto_telegram = texto_salida.replace('<tts>', '').replace('</tts>', '').strip()
            texto_seguro = html.escape(texto_telegram)
            texto_oculto = f"<tg-spoiler>{texto_seguro}</tg-spoiler>"
            
            await context.bot.send_message(chat_id=chat_id, text=texto_oculto, parse_mode='HTML')
            
        except Exception as e:
            print(f"Error enviando mensaje proactivo a {chat_id}: {e}")

# --- PROCESAMIENTO DE MENSAJES Y AUDIO ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False, texto_original=""):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_mode = user_states.get(user_id, 'dialogo')
    
    if current_mode != 'dialogo' and user_id not in user_sessions:
        user_sessions[user_id] = client.chats.create(model='gemini-3.5-flash-lite')
        
    chat_session = user_sessions.get(user_id)
    if current_mode == 'dialogo':
        palabras_a_practicar = ", ".join(obtener_palabras_recientes(5))
        prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo=palabras_a_practicar)
        historial = recuperar_historial(chat_id)
        chat_session = client.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': prompt_dinamico})
        user_sessions[user_id] = chat_session

    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    try:
        registrar_interaccion(chat_id, "user", texto_original if not is_audio else "[Mensaje de Voz]")
        
        instruccion = input_data
        if is_audio:
            instruccion = [input_data, "El usuario te envió un mensaje de voz. Responde siguiendo tus reglas estrictas de formato: <tts>, pinyin y español separados por saltos de línea."]
        
        respuesta = chat_session.send_message(instruccion)
        texto_salida = respuesta.text
        
        registrar_interaccion(chat_id, "model", texto_salida)

        # --- LIMPIEZA DE TEXTO (Para todos los modos) ---
        texto_telegram = texto_salida.replace('<tts>', '').replace('</tts>', '').strip()
        texto_seguro = html.escape(texto_telegram)

        if current_mode == 'dialogo':
            # --- SOLO EN MODO DIÁLOGO: GENERAR AUDIO Y OCULTAR TEXTO ---
            await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
            output_audio_path = f"response_{user_id}.mp3"
            
            matches = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
            texto_para_audio = " ".join(matches).replace('*', '').strip() if matches else texto_salida.replace('*', '')
            
            tts = edge_tts.Communicate(texto_para_audio, voice="zh-CN-XiaoxiaoNeural", rate="-25%")
            await tts.save(output_audio_path)
            
            with open(output_audio_path, "rb") as audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)
            os.remove(output_audio_path)

            texto_final = f"<tg-spoiler>{texto_seguro}</tg-spoiler>"
            await context.bot.send_message(chat_id=chat_id, text=texto_final, parse_mode='HTML')
            
        else:
            # --- MODO ENSEÑANZA Y EVALUACIÓN ---
            # No enviamos audio, y el texto va directo sin ocultar
            texto_final = texto_seguro
            await context.bot.send_message(chat_id=chat_id, text=texto_final, parse_mode='HTML')
            
    except Exception as e:
        await update.message.reply_text(f"Hubo un error procesando la solicitud: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    chat_id = update.effective_chat.id
    
    # 1. Detectar si es una lista separada por comas
    if ',' in texto:
        palabras = [p.strip() for p in texto.split(',') if p.strip()]
        if len(palabras) > 1:
            for p in palabras:
                guardar_palabra(p)
            await update.message.reply_text(f"✅ Lexy ha guardado estas {len(palabras)} palabras directamente en tu base de datos.")
            return

    # Si no es una lista, guardar si estamos en enseñanza/evaluación y procesar normal
    current_mode = user_states.get(update.effective_user.id, 'dialogo')
    if current_mode in ['ensenanza', 'evaluacion']:
        guardar_palabra(texto)
        
    await process_interaction(update, context, texto, is_audio=False, texto_original=texto)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    input_audio_path = f"input_{user_id}.ogg"
    
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    await voice_file.download_to_drive(input_audio_path)
    
    audio_part = client.files.upload(
        file=input_audio_path,
        config={'mime_type': 'audio/ogg'}
    )
    
    await process_interaction(update, context, audio_part, is_audio=True)
    
    client.files.delete(name=audio_part.name)
    os.remove(input_audio_path)

async def configurar_menu(application: Application):
    await application.bot.set_my_commands([
        BotCommand("profesora", "📚 Modo Enseñanza (Retos y vocabulario)"),
        BotCommand("evaluar", "📝 Modo Evaluación (Corregir oraciones)"),
        BotCommand("amiga", "🗣️ Modo Conversación (Diálogo libre)"),
        BotCommand("start", "🔄 Ver mensaje de bienvenida")
    ])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(configurar_menu).build()  

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profesora", set_modo_ensenanza))
    app.add_handler(CommandHandler("evaluar", set_modo_evaluacion))
    app.add_handler(CommandHandler("amiga", set_modo_dialogo))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    # Configurar tareas proactivas cada 12 horas
    app.job_queue.run_repeating(escribir_proactivamente, interval=43200, first=10)
    
    print("Lexy Trabajando...")
    app.run_polling()

if __name__ == "__main__":
    main()