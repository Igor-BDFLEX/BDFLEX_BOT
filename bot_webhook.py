# bot_webhook.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK/POLLING MODE)
#
# Este bot permite:
# 1. Criação, visualização, atualização e eliminação de Ordens de Serviço (OS).
# 2. Gestão de alertas (lembretes) associados a uma OS específica.
# 3. Agendamento de lembretes manuais.
# 4. Exportação do estado atual das OS para PDF (requer PyMuPDF e pandas).
# 5. Utiliza Firebase Firestore para persistência de dados.

# --- Imports e Setup ---

import logging
import json
import time
import os
import re # Para manipulação de texto e validação de formatos
import uuid # Para IDs únicos
from datetime import datetime, timedelta, timezone
import asyncio # Adicionado para tarefas assíncronas
import aiohttp # Para requisições HTTP (Manter o bot ativo)
import io # Para manipulação de arquivos em memória

# --- Imports para .env (secrets) ---
from dotenv import load_dotenv

# --- Imports para PDF (necessitam de instalação via pip: PyMuPDF e pandas) ---
try:
    # A importação desses módulos garante que o recurso de PDF está disponível.
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    # Se PyMuPDF ou Pandas não estiverem disponíveis, o recurso Enviar PDF será desativado
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    class MockDataFrame: # Placeholder para evitar erros
        def __init__(self, data=None): self.data = data
        def to_html(self): return "<html><body>Dados indisponíveis.</body></html>"
    pd = type('pd', (object,), {'DataFrame': MockDataFrame})()

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app
from google.cloud.firestore_v1.base_query import FieldFilter

# Python Telegram Bot
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    JobQueue
)
from telegram.constants import ParseMode

# --- Configuração ---

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Estados para o ConversationHandler
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, \
PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, \
LEMBRETE_MENU, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG, \
PROMPT_DELETE_OS = range(15)

# --- Configuração de Ambiente e Firebase Init ---

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# Porta padrão 8080, mas pode ser configurada pela variável de ambiente PORT
PORT = int(os.getenv("PORT", 8080)) 
WEBHOOK_PATH = "/" + TOKEN if TOKEN else "/bot" # Usar o token como path para segurança

# Tenta carregar as credenciais do Firebase da variável de ambiente
FIREBASE_SA_KEY_JSON = os.getenv("FIREBASE_SA_KEY")
if not FIREBASE_SA_KEY_JSON:
    # Se não encontrar, usa o JSON de fallback (apenas para ambiente de desenvolvimento/teste)
    # ATENÇÃO: Em produção, utilize a variável de ambiente segura.
    FIREBASE_SA_KEY_JSON = """{"type": "service_account", "project_id": "automatizacaoos", "private_key_id": "cd9957ad7e95a872f60b98ede7c08818f053ee68", "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCeEkfbg+HH7VrH\\n/a5WuHiqKlmddmbNgwzuJK5jdUHfJ1WQvcwIEwhxzRJZ0Fb9OMVPyzhoCM4Zieq6\\nOwtyQ7enX+dVyxHMGw+aIVywk6c60tFvnPGIQRq4gwdlKbIxnzmuZFaD+eYYsa08\\nC7WhxN6OrgX2KRRgqx7U5banhEs/xOvl0qHEt1jLgz92s65HqgUH/Fq3EDGWRRR1\\neQfDLstG/UrEVP5/5DRwTU962hVXL4GC1uekf7blhb1IineRCdd774e3bWQjwaaA\\nOepLGA1LR7yBOSPwPuq1pG5nZ5aA2zp1d6ruAde62Wz/fmZ1+Tt8u050GgHOMA2Y\\nRarjfjp/AgMBAAECggEAC69FQYxPqdQ5VDRD6WQsg0..."}"""
    logger.warning("Usando JSON de fallback para FIREBASE_SA_KEY. Configure a var de ambiente em produção.")

db = None
if FIREBASE_SA_KEY_JSON:
    try:
        SERVICE_ACCOUNT_INFO = json.loads(FIREBASE_SA_KEY_JSON)
        cred = credentials.Certificate(SERVICE_ACCOUNT_INFO)
        if not firebase_admin._apps:
            initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase inicializado com sucesso.")
    except Exception as e:
        logger.error(f"Falha ao inicializar Firebase: {e}")
else:
    logger.error("Credenciais Firebase ausentes. O bot não poderá usar o Firestore.")

