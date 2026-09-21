import os
import sqlite3
import re
import html
import random
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

# --- REGLAS HSK 2 OFICIALES ---
REGLAS_HSK2 = [
    "Experiencia Pasada (Sujeto + Verbo + 过)",
    "Estado Continuo (Sujeto + Verbo + 着)",
    "Acción Inminente (快要/就要...了)",
    "Comparación (A 比 B + Adj)",
    "Complemento de Grado (Verbo + 得 + 很/太 + Adj)",
    "Causa y Efecto (因为...所以)",
    "Contraste (虽然...但是)",
    "Totalidad/Frecuencia (每...都)",
    "Distancia (A 离 B + 很远/很近)",
    "Prohibición (别/不要...了)",
    "Sugerencia/Suposición (...吧)"
]

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

def eliminar_palabra(palabra):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM palabras WHERE palabra = ?", (palabra,))
    filas_borradas = cursor.rowcount
    conn.commit()
    conn.close()
    return filas_borradas > 0

def obtener_todas_las_palabras():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT palabra FROM palabras ORDER BY fecha DESC")
    palabras = [fila[0] for fila in cursor.fetchall()]
    conn.close()
    return palabras

def obtener_palabras_aleatorias(limite=3):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT palabra FROM palabras ORDER BY RANDOM() LIMIT ?", (limite,))
    palabras = [fila[0] for fila in cursor.fetchall()]
    conn.close()
    return palabras

def registrar_interaccion(chat_id, rol, contenido):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO usuarios (chat_id, ultimo_contacto) VALUES (?, CURRENT_TIMESTAMP)", (chat_id,))
    cursor.execute("INSERT INTO historial (chat_id, rol, contenido) VALUES (?, ?, ?)", (chat_id, rol, contenido))
    conn.commit()
    conn.close()

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
PROMPT_ENSENANZA="""Eres Lexy mi tutora nativa de chino mandarín. Mi objetivo actual es dominar el HSK 2 (versión 3.0). 
REGLA VITAL: NO inicies la lección ni sugieras palabras por tu cuenta. ESPERA siempre a que yo te envíe la palabra o el carácter que quiero estudiar.
1. Análisis del Carácter: Significado y lógica básica.
2. Regla de Tres (Usos Clave): 2 a 3 oraciones usando gramática HSK 2. Incluye caracteres, Pinyin y traducción.
3. Aplicación a la vida real: Hazme una pregunta usando esa palabra para que te la responda con algo que me haya pasado hoy.
"""

PROMPT_DIALOGO_BASE = """Eres Lexy, mi entrenadora de conversación (HSK 2).
Tu objetivo y OBLIGARME a usar la gramática del HSK 2.Si mi respuesta es muy simple, o crees que pude utilizar mejor alguna estructura gramatical, corrígeme y dame un ejemplo de cómo podría haberlo dicho mejor.

🧠 REGLA VITAL: EL MODELO BOLA DE NIEVE
Usa las siguientes reglas para obligarme a expandir mis respuestas:
1. Experiencia Pasada (Sujeto + Verbo + 过)
2. Estado Continuo (Sujeto + Verbo + 着)
3. Acción Inminente (快要/就要...了)
4. Comparación (A 比 B + Adj)
5. Complemento de Grado (Verbo + 得 + 很/太 + Adj)
6. Causa y Efecto (因为...所以)
7. Contraste (虽然...但是)
8. Totalidad/Frecuencia (每...都)
9. Distancia (A 离 B + 很远/很近)
10. Prohibición (别/不要...了)
11. Sugerencia/Suposición (...吧)
12. Énfasis de Circunstancias Pasadas (Sujeto + 是 + [Detalle] + Verbo + 的)
13. Preguntas de Opción (Opción A + 还是 + Opción B?)
14. Condicional Básico (如果/要是...就...)
15. Acciones Simultáneas (一边 + Verbo 1 + 一边 + Verbo 2)
16. Adición de Cualidades (又 + Adj 1 + 又 + Adj 2)
17. Expectativa de Tiempo Temprana/Tardía (Tiempo + 就/才 + Verbo)
18. Complemento de Dirección Simple (Verbo + 来 / 去)
19. Duración de la Acción (Sujeto + Verbo + [了] + Duración)
20. Preposición de Actitud u Objetivo (Sujeto + 对 + Persona/Cosa + Adjetivo/Verbo)
21. Superlativo (最 + Adjetivo / Verbo psicológico)

REGLA DE FORMATO ESTRICTA Y OBLIGATORIA: 
Tu respuesta debe tener SIEMPRE esta estructura exacta separada por saltos de línea (nunca añadas introducciones antes):
<tts>Respuesta en caracteres chinos (solo caracteres y puntuación)</tts>
Respuesta en Pinyin
Traducción al español
"""

