import os
import sqlite3
import asyncio
import re
import html
import random
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

# --- LISTA HSK 2 (3.0) PARA LECCIONES PROACTIVAS ---
# Muestra representativa de vocabulario HSK 2 (3.0) para las lecciones cada 2 horas
PALABRAS_HSK2 = [
    "帮 (bāng - ayudar)", "比如 (bǐrú - por ejemplo)", "必须 (bìxū - deber/tener que)", 
    "发现 (fāxiàn - descubrir/darse cuenta)", "刚才 (gāngcái - hace un momento)", 
    "讲 (jiǎng - hablar/explicar)", "经常 (jīngcháng - a menudo)", "马上 (mǎshàng - de inmediato)", 
    "明白 (míngbai - entender)", "认为 (rènwéi - creer/opinar)", "虽然 (suīrán - aunque)", 
    "但是 (dànshì - pero)", "希望 (xīwàng - esperar/desear)", "需要 (xūyào - necesitar)", 
    "一直 (yìzhí - continuamente/siempre)", "因为 (yīnwèi - porque)", "所以 (suǒyǐ - por eso)",
    "以前 (yǐqián - antes)", "以后 (yǐhòu - después)", "准备 (zhǔnbèi - preparar)",
    "最近 (zuìjìn - últimamente)", "懂 (dǒng - entender)", "告诉 (gàosu - decir/contar)",
    "已经 (yǐjīng - ya)", "离 (lí - distancia desde)", "错 (cuò - error/equivocado)",
    "事情 (shìqing - asunto/cosa)", "觉得 (juéde - sentir/pensar)", "可能 (kěnéng - tal vez)",
    "每 (měi - cada)"
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

def obtener_todos_los_usuarios():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM usuarios")
    usuarios = [fila[0] for fila in cursor.fetchall()]
    conn.close()
    return usuarios

# --- GESTIÓN DE ESTADOS Y SESIONES ---
user_states = {}
user_sessions_gemini = {}

# --- PROMPTS ---
PROMPT_ENSENANZA="""Eres Lexy mi tutora nativa de chino mandarín. Mi objetivo actual es dominar el HSK 2 (versión 3.0). 
REGLA VITAL: NO inicies la lección ni sugieras palabras por tu cuenta. ESPERA siempre a que yo te envíe la palabra o el carácter que quiero estudiar.
Una vez que yo te envíe la palabra, seguiremos este método estructurado:
1. Análisis del Carácter: Significado, componente visual o radical, y lógica básica.
2. Regla de Tres (Usos Clave): 2 a 3 palabras compuestas o estructuras usando gramática HSK 2. Incluye caracteres, Pinyin y traducción.
3. El Reto de Chat: Plantéame un escenario cotidiano real y pídeme redactar una frase usando la palabra nueva + gramática HSK 2.
4. Feedback: Corrige mi frase de forma directa y amable explicando el porqué.
5. El Contador del Bloque: Vamos a agrupar palabras de 5 en 5. Al completar un bloque, hazme un examen/repaso general.
"""

PROMPT_LECCION_PERIODICA = """Eres Lexy. Es momento de enviarle al estudiante su píldora de estudio programada (HSK 2 - 3.0).
Hoy repasarás con él la siguiente palabra/concepto: {palabra_hoy}

Tu mensaje debe ser directo, amigable y estructurado así:
1. Saluda y presenta la palabra.
2. Escribe 2 oraciones de ejemplo muy naturales usando esa palabra combinada con la gramática HSK 2.
3. Hazle una pregunta corta para que la responda en el chat usando la palabra.

REGLA DE FORMATO ESTRICTA:
TODO el texto en caracteres chinos (ejemplos, saludos y preguntas) debe estar envuelto en una sola etiqueta <tts>...</tts> al principio de tu mensaje. 
Debajo de la etiqueta, coloca el Pinyin y la traducción al español.
"""

PROMPT_DIALOGO_BASE = """Eres Lexy, mi compañera de intercambio de idiomas nativa de China. 
Hablar contigo me sirve para aprender y practicar vocabulario y gramática del nivel HSK 2 (3.0). Mantén una conversación casual.

🧠 REGLA VITAL: EL MODELO BOLA DE NIEVE
Tu objetivo principal es forzarme a practicar la gramática del HSK 2. Si mi respuesta es muy básica, NO cambies de tema. En su lugar, hazme preguntas de seguimiento (ej. ¿cuándo?, ¿con quién?, ¿por qué?) para obligarme a expandir mi respuesta.
Oblígame sutilmente a usar estas estructuras gramaticales en la charla:
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

INFORMACIÓN DE CONTEXTO (OPCIONAL):
Recientemente he estudiado estas palabras: {palabras_objetivo}.
REGLA PSICOLÓGICA VITAL: Trata estas palabras SOLO como referencia. NO fuerces su uso si no encajan en el tema actual. NO cambies tu estado de ánimo ni inventes historias dramáticas para usarlas (por ejemplo, si estudié la palabra "llorar", NO actúes triste). La prioridad absoluta es que la charla sea 100% natural.

REGLA DE FORMATO ESTRICTA Y OBLIGATORIA: 
Tu respuesta debe tener SIEMPRE esta estructura exacta separada por saltos de línea (nunca añadas introducciones antes):
<tts>Respuesta en caracteres chinos (solo caracteres y puntuación)</tts>
Respuesta en Pinyin
Traducción al español
"""

PROMPT_EXAMEN_HSK = """Eres un examinador oficial de las pruebas HSK. Mi meta es certificarme en HSK {nivel}.
Estamos en una simulación de examen oficial (Mock Test). Revisa el historial para NO repetir preguntas que ya me hayas hecho.
Realiza preguntas de lectura y gramática acordes EXCLUSIVAMENTE al nivel HSK {nivel} (opción múltiple o rellenar espacios en blanco). 
Haz UNA sola pregunta a la vez, espera mi respuesta, corrígela y haz la siguiente. NO uses etiquetas <tts>.
"""

PROMPT_EXAMEN_HSKK = """Eres un examinador oficial del test oral HSKK (Nivel {nivel}). Mi meta es certificarme. 
Tienes 3 etapas. Alterna entre ellas. Haz UNA SOLA actividad a la vez y evalúa mi pronunciación.
1. REPETIR AUDIO: Envíame la etiqueta <hskk_audio>frase en caracteres chinos</hskk_audio>.
2. LEER TEXTO: Envíame un texto (solo en caracteres chinos) y exígeme que lo lea en voz alta.
3. DESCRIBIR IMAGEN: Envíame la etiqueta <hskk_img>Una descripción de 3 palabras en inglés de un escenario común, ej: cat eating fish</hskk_img> y pídeme que te describa la imagen que me acabas de enviar en voz alta.
IMPORTANTE: Evalúa mis respuestas auditivas (Mensaje de Voz HSKK) comparando mi pronunciación con el texto correcto o evaluando si la descripción de la imagen es coherente para el nivel {nivel}.
"""

# --- COMANDOS Y TAREAS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    registrar_interaccion(chat_id, "user", "/start")
    mensaje = (
        "¡Hola! He sido optimizada para enfocarnos al 100% en tu meta: HSK 2 (3.0).\n\n"
        "Comandos disponibles:\n"
        "📚 /profesora - Estudiar palabras nuevas\n"
        "🗣️ /amiga - Práctica Bola de Nieve (HSK 2)\n"
        "📝 /examen - Simulacros HSK/HSKK\n"
        "🧹 /reiniciar - Borrar memoria del chat\n\n"
        "💡 *Nota:* Te enviaré píldoras de estudio automáticamente cada 2 horas."
    )
    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def enviar_leccion_periodica(context: ContextTypes.DEFAULT_TYPE):
    usuarios = obtener_todos_los_usuarios()
    if not usuarios:
        return
        
    palabra_hoy = random.choice(PALABRAS_HSK2)
    prompt_armado = PROMPT_LECCION_PERIODICA.format(palabra_hoy=palabra_hoy)
    
    for chat_id in usuarios:
        try:
            # Generar contenido con Gemini
            respuesta = client_gemini.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=prompt_armado
            )
            texto_salida = respuesta.text
            registrar_interaccion(chat_id, "model", texto_salida)
            
            # Generar Audio
            output_audio_path = f"leccion_{chat_id}.mp3"
            matches = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
            texto_para_audio = " ".join(matches).replace('*', '').strip() if matches else None
            
            if texto_para_audio:
                # Velocidad 0.75x (-25%)
                tts = edge_tts.Communicate(texto_para_audio, voice="zh-CN-XiaoxiaoNeural", rate="-25%")
                await tts.save(output_audio_path)
                with open(output_audio_path, "rb") as audio:
                    await context.bot.send_voice(chat_id=chat_id, voice=audio, caption="📚 ¡Tu píldora de estudio HSK 2 ha llegado!")
                os.remove(output_audio_path)
            
            texto_seguro = html.escape(texto_salida.replace('<tts>', '').replace('</tts>', '').strip())
            await context.bot.send_message(chat_id=chat_id, text=texto_seguro, parse_mode='HTML')
            
        except Exception as e:
            print(f"Error enviando lección a {chat_id}: {e}")

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
    await update.message.reply_text("📚 Modo Enseñanza HSK 2 activado. Envíame la palabra que deseas estudiar.")

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
    await update.message.reply_text("🗣️ Modo Bola de Nieve activado. ¡Hablemos! Prepárate para usar la gramática del HSK 2.")