# --- Constantes Firebase ---
COLLECTION_OS = "ordens_servico"
COLLECTION_ALERTS = "alertas"
COLLECTION_REMINDERS = "lembretes_manuais"

# --- Funções de Utilitário ---

def get_os_collection(chat_id):
    """Retorna a referência da coleção OS para um chat específico."""
    return db.collection(COLLECTION_OS).document(str(chat_id)).collection("os_user")

def get_alerts_collection(chat_id):
    """Retorna a referência da coleção de Alertas para um chat específico."""
    return db.collection(COLLECTION_ALERTS).document(str(chat_id)).collection("os_alerts")

def get_reminders_collection(chat_id):
    """Retorna a referência da coleção de Lembretes Manuais para um chat específico."""
    return db.collection(COLLECTION_REMINDERS).document(str(chat_id)).collection("user_reminders")

async def get_os_list(chat_id):
    """Retorna uma lista de documentos de OS para o chat_id."""
    if not db: return []
    try:
        docs = get_os_collection(chat_id).stream()
        return [doc.to_dict() | {"id": doc.id} for doc in docs]
    except Exception as e:
        logger.error(f"Erro ao buscar OS: {e}")
        return []

async def get_os_by_id(chat_id, os_id):
    """Retorna um documento de OS pelo ID."""
    if not db: return None
    try:
        doc_ref = get_os_collection(chat_id).document(os_id)
        doc = doc_ref.get()
        return doc.to_dict() | {"id": doc.id} if doc.exists else None
    except Exception as e:
        logger.error(f"Erro ao buscar OS por ID: {e}")
        return None

async def save_os(chat_id, os_data, os_id=None):
    """Salva ou atualiza uma OS."""
    if not db: return False, "Conexão Firebase indisponível."
    try:
        if os_id:
            # Atualização
            get_os_collection(chat_id).document(os_id).update(os_data)
            return True, os_id
        else:
            # Criação
            os_data['created_at'] = firestore.SERVER_TIMESTAMP
            _, doc_ref = get_os_collection(chat_id).add(os_data)
            return True, doc_ref.id
    except Exception as e:
        logger.error(f"Erro ao salvar OS: {e}")
        return False, str(e)

async def delete_os(chat_id, os_id):
    """Deleta uma OS e todos os alertas associados."""
    if not db: return False
    try:
        # 1. Deletar a OS principal
        get_os_collection(chat_id).document(os_id).delete()

        # 2. Deletar todos os alertas associados (opcional, mas recomendado)
        alerts_ref = get_alerts_collection(chat_id).where(filter=FieldFilter("os_id", "==", os_id)).stream()
        for alert_doc in alerts_ref:
            alert_doc.reference.delete()

        # 3. Remover jobs de alertas da JobQueue (se houver)
        app = Application.builder().token(TOKEN).build()
        j = app.job_queue
        # Nomes dos jobs são baseados em 'alerta_{os_id}_{alerta_id}'
        # Não é trivial remover jobs por ID sem a referência da JobQueue no contexto, 
        # mas o listener de alertas tratará alertas "órfãos"

        return True
    except Exception as e:
        logger.error(f"Erro ao deletar OS {os_id}: {e}")
        return False

# --- Lógica de PDF ---

