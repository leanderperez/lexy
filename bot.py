import os
import sqlite3
import asyncio
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import google.generativeai as genai
import edge_tts

# --- CONFIGURACIÓN DE APIS ---
# Reemplaza con tus claves reales
GEMINI_API_KEY = "TU_API_KEY_DE_GEMINI"
TELEGRAM_TOKEN = "TU_TELEGRAM_BOT_TOKEN"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- CONFIGURACIÓN DE BASE DE DATOS ---
DB_FILE = 'vocabulario.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS palabras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            palabra TEXT UNIQUE,
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
        pass # La palabra ya existe
    conn.close()

def obtener_palabras_recientes(limite=3):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT palabra FROM palabras ORDER BY fecha DESC LIMIT ?", (limite,))
    palabras = [fila[0] for fila in cursor.fetchall()]
    conn.close()
    return palabras

# --- GESTIÓN DE ESTADOS Y SESIONES ---
user_states = {}
user_sessions = {} # Para mantener el historial de la conversación

PROMPT_ENSENANZA = """Eres un profesor de mandarín. Tu estudiante tiene nivel HSK 1 3.0. 
Se te dará una palabra u oración. Explica su significado, da el pinyin y proporciona 3 ejemplos claros de uso. 
Estructura tu respuesta de forma limpia y fácil de leer."""

PROMPT_DIALOGO_BASE = """Eres un compañero de intercambio de idiomas nativo de China. 
Estamos practicando conversación de nivel HSK 1 3.0. Responde de forma concisa y natural a lo que escuches o leas.
REGLA IMPORTANTE: Intenta incluir de forma natural las siguientes palabras en tu respuesta para que el estudiante las practique: {palabras_objetivo}.
Incluye siempre los caracteres y el pinyin en tu texto."""

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "¡Hola! Soy tu asistente de mandarín.\n\n"
        "Usa /ensenar para cambiar al modo de explicación de vocabulario.\n"
        "Usa /dialogar para practicar conversación libre.\n"
        "Usa /exportar para obtener tu vocabulario en CSV."
    )
    await update.message.reply_text(mensaje)

async def set_modo_ensenanza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = 'ensenanza'
    # Reiniciamos la sesión de chat para cambiar el contexto
    user_sessions[update.effective_user.id] = model.start_chat(history=[])
    await update.message.reply_text("📚 Modo Enseñanza activado. Envíame una palabra u oración y te daré ejemplos de uso.")

async def set_modo_dialogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = 'dialogo'
    user_sessions[update.effective_user.id] = model.start_chat(history=[])
    await update.message.reply_text("🗣️ Modo Diálogo activado. ¡Hablemos! Envíame audios o texto.")

async def exportar_vocabulario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import csv
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT palabra, fecha FROM palabras ORDER BY fecha DESC")
    filas = cursor.fetchall()
    conn.close()
    
    if not filas:
        await update.message.reply_text("Tu base de datos está vacía.")
        return
        
    csv_filename = "vocabulario_exportado.csv"
    with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["Palabra/Oracion", "Fecha Agregada"])
        writer.writerows(filas)
        
    with open(csv_filename, 'rb') as doc:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=doc, filename="anki_import.csv")
    os.remove(csv_filename)

# --- PROCESAMIENTO DE MENSAJES Y AUDIO ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False):
    user_id = update.effective_user.id
    current_mode = user_states.get(user_id, 'dialogo')
    
    if user_id not in user_sessions:
        user_sessions[user_id] = model.start_chat(history=[])
    
    chat_session = user_sessions[user_id]
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    try:
        if current_mode == 'ensenanza':
            if not is_audio:
                guardar_palabra(input_data)
            instruccion = [PROMPT_ENSENANZA, input_data]
            
        elif current_mode == 'dialogo':
            palabras_a_practicar = ", ".join(obtener_palabras_recientes(3))
            prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo=palabras_a_practicar)
            instruccion = [prompt_dinamico, input_data]

        # Enviar a Gemini
        respuesta = chat_session.send_message(instruccion)
        texto_salida = respuesta.text

        # Si el usuario envió audio, asumimos que quiere respuesta en audio (TTS)
        if is_audio or current_mode == 'dialogo':
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='record_voice')
            output_audio_path = f"response_{user_id}.mp3"
            
            # Limpiar texto para TTS (quitar asteriscos de markdown que puedan afectar la lectura)
            texto_limpio = texto_salida.replace('*', '')
            
            tts = edge_tts.Communicate(texto_limpio, voice="zh-CN-XiaoxiaoNeural")
            await tts.save(output_audio_path)
            
            with open(output_audio_path, "rb") as audio:
                await context.bot.send_voice(chat_id=update.effective_chat.id, voice=audio, caption=texto_salida[:1000])
            os.remove(output_audio_path)
        else:
            await update.message.reply_text(texto_salida)
            
    except Exception as e:
        await update.message.reply_text(f"Hubo un error procesando la solicitud: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    await process_interaction(update, context, texto, is_audio=False)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    input_audio_path = f"input_{user_id}.ogg"
    
    # Descargar audio de Telegram
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    await voice_file.download_to_drive(input_audio_path)
    
    # Subir a Gemini
    audio_part = genai.upload_file(input_audio_path)
    
    await process_interaction(update, context, audio_part, is_audio=True)
    
    # Limpieza
    genai.delete_file(audio_part.name)
    os.remove(input_audio_path)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ensenar", set_modo_ensenanza))
    app.add_handler(CommandHandler("dialogar", set_modo_dialogo))
    app.add_handler(CommandHandler("exportar", exportar_vocabulario))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("Bot de mandarín iniciado y esperando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()