PROMPT_RETO_SITUACIONAL = """Eres Lexy. Es hora del "Reto de los Bloques de Lego". 
El estudiante quiere integrar el chino a su día a día. Tu tarea es plantearle un reto de construcción de oraciones basado en su entorno actual.

Te daré una lista de palabras guardadas por el estudiante y una regla gramatical obligatoria.
Tu misión:
1. Saluda y dale las palabras (carácter, pinyin y significado).
2. Explícale brevemente la regla gramatical que le tocó.
3. EXÍGELE que mire a su alrededor en este instante y construya una oración que combine obligatoriamente al menos 1 o 2 de esas palabras con la regla gramatical asignada.

Si el estudiante te responde, evalúa su oración. Si suena robótica, corrígelo para que suene más natural.
"""

PROMPT_EXAMEN_HSK = "Eres un examinador oficial HSK {nivel}. Haz UNA sola pregunta de gramática/lectura a la vez, espera mi respuesta, corrígela y haz la siguiente. NO uses etiquetas <tts>."

PROMPT_EXAMEN_HSKK = "Eres un examinador oficial HSKK {nivel}. Alterna: 1. <hskk_audio>frase en chino</hskk_audio>. 2. Leer texto. 3. <hskk_img>escenario simple en inglés</hskk_img>. Evalúa mi pronunciación. Una tarea a la vez."

# --- COMANDOS CRUD BASE DE DATOS ---
async def comando_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Indica las palabras a agregar. Ejemplo: `/agregar 苹果, 学习, 汽车`", parse_mode='Markdown')
        return
    
    texto = " ".join(context.args)
    palabras = [p.strip() for p in texto.split(',') if p.strip()]
    for p in palabras:
        guardar_palabra(p)
    await update.message.reply_text(f"✅ Se agregaron {len(palabras)} palabras a tu base de datos:\n{', '.join(palabras)}")

async def comando_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    palabras = obtener_todas_las_palabras()
    if not palabras:
        await update.message.reply_text("📭 Tu lista de palabras está vacía.")
        return
    
    mensaje = "📋 *Tu Banco de Bloques (Vocabulario):*\n\n" + ", ".join(palabras)
    if len(mensaje) > 4000:
        await update.message.reply_text("📋 *Tu lista es muy larga. Mostrando las últimas 100:*", parse_mode='Markdown')
        await update.message.reply_text(", ".join(palabras[:100]))
    else:
        await update.message.reply_text(mensaje, parse_mode='Markdown')

async def comando_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Indica qué palabra borrar. Ejemplo: `/borrar 苹果`", parse_mode='Markdown')
        return
    
    palabra_a_borrar = " ".join(context.args).strip()
    if eliminar_palabra(palabra_a_borrar):
        await update.message.reply_text(f"🗑️ La palabra '{palabra_a_borrar}' ha sido eliminada.")
    else:
        await update.message.reply_text(f"❌ No encontré la palabra '{palabra_a_borrar}' en tu lista.")

# --- COMANDOS DE ACCIÓN ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    registrar_interaccion(chat_id, "user", "/start")
    mensaje = (
        "¡Hola! Soy Lexy, tu entrenadora situacional HSK 2.\n\n"
        "🛠️ *Gestión de Palabras (Base de Datos):*\n"
        "➕ /agregar palabra1, palabra2\n"
        "👀 /lista - Ver todas tus palabras guardadas\n"
        "🗑️ /borrar palabra - Eliminar una palabra\n\n"
        "🚀 *Modos de Entrenamiento:*\n"
        "🧩 /reto - Reto Situacional (Piezas de Lego con HSK 2)\n"
        "🗣️ /amiga - Charla Bola de Nieve (Te obligaré a usar estructuras)\n"
        "📚 /profesora - Analizar Palabra nueva\n"
        "📝 /examen - Simulacros\n"
        "🧹 /reiniciar - Borrar memoria del chat"
    )
    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def reiniciar_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM historial WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

    if user_id in user_sessions_gemini: del user_sessions_gemini[user_id]
    if user_id in user_states: user_states[user_id] = 'dialogo'
    await update.message.reply_text("🧹 <b>¡Historial borrado!</b> Empecemos de cero.", parse_mode='HTML')