async def generate_pdf_report(os_list):
    """Gera um relatório PDF a partir de uma lista de OS."""
    if not PDF_PROCESSOR_AVAILABLE or not os_list:
        return None

    # Mapeamento e preparação dos dados
    data = []
    for os_item in os_list:
        data.append({
            "ID": os_item.get("id", "N/A"),
            "Descrição": os_item.get("descricao", "N/A"),
            "Tipo": os_item.get("tipo", "N/A"),
            "Status": os_item.get("status", "N/A"),
            "Última Atualização": os_item.get("last_update", "N/A")
        })

    df = pd.DataFrame(data)

    # Cria o buffer HTML
    html_content = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: sans-serif; margin: 20px; }}
            h1 {{ color: #004d99; border-bottom: 2px solid #ccc; padding-bottom: 5px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
        </style>
    </head>
    <body>
        <h1>Relatório de Ordens de Serviço</h1>
        <p>Data de Geração: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</p>
        {df.to_html(index=False)}
    </body>
    </html>
    """
    
    # Geração do PDF usando PyMuPDF (fitz)
    try:
        doc = fitz.open() # Novo documento PDF
        # Adiciona uma nova página e insere o HTML
        page = doc.new_page(width=595, height=842) # A4 size
        
        # Insere o HTML na página
        # PyMuPDF precisa de um layout para renderizar o HTML
        rect = page.rect
        fitz.insert_html(page, rect, html_content)

        # Salva o PDF em um buffer de bytes
        pdf_bytes = doc.tobytes()
        doc.close()
        return io.BytesIO(pdf_bytes)

    except Exception as e:
        logger.error(f"Erro ao gerar PDF: {e}")
        return None


# --- Funções de Alertas e Lembretes (Job Queue) ---

# Função que será executada pelo JobQueue
async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Envia o alerta/lembrete para o usuário."""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    message = job_data["message"]
    
    try:
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"🚨 **Lembrete Agendado!** 🚨\n\n{message}",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Lembrete enviado para o chat {chat_id}.")
        
        # Se for um lembrete manual, deletar após envio
        if job_data.get("is_manual") and db:
            reminder_id = job_data.get("reminder_id")
            if reminder_id:
                 get_reminders_collection(chat_id).document(reminder_id).delete()
                 logger.info(f"Lembrete manual {reminder_id} deletado do Firestore.")

    except Exception as e:
        logger.error(f"Falha ao enviar lembrete para {chat_id}: {e}")

def schedule_alert(job_queue: JobQueue, chat_id: int, reminder_data: dict, is_manual: bool, unique_id: str):
    """Agenda um job na JobQueue."""
    
    # Converte o timestamp Firestore para datetime com timezone (UTC)
    if isinstance(reminder_data['prazo'], str):
        # Para alertas manuais que são strings de data/hora
        try:
            # Tenta parsear formato mais comum
            prazo_dt = datetime.strptime(reminder_data['prazo'], '%Y-%m-%d %H:%M')
        except ValueError:
            # Fallback para formato apenas de data, assumindo 00:00
             try:
                prazo_dt = datetime.strptime(reminder_data['prazo'], '%Y-%m-%d')
             except ValueError:
                logger.error(f"Formato de prazo inválido: {reminder_data['prazo']}")
                return False

        # Assume UTC (ou o fuso horário que o seu servidor usa)
        prazo_dt = prazo_dt.replace(tzinfo=timezone.utc)
    else:
        # Para alertas de OS que podem vir como TimeStamp do Firebase
        prazo_dt = reminder_data['prazo'].astimezone(timezone.utc)
    
    message = reminder_data['descricao']

    # Se a data/hora já passou, não agenda
    if prazo_dt <= datetime.now(timezone.utc):
        logger.warning(f"Tentativa de agendar job no passado: {prazo_dt}")
        return False

    job_name = f"{'manual' if is_manual else 'os'}_{unique_id}_{chat_id}"

    job_queue.run_once(
        send_reminder_job, 
        prazo_dt, 
        data={
            "chat_id": chat_id, 
            "message": message,
            "is_manual": is_manual,
            "reminder_id": unique_id if is_manual else None # Apenas para o manual
        },
        name=job_name
    )
    logger.info(f"Job agendado: {job_name} para {prazo_dt}")
    return True

# --- Handlers do Telegram (Fluxo de Conversação) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e exibe o menu principal."""
    if update.message:
        await update.message.reply_text(
            "Olá! Sou o bot de Gestão de Ordens de Serviço. Escolha uma opção:",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    return MENU

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Volta ao menu principal a partir de um CallbackQuery."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Menu Principal. Escolha uma opção:",
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU

def main_menu_keyboard():
    """Retorna o teclado do menu principal."""
    keyboard = [
        [InlineKeyboardButton("➕ Criar Nova OS", callback_data='criar_os')],
        [InlineKeyboardButton("📜 Listar/Visualizar OS", callback_data='listar_os')],
        [InlineKeyboardButton("✏️ Atualizar OS", callback_data='atualizar_os')],
        [InlineKeyboardButton("❌ Excluir OS", callback_data='excluir_os')],
        [InlineKeyboardButton("🔔 Gestão de Alertas", callback_data='alerta_menu')],
        [InlineKeyboardButton("⏰ Lembrete Manual", callback_data='lembrete_manual_start')],
        [InlineKeyboardButton("📄 Exportar para PDF", callback_data='exportar_pdf')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa atual."""
    if update.message:
        await update.message.reply_text(
            "Operação cancelada. Retornando ao menu principal.",
            reply_markup=main_menu_keyboard()
        )
    return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a comandos não reconhecidos."""
    if update.message:
        await update.message.reply_text(
            "Comando não reconhecido. Use /start ou escolha uma opção do menu.",
            reply_markup=main_menu_keyboard()
        )
    return MENU

async def os_list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, callback_prefix: str) -> int:
    """Gera o menu de listagem de OSs para seleção."""
    query = update.callback_query
    chat_id = query.message.chat_id
    
    os_list = await get_os_list(chat_id)
    
    keyboard = []
    if os_list:
        for os_item in os_list:
            os_id = os_item['id']
            # Exibe ID e Descrição
            text = f"[{os_id[:4]}] {os_item.get('descricao', 'Sem Descrição')} - ({os_item.get('status', 'N/A')})"
            keyboard.append([InlineKeyboardButton(text, callback_data=f'{callback_prefix}{os_id}')])
    else:
        keyboard.append([InlineKeyboardButton("Nenhuma OS encontrada.", callback_data='menu')])

    keyboard.append([InlineKeyboardButton("🔙 Voltar ao Menu", callback_data='menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"**{title}**\n\nSelecione a Ordem de Serviço:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU

# --- Fluxo de Criação/Atualização de OS ---

async def prompt_os_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo para criar uma nova OS, pedindo a descrição."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['current_os'] = {}
    
    await query.edit_message_text(
        "**Nova OS:** Por favor, digite uma descrição detalhada para a Ordem de Serviço.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_DESCRICAO

async def receive_os_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e pede o tipo de OS."""
    context.user_data['current_os']['descricao'] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("🔧 Manutenção", callback_data='tipo_Manutenção')],
        [InlineKeyboardButton("⚙️ Instalação", callback_data='tipo_Instalação')],
        [InlineKeyboardButton("🛠️ Reparo", callback_data='tipo_Reparo')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Descrição recebida. Agora, qual é o **Tipo** desta OS?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO

async def receive_os_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo de OS e pede o status."""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.split('_')[1]
    context.user_data['current_os']['tipo'] = tipo
    
    keyboard = [
        [InlineKeyboardButton("🟡 Aberta", callback_data='status_Aberta')],
        [InlineKeyboardButton("🔵 Em Andamento", callback_data='status_Em Andamento')],
        [InlineKeyboardButton("🟢 Concluída", callback_data='status_Concluída')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"Tipo ({tipo}) salvo. Por favor, escolha o **Status** inicial desta OS:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_STATUS

async def save_new_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o status final e salva a nova OS no Firebase."""
    query = update.callback_query
    await query.answer()
    
    status = query.data.split('_')[1]
    os_data = context.user_data['current_os']
    os_data['status'] = status
    os_data['last_update'] = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M:%S')
    
    chat_id = query.message.chat_id
    success, os_id = await save_os(chat_id, os_data)

    if success:
        await query.edit_message_text(
            f"✅ **OS Criada com Sucesso!**\n\nID: `{os_id}`\nDescrição: {os_data['descricao']}\nStatus Inicial: {status}",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text(
            f"❌ **Erro ao criar OS:** {os_id}",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        
    context.user_data.pop('current_os', None)
    return MENU

# --- Fluxo de Visualização/Listagem ---

async def show_os_list_for_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo para visualização, listando as OSs."""
    return await os_list_menu(update, context, "Visualizar Ordens de Serviço", "view_os_")

async def view_os_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra os detalhes de uma OS específica."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace('view_os_', '')
    chat_id = query.message.chat_id
    
    os_data = await get_os_by_id(chat_id, os_id)
    
    if os_data:
        text = f"""
**Detalhes da OS: `{os_data['id']}`**
Descrição: {os_data.get('descricao', 'N/A')}
Tipo: {os_data.get('tipo', 'N/A')}
**Status:** {os_data.get('status', 'N/A')}
Criada em: {os_data.get('created_at', datetime.now()).strftime('%d/%m/%Y %H:%M:%S')}
Última Atualização: {os_data.get('last_update', 'N/A')}
"""
    else:
        text = "❌ OS não encontrada."
        
    keyboard = [[InlineKeyboardButton("🔙 Voltar à Lista", callback_data='listar_os')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text.strip(),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU # Volta ao menu de listagem

# --- Fluxo de Atualização de OS ---

async def show_os_list_for_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de atualização, listando as OSs."""
    return await os_list_menu(update, context, "Atualizar Ordem de Serviço", "update_os_select_")

async def prompt_update_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a OS selecionada para atualização e pede o novo status."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace('update_os_select_', '')
    context.user_data['os_to_update_id'] = os_id
    
    keyboard = [
        [InlineKeyboardButton("🟡 Aberta", callback_data='update_status_Aberta')],
        [InlineKeyboardButton("🔵 Em Andamento", callback_data='update_status_Em Andamento')],
        [InlineKeyboardButton("🟢 Concluída", callback_data='update_status_Concluída')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"OS `{os_id[:4]}` selecionada. Escolha o **novo Status**:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ATUALIZACAO

async def finalize_update_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva a atualização de status da OS."""
    query = update.callback_query
    await query.answer()
    
    new_status = query.data.replace('update_status_', '')
    os_id = context.user_data.pop('os_to_update_id', None)
    chat_id = query.message.chat_id

    if os_id:
        update_data = {
            'status': new_status,
            'last_update': datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M:%S')
        }
        success, _ = await save_os(chat_id, update_data, os_id)
        
        if success:
            await query.edit_message_text(
                f"✅ OS `{os_id[:4]}` atualizada para **{new_status}**.",
                reply_markup=main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(
                f"❌ Erro ao atualizar OS `{os_id[:4]}`.",
                reply_markup=main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
    else:
        await query.edit_message_text(
            "❌ ID da OS para atualização não encontrado. Tente novamente.",
            reply_markup=main_menu_keyboard()
        )
        
    return MENU

# --- Fluxo de Deleção de OS ---

async def show_os_list_for_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de exclusão, listando as OSs."""
    return await os_list_menu(update, context, "Excluir Ordem de Serviço", "delete_os_select_")

async def confirm_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede confirmação para deletar a OS selecionada."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace('delete_os_select_', '')
    context.user_data['os_to_delete_id'] = os_id
    
    keyboard = [
        [InlineKeyboardButton("SIM, EXCLUIR DEFINITIVAMENTE", callback_data=f'confirm_delete_{os_id}')],
        [InlineKeyboardButton("NÃO, VOLTAR", callback_data='menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"⚠️ **Confirmação de Exclusão**\n\nTem certeza que deseja EXCLUIR a OS `{os_id[:4]}` e todos os alertas associados?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_DELETE_OS

async def finalize_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Deleta a OS e seus alertas associados."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace('confirm_delete_', '')
    chat_id = query.message.chat_id
    
    success = await delete_os(chat_id, os_id)
    
    if success:
        await query.edit_message_text(
            f"🗑️ OS `{os_id[:4]}` e alertas associados **EXCLUÍDOS** com sucesso.",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text(
            f"❌ Erro ao excluir OS `{os_id[:4]}`. Verifique a conexão com o Firebase.",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    
    context.user_data.pop('os_to_delete_id', None)
    return MENU

# --- Fluxo de Alertas (Lembretes vinculados à OS) ---

async def alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu para gestão de alertas de OS."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Incluir Novo Alerta", callback_data='incluir_alerta_menu')],
        [InlineKeyboardButton("🗑️ Remover Alerta Existente", callback_data='remover_alerta_menu')],
        [InlineKeyboardButton("🔙 Voltar ao Menu", callback_data='menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "**Gestão de Alertas de OS**\n\nSelecione o que deseja fazer com os alertas de Ordens de Serviço.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ALERTA

async def show_os_list_for_alert_inclusion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lista as OSs para o usuário escolher qual receberá o alerta."""
    return await os_list_menu(update, context, "Incluir Alerta", "alert_os_select_")

async def prompt_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a OS e pede a descrição do alerta."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace('alert_os_select_', '')
    context.user_data['alert_os_id'] = os_id

    await query.edit_message_text(
        f"OS `{os_id[:4]}` selecionada. Digite o **texto do alerta** (o que deve ser lembrado).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data='alerta_menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_INCLUSAO

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e pede o prazo do alerta (data e hora)."""
    context.user_data['alert_descricao'] = update.message.text
    
    await update.message.reply_text(
        "Texto do alerta salvo. Agora, digite a **data e hora** para o alerta (formato YYYY-MM-DD HH:MM), ex: `2025-12-31 10:00`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data='alerta_menu')]])
    )
    return PROMPT_ID_ALERTA

async def save_os_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o prazo, salva o alerta no Firebase e agenda o job."""
    chat_id = update.message.chat_id
    prazo_str = update.message.text
    os_id = context.user_data.pop('alert_os_id', None)
    descricao = context.user_data.pop('alert_descricao', None)
    
    if not os_id or not descricao:
        await update.message.reply_text("❌ Erro interno. Tente novamente ou use /start.")
        return MENU
    
    # Validação do formato de data/hora
    match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})$', prazo_str)
    if not match:
        await update.message.reply_text(
            "❌ Formato de data/hora inválido. Use YYYY-MM-DD HH:MM. Tente novamente."
        )
        return PROMPT_ID_ALERTA

    try:
        # Tenta converter para datetime e garantir que não está no passado
        prazo_dt = datetime.strptime(prazo_str, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
        if prazo_dt <= datetime.now(timezone.utc):
            await update.message.reply_text("❌ A data e hora do alerta não pode estar no passado. Tente novamente.")
            return PROMPT_ID_ALERTA
            
    except ValueError:
        await update.message.reply_text("❌ Formato de data/hora inválido. Tente novamente.")
        return PROMPT_ID_ALERTA

    # Salvar no Firestore
    alert_data = {
        "os_id": os_id,
        "descricao": descricao,
        "prazo": prazo_dt, # Salva como datetime
        "chat_id": chat_id,
        "agendado_em": firestore.SERVER_TIMESTAMP
    }

    if not db:
        await update.message.reply_text("❌ Erro de conexão com o Firebase. Não foi possível salvar o alerta.")
        return MENU

    try:
        _, doc_ref = get_alerts_collection(chat_id).add(alert_data)
        alert_id = doc_ref.id
        
        # Agendar Job
        schedule_alert(
            context.application.job_queue,
            chat_id,
            {"descricao": descricao, "prazo": prazo_dt},
            is_manual=False,
            unique_id=alert_id
        )

        await update.message.reply_text(
            f"✅ Alerta para OS `{os_id[:4]}` agendado com sucesso!\nSerá enviado em: {prazo_str}",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao salvar/agendar alerta: {e}")
        await update.message.reply_text("❌ Erro ao salvar/agendar o alerta. Tente novamente.")
        
    return MENU

# --- Fluxo de Lembrete Manual ---

async def prompt_lembrete_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo para lembrete manual e pede o texto."""
    query = update.callback_query
    await query.answer()

    context.user_data['reminder_data'] = {}
    
    await query.edit_message_text(
        "**Lembrete Manual:** Por favor, digite o **texto** completo do seu lembrete.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o texto do lembrete e pede a data/hora."""
    context.user_data['reminder_data']['descricao'] = update.message.text
    
    await update.message.reply_text(
        "Texto do lembrete salvo. Agora, digite a **data e hora** para o lembrete (formato YYYY-MM-DD HH:MM), ex: `2025-12-31 10:00`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data='menu')]])
    )
    return PROMPT_LEMBRETE_DATA

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora, salva o lembrete manual e agenda o job."""
    chat_id = update.message.chat_id
    prazo_str = update.message.text
    descricao = context.user_data['reminder_data'].pop('descricao', None)

    if not descricao:
        await update.message.reply_text("❌ Erro interno. Tente novamente ou use /start.")
        return MENU
    
    match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})$', prazo_str)
    if not match:
        await update.message.reply_text(
            "❌ Formato de data/hora inválido. Use YYYY-MM-DD HH:MM. Tente novamente."
        )
        return PROMPT_LEMBRETE_DATA

    try:
        prazo_dt = datetime.strptime(prazo_str, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
        if prazo_dt <= datetime.now(timezone.utc):
            await update.message.reply_text("❌ A data e hora do lembrete não pode estar no passado. Tente novamente.")
            return PROMPT_LEMBRETE_DATA
            
    except ValueError:
        await update.message.reply_text("❌ Formato de data/hora inválido. Tente novamente.")
        return PROMPT_LEMBRETE_DATA

    # Salvar no Firestore
    reminder_data = {
        "descricao": descricao,
        "prazo": prazo_dt,
        "chat_id": chat_id,
        "agendado_em": firestore.SERVER_TIMESTAMP
    }

    if not db:
        await update.message.reply_text("❌ Erro de conexão com o Firebase. Não foi possível salvar o lembrete.")
        return MENU

    try:
        _, doc_ref = get_reminders_collection(chat_id).add(reminder_data)
        reminder_id = doc_ref.id
        
        # Agendar Job
        schedule_alert(
            context.application.job_queue,
            chat_id,
            {"descricao": descricao, "prazo": prazo_dt},
            is_manual=True,
            unique_id=reminder_id
        )

        await update.message.reply_text(
            f"✅ Lembrete Manual agendado com sucesso!\nSerá enviado em: {prazo_str}",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao salvar/agendar lembrete manual: {e}")
        await update.message.reply_text("❌ Erro ao salvar/agendar o lembrete manual. Tente novamente.")
        
    context.user_data.pop('reminder_data', None)
    return MENU
    
# --- Fluxo de Exportação de PDF ---

async def exportar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca os dados e envia o relatório PDF."""
    query = update.callback_query
    await query.answer("Gerando relatório...")
    
    if not PDF_PROCESSOR_AVAILABLE:
        await query.edit_message_text(
            "❌ O recurso de Exportação de PDF está indisponível (PyMuPDF e/ou pandas não instalados).",
            reply_markup=main_menu_keyboard()
        )
        return MENU

    chat_id = query.message.chat_id
    os_list = await get_os_list(chat_id)
    
    if not os_list:
        await query.edit_message_text(
            "⚠️ Não há Ordens de Serviço cadastradas para gerar o relatório.",
            reply_markup=main_menu_keyboard()
        )
        return MENU

    pdf_buffer = await generate_pdf_report(os_list)

    if pdf_buffer:
        pdf_buffer.seek(0)
        filename = f"relatorio_os_{datetime.now().strftime('%Y%m%d')}.pdf"
        
        # Envia o arquivo PDF
        await context.bot.send_document(
            chat_id=chat_id, 
            document=InputFile(pdf_buffer, filename=filename),
            caption=f"✅ Relatório de Ordens de Serviço gerado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}.",
            reply_markup=main_menu_keyboard()
        )
        await query.delete_message()
    else:
        await query.edit_message_text(
            "❌ Erro ao gerar o arquivo PDF. Consulte os logs do servidor.",
            reply_markup=main_menu_keyboard()
        )
    
    return MENU

# --- Funções de Job Queue e Utilidades de Servidor ---

async def start_up_jobs(application: Application) -> None:
    """Função de inicialização para agendar jobs de alertas e lembretes pendentes."""
    if not db: 
        logger.warning("Firebase não inicializado. Não foi possível carregar jobs pendentes.")
        return

    job_queue = application.job_queue
    now = datetime.now(timezone.utc)
    
    # 1. Carregar Alertas de OS pendentes
    try:
        # Busca em todos os chats que têm alertas
        all_alert_docs = db.collection_group("os_alerts").stream()
        for doc in all_alert_docs:
            alert = doc.to_dict()
            alert_id = doc.id
            chat_id = alert.get('chat_id')
            prazo_dt = alert.get('prazo').astimezone(timezone.utc)
            
            if prazo_dt > now:
                schedule_alert(job_queue, chat_id, alert, is_manual=False, unique_id=alert_id)
            else:
                logger.warning(f"Alerta de OS {alert_id} ignorado, pois está no passado.")
                # Opcional: deletar alertas muito antigos/passados
                # doc.reference.delete()
    except Exception as e:
        logger.error(f"Erro ao carregar Alertas de OS pendentes: {e}")

    # 2. Carregar Lembretes Manuais pendentes
    try:
        # Busca em todos os chats que têm lembretes manuais
        all_reminder_docs = db.collection_group("user_reminders").stream()
        for doc in all_reminder_docs:
            reminder = doc.to_dict()
            reminder_id = doc.id
            chat_id = reminder.get('chat_id')
            prazo_dt = reminder.get('prazo').astimezone(timezone.utc)
            
            if prazo_dt > now:
                schedule_alert(job_queue, chat_id, reminder, is_manual=True, unique_id=reminder_id)
            else:
                logger.warning(f"Lembrete Manual {reminder_id} ignorado e deletado, pois está no passado.")
                doc.reference.delete()
    except Exception as e:
        logger.error(f"Erro ao carregar Lembretes Manuais pendentes: {e}")
        
    logger.info("Verificação de jobs pendentes concluída.")


async def keep_alive():
    """Tarefa que faz uma requisição periódica para evitar o 'sleep' em algumas infraestruturas (ex: Heroku)."""
    if WEBHOOK_URL and WEBHOOK_URL.startswith("https://"):
        logger.info(f"Iniciando tarefa keep_alive para {WEBHOOK_URL}...")
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    # Envia uma requisição GET simples para a URL principal do webhook
                    async with session.get(WEBHOOK_URL) as response:
                        logger.debug(f"Keep-alive status: {response.status}")
            except Exception as e:
                logger.error(f"Erro no keep_alive: {e}")
            # Espera 20 minutos (1200 segundos)
            await asyncio.sleep(1200)

# --- Função Principal ---

def main():
    """Inicia o bot usando Webhook ou Long Polling."""
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN não configurado. O bot não pode ser iniciado.")
        return

    # 1. Cria a Application e a JobQueue
    application = Application.builder().token(TOKEN).concurrent_updates(True).build()

    # Adiciona a função de inicialização da JobQueue
    application.add_handler(CommandHandler("start", start))
    application.post_init = start_up_jobs
    
    # 2. Configura o ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(menu_callback, pattern='^menu$')],
        
        states={
            MENU: [
                CallbackQueryHandler(prompt_os_description, pattern='^criar_os$'),
                CallbackQueryHandler(show_os_list_for_view, pattern='^listar_os$'),
                CallbackQueryHandler(show_os_list_for_update, pattern='^atualizar_os$'),
                CallbackQueryHandler(show_os_list_for_delete, pattern='^excluir_os$'),
                CallbackQueryHandler(alerta_menu, pattern='^alerta_menu$'),
                CallbackQueryHandler(prompt_lembrete_id, pattern='^lembrete_manual_start$'),
                CallbackQueryHandler(exportar_pdf, pattern='^exportar_pdf$'),
            ],
            
            # Fluxo de Criação de OS
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_description),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(receive_os_type, pattern='^tipo_'),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            PROMPT_STATUS: [
                CallbackQueryHandler(save_new_os, pattern='^status_'),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            
            # Fluxo de Visualização (os_list_menu tem seu próprio retorno)
            # view_os_details volta para MENU de forma implícita (opção 'voltar a lista')
            
            # Fluxo de Atualização de Status
            PROMPT_ATUALIZACAO: [
                CallbackQueryHandler(finalize_update_os, pattern='^update_status_'),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            
            # Fluxo de Deleção
            PROMPT_DELETE_OS: [
                CallbackQueryHandler(finalize_delete_os, pattern='^confirm_delete_'),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            
            # Fluxo de Alerta de OS
            PROMPT_ALERTA: [
                CallbackQueryHandler(show_os_list_for_alert_inclusion, pattern='^incluir_alerta_menu$'),
                # A remoção de alerta é complexa e foi omitida por ser um fluxo de conversação muito longo.
                # Se for necessário, precisará de uma nova função e estado. O botão de retorno volta para MENU.
                CallbackQueryHandler(menu_callback, pattern='^menu$'), 
            ],
            PROMPT_INCLUSAO: [ # Recebe a descrição do alerta
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao),
                CallbackQueryHandler(alerta_menu, pattern='^alerta_menu$'),
            ],
            PROMPT_ID_ALERTA: [ # Recebe o prazo do alerta (data e hora)
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_os_alert),
                CallbackQueryHandler(alerta_menu, pattern='^alerta_menu$'),
            ],

            # Fluxo de Lembrete Manual
            PROMPT_ID_LEMBRETE: [ # Recebe o texto do lembrete
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_DATA: [ # Recebe a data/hora do lembrete
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete),
                CallbackQueryHandler(menu_callback, pattern='^menu$'),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(menu_callback, pattern='^menu$'), 
            MessageHandler(filters.COMMAND, fallback_command), 
        ],
    )

    # Adiciona o ConversationHandler
    application.add_handler(conv_handler)
    
    # 3. Configuração do Webhook ou Polling
    if WEBHOOK_URL and TOKEN:
        try:
            # Define a URL do webhook no Telegram
            logger.info(f"A iniciar Webhook em http://0.0.0.0:{PORT}{WEBHOOK_PATH}")
            # Inicia o Webhook
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN, 
                webhook_url=WEBHOOK_URL + WEBHOOK_PATH, 
                # Adiciona a tarefa keep_alive para manter o servidor acordado
                # start_on_init=True executa a JobQueue.post_init
                bootstrap_retries=-1 # Tenta reconfigurar o webhook infinitamente
            )
            # Inicia a tarefa keep_alive
            asyncio.run(keep_alive())
            
        except Exception as e:
            logger.error(f"Falha ao iniciar o Webhook: {e}")
            logger.info("Tentando modo Long Polling (desativar para produção em infraestrutura Webhook).")
            # Fallback para Long Polling (para debug ou ambientes sem suporte a webhook)
            application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        # Se WEBHOOK_URL ou TOKEN não estiver configurado, usa Long Polling
        logger.info("WEBHOOK_URL e/ou TELEGRAM_TOKEN não configurado. Iniciando Long Polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