async def set_modo_examen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = 'esperando_examen'
    await update.message.reply_text("📝 ¿Qué examen quieres practicar hoy? Responde con *HSK* (escrito) o *HSKK* (oral).", parse_mode='Markdown')

# --- PROCESAMIENTO CENTRAL ---
async def process_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, input_data, is_audio=False, texto_original=""):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_mode = user_states.get(user_id, 'dialogo')
    
    # --- RUTEO DE SUB-ESTADOS PARA EXÁMENES ---
    if current_mode == 'esperando_examen':
        eleccion = texto_original.strip().upper()
        if eleccion == 'HSK':
            user_states[user_id] = 'esperando_nivel_hsk'
            await update.message.reply_text("Has elegido HSK (Escrito). ¿Qué nivel deseas evaluar? Responde con *1, 2 o 3*.", parse_mode='Markdown')
        elif eleccion == 'HSKK':
            user_states[user_id] = 'esperando_nivel_hskk'
            await update.message.reply_text("Has elegido HSKK (Oral). ¿Qué nivel deseas evaluar? Responde con *Básico* o *Intermedio*.", parse_mode='Markdown')
        else:
            await update.message.reply_text("Por favor, responde solo 'HSK' o 'HSKK'.")
        return

    if current_mode == 'esperando_nivel_hsk':
        nivel = texto_original.strip()
        if nivel in ['1', '2', '3']:
            user_states[user_id] = 'examen_hsk'
            prompt_configurado = PROMPT_EXAMEN_HSK.format(nivel=nivel)
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': prompt_configurado})
            await update.message.reply_text(f"Iniciando simulacro HSK Nivel {nivel}...")
            input_data = f"¡Empecemos el examen HSK nivel {nivel}!"
            current_mode = 'examen_hsk'
        else:
            await update.message.reply_text("Por favor, responde solo 1, 2 o 3.")
            return

    if current_mode == 'esperando_nivel_hskk':
        nivel = texto_original.strip().lower()
        if nivel in ['básico', 'basico', 'intermedio']:
            user_states[user_id] = 'examen_hskk'
            nivel_limpio = 'Básico' if nivel in ['básico', 'basico'] else 'Intermedio'
            prompt_configurado = PROMPT_EXAMEN_HSKK.format(nivel=nivel_limpio)
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': prompt_configurado})
            await update.message.reply_text(f"Iniciando simulacro HSKK Nivel {nivel_limpio}...")
            input_data = f"¡Empecemos el examen HSKK nivel {nivel_limpio}!"
            current_mode = 'examen_hskk'
        else:
            await update.message.reply_text("Por favor, responde 'Básico' o 'Intermedio'.")
            return

    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    texto_salida = ""

    try:
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

        texto_a_guardar = instruccion if isinstance(instruccion, str) else "[Mensaje de Voz HSKK]"
        registrar_interaccion(chat_id, "user", texto_a_guardar)

        if user_id not in user_sessions_gemini:
            prompt_dinamico = PROMPT_DIALOGO_BASE.format(palabras_objetivo="")
            historial = recuperar_historial(chat_id, limite=10)
            user_sessions_gemini[user_id] = client_gemini.chats.create(model='gemini-3.5-flash-lite', history=historial, config={'system_instruction': prompt_dinamico})

        if current_mode == 'dialogo' and isinstance(instruccion, str):
             instruccion += "\n\n(Regla obligatoria: Aplica el modelo bola de nieve para hacerme una pregunta y responde usando estrictamente el formato de 3 líneas empezando con <tts>Caracteres chinos</tts>)"

        respuesta = user_sessions_gemini[user_id].send_message(instruccion)
        texto_salida = respuesta.text

        if not texto_salida:
            await update.message.reply_text("Corte de conexión. ¿Puedes repetirlo?")
            return
            
        registrar_interaccion(chat_id, "model", texto_salida)

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

        matches_tts = re.findall(r'<tts>(.*?)</tts>', texto_salida, re.DOTALL)
        if matches_tts and not texto_para_audio:
             texto_para_audio = " ".join(matches_tts)
        elif current_mode == 'dialogo' and not texto_para_audio:
             caracteres_chinos = re.findall(r'[\u4e00-\u9fa5，。！？、]+', texto_salida)
             if caracteres_chinos:
                 texto_para_audio = "".join(caracteres_chinos)
             else:
                 texto_para_audio = None

        if texto_para_audio:
            await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
            out_audio = f"resp_{user_id}.mp3"
            # Audio siempre a 0.75x
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
    if ',' in texto:
        palabras = [p.strip() for p in texto.split(',') if p.strip()]
        if len(palabras) > 1:
            for p in palabras: guardar_palabra(p)
            await update.message.reply_text(f"✅ {len(palabras)} palabras guardadas.")
            return

    current_mode = user_states.get(update.effective_user.id, 'dialogo')
    if current_mode == 'ensenanza':
        guardar_palabra(texto)
        
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
        BotCommand("profesora", "📚 Modo Enseñanza"),
        BotCommand("amiga", "🗣️ Modo Conversación"),
        BotCommand("examen", "📝 Examen HSK/HSKK"),
        BotCommand("reiniciar", "🧹 Borrar memoria"),
        BotCommand("start", "🔄 Inicio")
    ])

def main():
    init_db()
    
    # Se añade job_queue al constructor de la aplicación
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(configurar_menu).build()  
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profesora", set_modo_ensenanza))
    app.add_handler(CommandHandler("amiga", set_modo_dialogo))
    app.add_handler(CommandHandler("examen", set_modo_examen))
    app.add_handler(CommandHandler("reiniciar", reiniciar_historial)) 
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    # Programa el envío automático cada 2 horas (43200 segundos). El primero iniciará a los 10 segundos para probar que funciona.
    app.job_queue.run_repeating(enviar_leccion_periodica, interval=7200, first=10)
    
    print("Lexy enfocada en HSK 2 Trabajando...")
    app.run_polling()

if __name__ == "__main__":
    main()