async def set_modo_ensenanza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'ensenanza'
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_ENSENANZA})
    await update.message.reply_text("📚 Modo Enseñanza. Envíame la palabra que deseas analizar.")

async def set_modo_dialogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_states[user_id] = 'dialogo'
    historial = recuperar_historial(chat_id, limite=10)
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': PROMPT_DIALOGO_BASE})
    await update.message.reply_text("🗣️ Modo Conversación. ¿Qué estás haciendo en este preciso instante? Cuéntamelo en chino.")

async def set_modo_reto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_states[user_id] = 'reto'
    
    palabras = obtener_palabras_aleatorias(3)
    if not palabras:
        await update.message.reply_text("⚠️ No tienes palabras guardadas. Usa `/agregar palabra1, palabra2` para alimentar tu base de datos.", parse_mode='Markdown')
        return

    regla_elegida = random.choice(REGLAS_HSK2)
    instruccion_reto = f"Palabras aleatorias de la DB: {', '.join(palabras)}. Regla gramatical obligatoria a evaluar: {regla_elegida}. Preséntale el reto ahora mismo."
    
    user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', config={'system_instruction': PROMPT_RETO_SITUACIONAL})
    
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    try:
        respuesta = user_sessions_gemini[user_id].send_message(instruccion_reto)
        registrar_interaccion(chat_id, "model", respuesta.text)
        await update.message.reply_text(respuesta.text)
    except Exception as e:
        await update.message.reply_text(f"Error generando el reto: {str(e)}")

async def set_modo_examen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'esperando_examen'
    await update.message.reply_text("📝 ¿Examen *HSK* (escrito) o *HSKK* (oral)?", parse_mode='Markdown')

# --- PROCESAMIENTO CENTRAL ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False, texto_original=""):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_mode = user_states.get(user_id, 'dialogo')

    if current_mode == 'esperando_examen':
        eleccion = texto_original.strip().upper()
        if eleccion == 'HSK':
            user_states[user_id] = 'esperando_nivel_hsk'
            await update.message.reply_text("Nivel HSK? (1, 2 o 3).")
        elif eleccion == 'HSKK':
            user_states[user_id] = 'esperando_nivel_hskk'
            await update.message.reply_text("Nivel HSKK? (Básico o Intermedio).")
        return
    if current_mode == 'esperando_nivel_hsk':
        nivel = texto_original.strip()
        user_states[user_id] = 'examen_hsk'
        user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=recuperar_historial(chat_id, 10), config={'system_instruction': PROMPT_EXAMEN_HSK.format(nivel=nivel)})
        await update.message.reply_text(f"Iniciando HSK {nivel}...")
        input_data = f"¡Empecemos el examen HSK {nivel}!"
        current_mode = 'examen_hsk'
    if current_mode == 'esperando_nivel_hskk':
        nivel = texto_original.strip().lower()
        user_states[user_id] = 'examen_hskk'
        nivel_limpio = 'Básico' if nivel in ['básico', 'basico'] else 'Intermedio'
        user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=recuperar_historial(chat_id, 10), config={'system_instruction': PROMPT_EXAMEN_HSKK.format(nivel=nivel_limpio)})
        await update.message.reply_text(f"Iniciando HSKK {nivel_limpio}...")
        input_data = f"¡Empecemos el HSKK {nivel_limpio}!"
        current_mode = 'examen_hskk'

    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    texto_salida = ""

    try:
        if is_audio:
            if current_mode in ['dialogo', 'examen_hsk', 'reto']:
                resp_trans = client_gemini.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=[input_data, "Transcribe este audio. Devuelve SOLO el texto transcrito en chino, sin explicaciones ni comillas."]
                )
                instruccion = resp_trans.text.strip()
            else:
                instruccion = [input_data, "Evalúa la pronunciación y responde."]
        else:
            instruccion = input_data

        texto_a_guardar = instruccion if isinstance(instruccion, str) else "[Mensaje de Voz]"
        registrar_interaccion(chat_id, "user", texto_a_guardar)

        if user_id not in user_sessions_gemini:
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': PROMPT_DIALOGO_BASE})

        if current_mode == 'dialogo' and isinstance(instruccion, str):
             instruccion += "\n\n(Aplica el modelo bola de nieve obligándome a usar estructuras del HSK 2. Responde estrictamente con <tts>Caracteres chinos</tts>)"

        respuesta = user_sessions_gemini[user_id].send_message(instruccion)
        texto_salida = respuesta.text

        if not texto_salida:
            await update.message.reply_text("Error de conexión.")
            return

        registrar_interaccion(chat_id, "model", texto_salida)

        # Manejo de imágenes HSKK
        match_hskk_img = re.search(r'<hskk_img>(.*?)</hskk_img>', texto_salida)
        if match_hskk_img:
            prompt_img = match_hskk_img.group(1).replace(" ", "%20")
            url_img = f"https://image.pollinations.ai/prompt/{prompt_img}?width=800&height=600&nologo=true"
            await context.bot.send_photo(chat_id=chat_id, photo=url_img)
            texto_salida = re.sub(r'<hskk_img>.*?</hskk_img>', '', texto_salida)

        # TTS y Audios
        match_hskk_audio = re.search(r'<hskk_audio>(.*?)</hskk_audio>', texto_salida)
        texto_para_audio = match_hskk_audio.group(1) if match_hskk_audio else None
        if match_hskk_audio: texto_salida = re.sub(r'<hskk_audio>.*?</hskk_audio>', '', texto_salida)

        matches_tts = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        if matches_tts and not texto_para_audio:
             texto_para_audio = " ".join(matches_tts)
        elif current_mode == 'dialogo' and not texto_para_audio:
             caracteres_chinos = re.findall(r'[\u4e00-\u9fa5，。！？、]+', texto_salida)
             texto_para_audio = "".join(caracteres_chinos) if caracteres_chinos else None

        if texto_para_audio:
            await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
            out_audio = f"resp_{user_id}.mp3"
            tts = edge_tts.Communicate(texto_para_audio.replace('*', ''), voice="zh-CN-XiaoxiaoNeural", rate="-25%")
            await tts.save(out_audio)
            with open(out_audio, "rb") as audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)
            os.remove(out_audio)

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
    current_mode = user_states.get(update.effective_user.id, 'dialogo')
    
    # En modo enseñanza, si envías una palabra sola, igual la guardamos para tu DB
    if current_mode == 'ensenanza':
        guardar_palabra(texto.strip())

    await process_interaction(update, context, texto, is_audio=False, texto_original=texto)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    input_audio_path = f"input_{user_id}.ogg"
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    await voice_file.download_to_drive(input_audio_path)
    audio_part = client_gemini.files.upload(file=input_audio_path, config={'mime_type': 'audio/ogg'})
    await process_interaction(update, context, audio_part, is_audio=True)
    os.remove(input_audio_path)

async def configurar_menu(application: Application):
    await application.bot.set_my_commands([
        BotCommand("reto", "🧩 Reto Situacional HSK 2"),
        BotCommand("agregar", "➕ Añadir palabra(s) a la DB"),
        BotCommand("lista", "👀 Ver palabras guardadas"),
        BotCommand("borrar", "🗑️ Eliminar una palabra"),
        BotCommand("amiga", "🗣️ Charla Bola de Nieve"),
        BotCommand("profesora", "📚 Analizar Palabra"),
        BotCommand("examen", "📝 Examen HSK/HSKK"),
        BotCommand("reiniciar", "🧹 Borrar memoria"),
    ])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(configurar_menu).build()  
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reto", set_modo_reto))
    app.add_handler(CommandHandler("agregar", comando_agregar))
    app.add_handler(CommandHandler("lista", comando_lista))
    app.add_handler(CommandHandler("borrar", comando_borrar))
    app.add_handler(CommandHandler("profesora", set_modo_ensenanza))
    app.add_handler(CommandHandler("amiga", set_modo_dialogo))
    app.add_handler(CommandHandler("examen", set_modo_examen))
    app.add_handler(CommandHandler("reiniciar", reiniciar_historial)) 
    
    # Removido el filtro de comas para evitar bugs. Todo el texto va al procesador.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Lexy Situacional (DB Optimizada) Trabajando...")
    app.run_polling()

if __name__ == "__main__":
    main()