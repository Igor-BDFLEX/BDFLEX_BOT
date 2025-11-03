# bot_os_telegram.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK MODE)
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
from datetime import datetime, timedelta
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
        def __init__(self, *args, **kwargs): pass
        def to_csv(self, *args, **kwargs): pass
    pd = MockDataFrame()

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app
from google.cloud.firestore_v1.base_query import FieldFilter # Para filtros de consulta

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
    JobQueue,
    ApplicationBuilder
)
from telegram.constants import ParseMode

# --- Configuração de Logging ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Configuração de Estados para ConversationHandler ---

MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG, LEMBRETE_MENU = range(14)

# --- Variáveis de Ambiente e Configuração ---

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
# Variáveis de Webhook (necessárias para o modo Webhook)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = f'/{TOKEN}' # Usar o token como path para segurança

# O conteúdo do service account JSON deve ser carregado.
# ATENÇÃO: O JSON abaixo é um placeholder. Em produção, ele deve ser carregado com 
# segurança de um arquivo ou variável de ambiente. O conteúdo real do 'app.json' deve 
# ser usado aqui.
FIREBASE_SERVICE_ACCOUNT_JSON_STRING = os.environ.get("FIREBASE_SERVICE_ACCOUNT", """
{
  "type": "service_account",
  "project_id": "automatizacaoos",
  "private_key_id": "cd9957ad7e95a872f60b98ede7c08818f053ee68",
  "private_key": "-----BEGIN PRIVATE KEY-----\\n... (SUA CHAVE PRIVADA AQUI) ...\\n-----END PRIVATE KEY-----\\n",
  "client_email": "firebase-adminsdk-...",
  "client_id": "...",
  "auth_uri": "...",
  "token_uri": "...",
  "auth_provider_x509_cert_url": "...",
  "client_x509_cert_url": "...",
  "universe_domain": "googleapis.com"
}
""")

if not TOKEN:
    logger.error("TELEGRAM_TOKEN não está configurado. O bot não pode ser iniciado.")

# --- Firebase Init ---

db = None
def firebase_init():
    """Inicializa o Firebase se ainda não tiver sido inicializado."""
    global db
    if not firebase_admin._apps:
        try:
            # Tenta carregar o JSON do service account
            service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON_STRING)
            cred = credentials.Certificate(service_account_info)
            initialize_app(cred)
            db = firestore.client()
            logger.info("Firebase e Firestore inicializados com sucesso.")
        except Exception as e:
            logger.error(f"Erro ao inicializar Firebase/Firestore: {e}. Verifique o JSON de Service Account.")
            # Set db to None if initialization fails
            db = None

def get_db():
    """Retorna a instância do Firestore. Inicializa se necessário."""
    if db is None:
        firebase_init()
    return db

def get_os_collection():
    """Retorna a referência da coleção de Ordens de Serviço."""
    return get_db().collection('ordens_servico')

def get_lembrete_collection():
    """Retorna a referência da coleção de Lembretes."""
    return get_db().collection('lembretes')

# --- Funções de Utilidade ---

def validate_date(date_text: str) -> datetime | None:
    """Valida se uma string é uma data válida no formato DD/MM/AAAA HH:MM."""
    # Expressão regular para DD/MM/AAAA HH:MM ou DD/MM/AAAA
    pattern = r"^\d{2}/\d{2}/\d{4}( \d{2}:\d{2})?$"
    if not re.match(pattern, date_text):
        return None

    formats = ["%d/%m/%Y %H:%M", "%d/%m/%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue
    return None

def format_date_br(dt: datetime) -> str:
    """Formata um objeto datetime para o formato DD/MM/AAAA HH:MM."""
    return dt.strftime("%d/%m/%Y %H:%M")

async def keep_alive():
    """Função assíncrona para enviar requisições periódicas para manter o serviço ativo."""
    if WEBHOOK_URL:
        # PING para evitar timeout no serviço de hospedagem
        await asyncio.sleep(15 * 60) # Espera 15 minutos
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(WEBHOOK_URL) as response:
                    logger.info(f"Keep-alive ping para {WEBHOOK_URL} status: {response.status}")
        except Exception as e:
            logger.error(f"Erro no keep-alive: {e}")

# --- Funções de Relatório (PDF) ---

async def generate_pdf_report(os_list: list[dict], chat_id: int) -> io.BytesIO | None:
    """Gera um relatório PDF com a lista de OS."""
    if not PDF_PROCESSOR_AVAILABLE:
        logger.warning("Tentativa de gerar PDF sem PyMuPDF/Pandas instalados.")
        return None

    try:
        # Preparar dados para o DataFrame
        data = []
        for os_data in os_list:
            data.append({
                "ID": os_data.get('id', 'N/A'),
                "Descrição": os_data.get('descricao', 'N/A'),
                "Tipo": os_data.get('tipo', 'N/A'),
                "Status": os_data.get('status', 'N/A'),
                "Criado em": os_data.get('data_criacao', 'N/A'),
                "Última Atualização": os_data.get('data_atualizacao', 'N/A'),
            })

        df = pd.DataFrame(data)

        # Usar um buffer de bytes para o PDF
        pdf_buffer = io.BytesIO()

        # Configuração básica do documento (usando PyMuPDF/fitz)
        doc = fitz.open()
        page = doc.new_page()
        
        # Converte o DataFrame para string CSV/texto
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        
        text = f"Relatório de Ordens de Serviço (OS)\n\nGerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        text += "--- Dados das OS ---\n"
        text += csv_buffer.getvalue()

        # Insere texto no PDF
        rect = page.rect
        text_area = rect.x0 + 50, rect.y0 + 50, rect.x1 - 50, rect.y1 - 50
        page.insert_text((text_area[0], text_area[1]), text, fontsize=10, rotate=0)

        # Salva o PDF no buffer
        doc.save(pdf_buffer)
        doc.close()
        pdf_buffer.seek(0)
        return pdf_buffer

    except Exception as e:
        logger.error(f"Erro ao gerar relatório PDF: {e}")
        # Enviar uma mensagem de erro ao usuário sobre o PDF
        # Nota: O bot precisa ter o 'application' disponível para usar 'context.bot'
        # Em um handler real, isso seria 'context.bot.send_message(...)'.
        # Aqui, estamos dentro de uma função auxiliar, então precisamos do chat_id.
        return None

# --- Funções do Bot (Handlers) ---

def get_menu_keyboard(update_id: str | None = None) -> InlineKeyboardMarkup:
    """Gera o teclado do menu principal."""
    keyboard = [
        [InlineKeyboardButton("➕ Criar Nova OS", callback_data='criar_os_start')],
        [InlineKeyboardButton("🔍 Ver Minhas OS", callback_data='ver_os')],
        [InlineKeyboardButton("✏️ Atualizar OS", callback_data='atualizar_os_menu')],
        [InlineKeyboardButton("🗑️ Excluir OS", callback_data='excluir_os_menu')],
        [InlineKeyboardButton("⏰ Lembretes e Alertas", callback_data='lembrete_menu')],
        [InlineKeyboardButton("📄 Exportar para PDF", callback_data='exportar_pdf_confirm')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e exibe o menu principal."""
    get_db()

    user = update.effective_user
    logger.info(f"Usuário {user.id} ({user.full_name}) iniciou a conversa.")

    if update.message:
        await update.message.reply_html(
            f"👋 Olá, <b>{user.first_name}!</b>\n\nSou o Bot de Gestão de Ordens de Serviço. Como posso ajudar?",
            reply_markup=get_menu_keyboard(),
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            "Bem-vindo(a) de volta ao menu principal.",
            reply_markup=get_menu_keyboard()
        )
    
    return MENU

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Volta ao menu principal."""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Menu Principal:",
            reply_markup=get_menu_keyboard()
        )
        return MENU
    await update.message.reply_text("Menu Principal:", reply_markup=get_menu_keyboard())
    return MENU

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manipula todas as consultas de callback (botões inline)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Menu Principal ---
    if data == 'menu':
        return await start(update, context)
    
    # --- Criação de OS ---
    elif data == 'criar_os_start':
        return await create_os_start(update, context)

    # --- Visualização ---
    elif data == 'ver_os':
        return await view_os(update, context)

    # --- Atualização de OS ---
    elif data == 'atualizar_os_menu':
        context.user_data['action'] = 'update'
        await query.edit_message_text(
            "Digite o <b>ID da OS</b> que deseja atualizar, ou /cancel para voltar.",
            parse_mode=ParseMode.HTML
        )
        return PROMPT_OS
    
    # --- Exclusão de OS ---
    elif data == 'excluir_os_menu':
        context.user_data['action'] = 'delete'
        await query.edit_message_text(
            "Digite o <b>ID da OS</b> que deseja excluir, ou /cancel para voltar.",
            parse_mode=ParseMode.HTML
        )
        return PROMPT_OS
    elif data.startswith('confirm_delete_'):
        os_id = data.split('_')[-1]
        return await delete_os(update, context, os_id)

    # --- Lembretes e Alertas ---
    elif data == 'lembrete_menu':
        return await prompt_lembrete_menu(update, context)
    elif data == 'lembrete_manual_start':
        return await prompt_id_lembrete(update, context)

    # --- Exportar PDF ---
    elif data == 'exportar_pdf_confirm':
        await query.edit_message_text(
            "Tem certeza que deseja exportar todas as Ordens de Serviço para PDF?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar Exportação", callback_data='exportar_pdf')],
                [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
            ])
        )
        return MENU

    elif data == 'exportar_pdf':
        return await exportar_pdf(update, context)
    
    # --- Alertas (dentro da OS) ---
    elif data.startswith('gerir_alerta_'):
        os_id = data.split('_')[-1]
        return await gerir_alerta_menu(update, context, os_id)
    elif data.startswith('incluir_alerta_'):
        context.user_data['target_os_id'] = data.split('_')[-1]
        await query.edit_message_text(
            "Digite a descrição do lembrete (ex: 'Ligar para o cliente sobre o orçamento').",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{context.user_data["target_os_id"]}'), InlineKeyboardButton("Cancelar", callback_data='menu')]])
        )
        return PROMPT_INCLUSAO
    elif data.startswith('remover_alerta_menu_'):
        return await remover_alerta_menu(update, context, data.split('_')[-1])
    elif data.startswith('remover_alerta_'):
        _, _, os_id, alerta_id = data.split('_')
        return await remover_alerta(update, context, os_id, alerta_id)


    # --- Comandos não reconhecidos no callback ---
    else:
        await query.edit_message_text("Ação não reconhecida. Voltando ao Menu Principal.")
        return await start(update, context)

# --- Handlers de Criação/Atualização de OS ---

async def create_os_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o processo de criação de uma nova OS."""
    context.user_data.clear() # Limpa dados anteriores
    context.user_data['step'] = 'descricao'
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "<b>Passo 1/4: Descrição</b>\n\nQual é a descrição da Ordem de Serviço? (ex: 'Instalação de rede na sala de reuniões')",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]])
        )
    return PROMPT_DESCRICAO

async def receive_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição da OS e pede o tipo."""
    text = update.message.text
    
    # Se estiver em fluxo de atualização de descrição
    if context.user_data.get('field_to_update') == 'descricao':
        context.user_data['temp_new_value'] = text
        return await receive_new_value_and_save(update, context)

    # Se estiver em fluxo de criação
    context.user_data['descricao'] = text

    keyboard = [
        [InlineKeyboardButton("Manutenção", callback_data='tipo_Manutenção')],
        [InlineKeyboardButton("Instalação", callback_data='tipo_Instalação')],
        [InlineKeyboardButton("Orçamento", callback_data='tipo_Orçamento')],
        [InlineKeyboardButton("Outro", callback_data='tipo_Outro')],
    ]
    
    await update.message.reply_html(
        "<b>Passo 2/4: Tipo</b>\n\nQual é o tipo desta Ordem de Serviço?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PROMPT_TIPO

async def receive_os_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo da OS e pede o status."""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.split('_')[1]
    context.user_data['tipo'] = tipo

    keyboard = [
        [InlineKeyboardButton("Pendente", callback_data='status_Pendente')],
        [InlineKeyboardButton("Em Andamento", callback_data='status_Em Andamento')],
        [InlineKeyboardButton("Concluído", callback_data='status_Concluído')],
        [InlineKeyboardButton("Cancelado", callback_data='status_Cancelado')],
    ]
    
    await query.edit_message_text(
        "<b>Passo 3/4: Status</b>\n\nQual é o status inicial desta Ordem de Serviço?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PROMPT_STATUS

async def receive_os_status_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o status da OS e salva no Firebase (criação)."""
    query = update.callback_query
    await query.answer()

    status = query.data.split('_')[1]
    context.user_data['status'] = status

    db = get_db()
    
    os_id = str(uuid.uuid4())[:8].upper()
    current_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    new_os = {
        'id': os_id,
        'user_id': str(update.effective_user.id),
        'descricao': context.user_data['descricao'],
        'tipo': context.user_data['tipo'],
        'status': context.user_data['status'],
        'data_criacao': current_time,
        'data_atualizacao': current_time,
        'alertas': [], 
    }

    try:
        # Usa set para criar/substituir o documento com o ID personalizado
        await db.collection('ordens_servico').document(os_id).set(new_os)
        
        await query.edit_message_text(
            f"✅ <b>OS {os_id} Criada com Sucesso!</b>\n\n"
            f"<b>Descrição:</b> {new_os['descricao']}\n"
            f"<b>Tipo:</b> {new_os['tipo']}\n"
            f"<b>Status:</b> {new_os['status']}\n"
            f"<i>Criação: {new_os['data_criacao']}</i>\n\n"
            "O que deseja fazer agora?",
            parse_mode=ParseMode.HTML,
            reply_markup=get_menu_keyboard()
        )
        return MENU
    except Exception as e:
        logger.error(f"Erro ao salvar OS no Firestore: {e}")
        await query.edit_message_text(f"❌ Erro ao criar OS. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU

async def prompt_os_to_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS para atualização ou exclusão e verifica se existe."""
    os_id = update.message.text.strip().upper()
    user_id = str(update.effective_user.id)
    
    doc_ref = get_os_collection().document(os_id)
    doc = await doc_ref.get()

    if not doc.exists:
        await update.message.reply_text(
            f"❌ OS com ID <b>{os_id}</b> não encontrada. Por favor, digite um ID válido ou /cancel.",
            parse_mode=ParseMode.HTML
        )
        return PROMPT_OS

    os_data = doc.to_dict()
    if os_data.get('user_id') != user_id:
        await update.message.reply_text("❌ Você não tem permissão para esta OS.", reply_markup=get_menu_keyboard())
        return MENU
    
    context.user_data['target_os_id'] = os_id
    
    # Se a ação for 'update', mostra o menu de atualização
    if context.user_data.get('action') == 'update':
        keyboard = [
            [InlineKeyboardButton("Mudar Descrição", callback_data='update_descricao')],
            [InlineKeyboardButton("Mudar Tipo", callback_data='update_tipo')],
            [InlineKeyboardButton("Mudar Status", callback_data='update_status')],
            [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
        ]
        
        await update.message.reply_html(
            f"<b>OS {os_id} - Atualizar:</b>\n\n"
            f"<b>Descrição Atual:</b> {os_data.get('descricao')}\n"
            f"<b>Status Atual:</b> {os_data.get('status')}\n\n"
            "O que deseja alterar?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PROMPT_ATUALIZACAO

    # Se a ação for 'delete', pede confirmação
    elif context.user_data.get('action') == 'delete':
        keyboard = [
            [InlineKeyboardButton("⚠️ CONFIRMAR EXCLUSÃO", callback_data=f'confirm_delete_{os_id}')],
            [InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')],
        ]
        await update.message.reply_html(
            f"<b>Confirmação de Exclusão:</b>\n\n"
            f"Você tem certeza que deseja EXCLUIR permanentemente a OS <b>{os_id}</b>?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PROMPT_OS

    return MENU

async def update_os_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manipula a seleção do campo a ser atualizado."""
    query = update.callback_query
    await query.answer()
    data = query.data
    os_id = context.user_data['target_os_id']

    if data == 'update_descricao':
        await query.edit_message_text(
            f"Digite a <b>nova descrição</b> para a OS {os_id}.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]])
        )
        context.user_data['field_to_update'] = 'descricao'
        return PROMPT_DESCRICAO 
    
    elif data == 'update_tipo':
        keyboard = [
            [InlineKeyboardButton("Manutenção", callback_data='update_field_tipo_Manutenção')],
            [InlineKeyboardButton("Instalação", callback_data='update_field_tipo_Instalação')],
            [InlineKeyboardButton("Orçamento", callback_data='update_field_tipo_Orçamento')],
            [InlineKeyboardButton("Outro", callback_data='update_field_tipo_Outro')],
        ]
        await query.edit_message_text(
            f"Selecione o <b>novo tipo</b> para a OS {os_id}.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['field_to_update'] = 'tipo'
        return PROMPT_ATUALIZACAO
    
    elif data == 'update_status':
        keyboard = [
            [InlineKeyboardButton("Pendente", callback_data='update_field_status_Pendente')],
            [InlineKeyboardButton("Em Andamento", callback_data='update_field_status_Em Andamento')],
            [InlineKeyboardButton("Concluído", callback_data='update_field_status_Concluído')],
            [InlineKeyboardButton("Cancelado", callback_data='update_field_status_Cancelado')],
        ]
        await query.edit_message_text(
            f"Selecione o <b>novo status</b> para a OS {os_id}.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['field_to_update'] = 'status'
        return PROMPT_ATUALIZACAO
    
    return MENU

async def receive_new_value_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (via mensagem de texto) e salva no Firestore."""
    field_to_update = context.user_data.get('field_to_update')
    os_id = context.user_data['target_os_id']
    new_value = update.message.text.strip()
    
    if not field_to_update or not os_id:
        await update.message.reply_text("❌ Erro interno. Voltando ao menu.", reply_markup=get_menu_keyboard())
        return MENU

    try:
        update_data = {
            field_to_update: new_value,
            'data_atualizacao': datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }
        await get_os_collection().document(os_id).update(update_data)

        await update.message.reply_html(
            f"✅ OS <b>{os_id}</b> atualizada com sucesso!\n"
            f"Campo <b>{field_to_update.capitalize()}</b> alterado para: <i>{new_value}</i>",
            reply_markup=get_menu_keyboard()
        )
        return MENU
    except Exception as e:
        logger.error(f"Erro ao atualizar OS {os_id}: {e}")
        await update.message.reply_text(f"❌ Erro ao atualizar OS. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU

async def update_os_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (via callback/botão) e salva no Firestore."""
    query = update.callback_query
    await query.answer()
    
    field_to_update = context.user_data.get('field_to_update')
    os_id = context.user_data['target_os_id']
    new_value = query.data.split('_')[-1] 

    if not field_to_update or not os_id:
        await query.edit_message_text("❌ Erro interno. Voltando ao menu.", reply_markup=get_menu_keyboard())
        return MENU

    try:
        update_data = {
            field_to_update: new_value,
            'data_atualizacao': datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }
        await get_os_collection().document(os_id).update(update_data)

        await query.edit_message_text(
            f"✅ OS <b>{os_id}</b> atualizada com sucesso!\n"
            f"Campo <b>{field_to_update.capitalize()}</b> alterado para: <i>{new_value}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_menu_keyboard()
        )
        return MENU
    except Exception as e:
        logger.error(f"Erro ao atualizar OS {os_id} via callback: {e}")
        await query.edit_message_text(f"❌ Erro ao atualizar OS. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU

async def delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str) -> int:
    """Executa a exclusão da OS."""
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    
    try:
        doc_ref = get_os_collection().document(os_id)
        doc = await doc_ref.get()
        
        if not doc.exists or doc.to_dict().get('user_id') != user_id:
            await query.edit_message_text("❌ Permissão negada ou OS não existe.", reply_markup=get_menu_keyboard())
            return MENU
        
        # Exclui a OS
        await doc_ref.delete()
        
        # Excluir lembretes associados (que têm o os_id)
        lembretes_q = get_lembrete_collection().where(filter=FieldFilter("os_id", "==", os_id)).stream()
        async for lembrete in lembretes_q:
            await lembrete.reference.delete()
        
        await query.edit_message_text(
            f"🗑️ OS <b>{os_id}</b> e seus alertas associados foram <b>excluídos</b> com sucesso.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_menu_keyboard()
        )
        return MENU
    except Exception as e:
        logger.error(f"Erro ao excluir OS {os_id}: {e}")
        await query.edit_message_text(f"❌ Erro ao excluir OS. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU


# --- Handlers de Visualização e Exportação ---

async def view_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca e exibe a lista de OS do usuário."""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    os_list = []
    
    try:
        # Filtra as OS pelo user_id
        docs = get_os_collection().where(filter=FieldFilter("user_id", "==", user_id)).stream()
        async for doc in docs:
            os_list.append(doc.to_dict())
            
        if not os_list:
            message = "⚠️ Você ainda não tem Ordens de Serviço cadastradas."
            reply_markup = get_menu_keyboard()
        else:
            message = "<b>📋 Suas Ordens de Serviço:</b>\n\n"
            for os_data in os_list:
                alert_count = len(os_data.get('alertas', []))
                message += (
                    f"🔗 <b>ID:</b> <code>{os_data['id']}</code>\n"
                    f"📝 <b>Descrição:</b> {os_data['descricao'][:40]}...\n"
                    f"🚦 <b>Status:</b> <b>{os_data['status']}</b>\n"
                    f"🔔 <b>Alertas:</b> {alert_count}\n"
                    "----------------------------------\n"
                )
            
            # Adiciona botões de detalhe/gestão para as ações do menu
            keyboard = [
                [InlineKeyboardButton("✏️ Atualizar OS (iniciar fluxo)", callback_data='atualizar_os_menu')], 
                [InlineKeyboardButton("🗑️ Excluir OS (iniciar fluxo)", callback_data='excluir_os_menu')],
                [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        return MENU

    except Exception as e:
        logger.error(f"Erro ao buscar OS: {e}")
        await query.edit_message_text(f"❌ Erro ao buscar OS. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU

async def exportar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exporta a lista de OS do usuário para PDF e envia."""
    query = update.callback_query
    await query.answer("Gerando relatório...")
    
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    await query.edit_message_text("⚙️ Gerando relatório, aguarde um momento...")
    
    os_list = []
    try:
        docs = get_os_collection().where(filter=FieldFilter("user_id", "==", user_id)).stream()
        async for doc in docs:
            os_list.append(doc.to_dict())
            
        if not os_list:
            await query.edit_message_text("⚠️ Você ainda não tem Ordens de Serviço para exportar.", reply_markup=get_menu_keyboard())
            return MENU
            
        pdf_buffer = await generate_pdf_report(os_list, chat_id)
        
        if pdf_buffer:
            pdf_file = InputFile(pdf_buffer, filename=f"Relatorio_OS_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf")
            await context.bot.send_document(
                chat_id=chat_id,
                document=pdf_file,
                caption="✅ Aqui está o seu relatório de Ordens de Serviço em PDF."
            )
        else:
             # Se pdf_buffer for None, a generate_pdf_report já deve ter enviado um erro ao chat_id
             # ou a aplicação está sem as dependências de PDF.
             logger.error("Falha na geração do PDF - Verifique logs de PyMuPDF/Pandas.")

        # Volta ao menu principal após a exportação
        await context.bot.send_message(chat_id=chat_id, text="Retornando ao Menu Principal...", reply_markup=get_menu_keyboard())
        return MENU
        
    except Exception as e:
        logger.error(f"Erro na função exportar_pdf: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Erro inesperado ao exportar para PDF: {e}", reply_markup=get_menu_keyboard())
        return MENU

# --- Handlers de Lembretes/Alertas (Manuais e de OS) ---

async def prompt_lembrete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu para gerenciar lembretes manuais."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🗓️ Agendar Novo Lembrete Manual", callback_data='lembrete_manual_start')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
    ]
    
    await query.edit_message_text(
        "<b>Menu de Lembretes:</b>\n\n"
        "Selecione uma opção para gerenciar lembretes.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LEMBRETE_MENU

async def prompt_id_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede um título/ID para o lembrete manual."""
    query = update.callback_query
    await query.answer()
    
    context.user_data.clear() 
    context.user_data['lembrete_type'] = 'manual'
    
    await query.edit_message_text(
        "<b>Passo 1/3: ID/Título</b>\n\n"
        "Digite um <b>título ou ID</b> para este lembrete (ex: 'Reunião semanal' ou 'Follow-up cliente A').",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')]])
    )
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o título e pede a data/hora do lembrete."""
    context.user_data['lembrete_titulo'] = update.message.text.strip()
    
    await update.message.reply_html(
        "<b>Passo 2/3: Data e Hora</b>\n\n"
        "Digite a <b>data e hora</b> para o lembrete no formato <code>DD/MM/AAAA HH:MM</code> "
        "(ex: 25/12/2025 10:30).",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]])
    )
    return PROMPT_LEMBRETE_DATA

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora e valida, então pede a mensagem do lembrete."""
    date_text = update.message.text.strip()
    target_dt = validate_date(date_text)
    
    if not target_dt or target_dt < datetime.now():
        await update.message.reply_html(
            "❌ Data ou formato inválido/passado. Use <code>DD/MM/AAAA HH:MM</code> e garanta que é futura.\n"
            "Tente novamente, ou /cancel.",
            parse_mode=ParseMode.HTML
        )
        return PROMPT_LEMBRETE_DATA

    context.user_data['lembrete_dt'] = target_dt
    
    await update.message.reply_html(
        "<b>Passo 3/3: Mensagem</b>\n\n"
        "Digite a <b>mensagem</b> que você quer receber no lembrete.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]])
    )
    return PROMPT_LEMBRETE_MSG

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a mensagem final e salva o lembrete no Firestore."""
    lembrete_msg = update.message.text.strip()
    target_dt = context.user_data['lembrete_dt']
    lembrete_titulo = context.user_data['lembrete_titulo']
    
    lembrete_id = str(uuid.uuid4())[:12] 
    
    new_lembrete = {
        'id': lembrete_id,
        'user_id': str(update.effective_user.id),
        'chat_id': update.effective_chat.id,
        'titulo': lembrete_titulo,
        'mensagem': lembrete_msg,
        'data_lembrete': target_dt.isoformat(), 
        'data_criacao': datetime.now().isoformat(),
        'os_id': None, 
        'status': 'agendado'
    }

    try:
        await get_lembrete_collection().document(lembrete_id).set(new_lembrete)
        
        await update.message.reply_html(
            f"✅ <b>Lembrete Agendado!</b>\n\n"
            f"<b>Título:</b> {lembrete_titulo}\n"
            f"<b>Data:</b> {format_date_br(target_dt)}\n"
            f"Você será notificado(a). Voltando ao menu.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_menu_keyboard()
        )
        return MENU
    except Exception as e:
        logger.error(f"Erro ao salvar lembrete: {e}")
        await update.message.reply_text(f"❌ Erro ao agendar lembrete. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU


# --- Handlers de Alertas Específicos da OS ---

async def gerir_alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str) -> int:
    """Exibe o menu de gestão de alertas para uma OS específica."""
    query = update.callback_query
    await query.answer()
    
    os_doc = await get_os_collection().document(os_id).get()
    if not os_doc.exists:
        await query.edit_message_text("❌ OS não encontrada.", reply_markup=get_menu_keyboard())
        return MENU
        
    os_data = os_doc.to_dict()
    alertas = os_data.get('alertas', [])
    context.user_data['target_os_id'] = os_id
    
    message = f"<b>Gestão de Alertas para OS {os_id}</b>\n\n"
    if alertas:
        message += "<b>Alertas Atuais:</b>\n"
        for i, alerta in enumerate(alertas):
            # Formata a data (remove 'T' e pega a parte da data)
            data_formatada = datetime.fromisoformat(alerta['data_lembrete']).strftime('%d/%m/%Y %H:%M')
            message += f" - <code>{alerta['id'][:4]}</code>: {alerta['descricao']} ({data_formatada})\n"
    else:
        message += "⚠️ Nenhuma alerta cadastrado para esta OS."

    keyboard = [
        [InlineKeyboardButton("➕ Incluir Novo Alerta", callback_data=f'incluir_alerta_{os_id}')],
        [InlineKeyboardButton("🗑️ Remover Alerta", callback_data=f'remover_alerta_menu_{os_id}')] if alertas else [],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
    ]
    
    await query.edit_message_text(
        message,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PROMPT_ALERTA 

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição do alerta da OS e pede o prazo."""
    text = update.message.text
    context.user_data['alerta_descricao'] = text
    os_id = context.user_data['target_os_id']
    
    await update.message.reply_html(
        f"<b>Alerta para OS {os_id} - Prazo</b>\n\n"
        f"Digite a <b>data e hora</b> para o alerta no formato <code>DD/MM/AAAA HH:MM</code> "
        "(ex: 25/12/2025 10:30).",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{os_id}')]])
    )
    return PROMPT_ID_ALERTA 

async def receive_alerta_prazo_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o prazo, valida e salva o alerta na lista da OS e no collection de lembretes."""
    date_text = update.message.text.strip()
    target_dt = validate_date(date_text)
    os_id = context.user_data['target_os_id']
    
    if not target_dt or target_dt < datetime.now():
        await update.message.reply_html(
            "❌ Data ou formato inválido/passado. Use <code>DD/MM/AAAA HH:MM</code>.\n"
            "Tente novamente, ou /cancel.",
            parse_mode=ParseMode.HTML
        )
        return PROMPT_ID_ALERTA
        
    alerta_id = str(uuid.uuid4())[:8]
    
    new_alerta = {
        'id': alerta_id,
        'descricao': context.user_data['alerta_descricao'],
        'data_lembrete': target_dt.isoformat(), # Salva em formato ISO
        'data_criacao': datetime.now().isoformat(),
        'status': 'agendado'
    }

    try:
        # 1. Salva o alerta na lista 'alertas' do documento da OS
        os_ref = get_os_collection().document(os_id)
        await os_ref.update({'alertas': firestore.ArrayUnion([new_alerta])})
        
        # 2. Salva também como um lembrete no collection 'lembretes' (para o cron job)
        lembrete_data = {
            'id': alerta_id,
            'user_id': str(update.effective_user.id),
            'chat_id': update.effective_chat.id,
            'titulo': f"ALERTA OS {os_id}",
            'mensagem': f"ALERTA para OS {os_id}: {new_alerta['descricao']}",
            'data_lembrete': new_alerta['data_lembrete'],
            'data_criacao': new_alerta['data_criacao'],
            'os_id': os_id, # Associa à OS
            'status': 'agendado'
        }
        await get_lembrete_collection().document(alerta_id).set(lembrete_data)

        await update.message.reply_html(
            f"✅ Alerta <b>{alerta_id[:4]}</b> para OS <b>{os_id}</b> agendado para {format_date_br(target_dt)}.\n"
            "Voltando ao menu de gestão de alertas...",
            parse_mode=ParseMode.HTML
        )
        
        # Simula o retorno ao menu de gestão de alerta
        # Este é um padrão para retornar de um MessageHandler a um CallbackQueryHandler
        class MockCallbackQuery:
            def __init__(self, data): self.data = data
            async def answer(self): pass
            
        class MockUpdate:
            def __init__(self, chat, user, data):
                self.effective_chat = chat
                self.effective_user = user
                self.callback_query = MockCallbackQuery(data)
                self.message = None 
                
        mock_update = MockUpdate(update.effective_chat, update.effective_user, f'gerir_alerta_{os_id}')
        
        return await gerir_alerta_menu(mock_update, context, os_id)
        
    except Exception as e:
        logger.error(f"Erro ao salvar alerta na OS {os_id}: {e}")
        await update.message.reply_text(f"❌ Erro ao salvar alerta. Tente novamente.\nErro: {e}", reply_markup=get_menu_keyboard())
        return MENU
        
async def remover_alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str) -> int:
    """Exibe a lista de alertas para remoção."""
    query = update.callback_query
    await query.answer()
    
    os_doc = await get_os_collection().document(os_id).get()
    if not os_doc.exists:
        await query.edit_message_text("❌ OS não encontrada.", reply_markup=get_menu_keyboard())
        return MENU
        
    os_data = os_doc.to_dict()
    alertas = os_data.get('alertas', [])
    
    if not alertas:
        await query.edit_message_text("⚠️ Nenhuma alerta para remover.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{os_id}')]]))
        return PROMPT_ALERTA

    message = f"<b>Remover Alerta para OS {os_id}</b>\n\nSelecione o alerta para remover:"
    keyboard = []
    for alerta in alertas:
        data_formatada = datetime.fromisoformat(alerta['data_lembrete']).strftime('%d/%m/%Y %H:%M')
        btn_text = f"🗑️ {alerta['descricao']} ({data_formatada})"
        callback_data = f'remover_alerta_{os_id}_{alerta["id"]}'
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        
    keyboard.append([InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{os_id}')])

    await query.edit_message_text(
        message,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PROMPT_ALERTA

async def remover_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str, alerta_id: str) -> int:
    """Remove o alerta da OS e do collection de lembretes."""
    query = update.callback_query
    await query.answer()
    
    try:
        os_ref = get_os_collection().document(os_id)
        os_doc = await os_ref.get()
        
        if not os_doc.exists:
            await query.edit_message_text("❌ OS não encontrada.", reply_markup=get_menu_keyboard())
            return MENU
            
        os_data = os_doc.to_dict()
        alertas = os_data.get('alertas', [])
        
        # Encontra o alerta a remover
        alerta_to_remove = next((a for a in alertas if a['id'] == alerta_id), None)
        
        if alerta_to_remove:
            # 1. Remove da lista 'alertas' da OS
            await os_ref.update({'alertas': firestore.ArrayRemove([alerta_to_remove])})
            
            # 2. Remove do collection de lembretes
            await get_lembrete_collection().document(alerta_id).delete()
            
            await query.edit_message_text(
                f"✅ Alerta <b>{alerta_id[:4]}</b> removido da OS <b>{os_id}</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{os_id}')]])
            )
        else:
            await query.edit_message_text("⚠️ Alerta não encontrado.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data=f'gerir_alerta_{os_id}')]]))
            
        return PROMPT_ALERTA
        
    except Exception as e:
        logger.error(f"Erro ao remover alerta {alerta_id} da OS {os_id}: {e}")
        await query.edit_message_text(f"❌ Erro ao remover alerta: {e}", reply_markup=get_menu_keyboard())
        return MENU


# --- Job Queue e Envio de Lembretes ---

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Função para enviar um lembrete agendado (chamada pelo JobQueue)."""
    job = context.job
    lembrete_data = job.data 
    
    chat_id = lembrete_data.get('chat_id')
    titulo = lembrete_data.get('titulo', 'Lembrete')
    mensagem = lembrete_data.get('mensagem', 'Você tem um lembrete.')
    os_id = lembrete_data.get('os_id')

    if not chat_id:
        logger.error(f"Lembrete {lembrete_data.get('id')} sem chat_id. Ignorando.")
        return

    try:
        # Envia a mensagem de lembrete
        text = f"🔔 <b>{titulo}</b>\n\n{mensagem}"
        
        if os_id:
            text += f"\n\n🔗 <b>OS Relacionada:</b> <code>{os_id}</code>"
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML
        )

        # Atualiza o status no Firestore para 'enviado'
        lembrete_ref = get_lembrete_collection().document(lembrete_data['id'])
        await lembrete_ref.update({'status': 'enviado'})

        logger.info(f"Lembrete {lembrete_data['id']} enviado para chat {chat_id}.")

    except Exception as e:
        logger.error(f"Erro ao enviar lembrete {lembrete_data.get('id')} para chat {chat_id}: {e}")

async def schedule_recurring_reminders(application: Application):
    """
    Busca lembretes 'agendado' no Firestore e os agenda no JobQueue para envio.
    """
    db = get_db()
    if not db:
        logger.error("Firestore não inicializado. Não é possível agendar lembretes.")
        return

    now = datetime.now()
    
    # Busca lembretes que estão 'agendado'
    q = get_lembrete_collection().where(filter=FieldFilter("status", "==", "agendado")).stream()
    
    # Nomes dos jobs já agendados para evitar duplicidade
    existing_jobs = [job.name for job in application.job_queue.jobs()]
    
    scheduled_count = 0
    
    async for doc in q:
        lembrete = doc.to_dict()
        lembrete_id = lembrete.get('id')
        job_name = f"lembrete_{lembrete_id}"
        
        if job_name in existing_jobs:
            continue # Já está agendado

        try:
            # Converte a data ISO para datetime
            lembrete_dt = datetime.fromisoformat(lembrete['data_lembrete'])
            
            if lembrete_dt > now:
                # Calcula o tempo de espera (em segundos)
                wait_time = (lembrete_dt - now).total_seconds()
                
                # Adiciona o job à fila
                application.job_queue.run_once(
                    send_reminder, 
                    wait_time, 
                    data=lembrete, 
                    name=job_name
                )
                scheduled_count += 1
                logger.info(f"Lembrete {lembrete_id} agendado para {format_date_br(lembrete_dt)}.")

            elif lembrete_dt < now - timedelta(minutes=5):
                # Atrasado demais, trata como falha ou envia imediatamente
                logger.warning(f"Lembrete {lembrete_id} está muito atrasado e não foi agendado. Marcando como enviado.")
                lembrete_ref = get_lembrete_collection().document(lembrete_id)
                await lembrete_ref.update({'status': 'enviado'})


        except Exception as e:
            logger.error(f"Erro ao processar/agendar lembrete {lembrete_id}: {e}")

    logger.info(f"Verificação de lembretes concluída. {scheduled_count} agendados.")


# --- Fallbacks e Cancelamento ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela qualquer conversa em andamento e volta ao menu."""
    if update.message:
        await update.message.reply_text(
            "Ação cancelada. Voltando ao Menu Principal.",
            reply_markup=get_menu_keyboard()
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            "Ação cancelada. Voltando ao Menu Principal.",
            reply_markup=get_menu_keyboard()
        )
    return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lida com comandos não reconhecidos."""
    await update.message.reply_text("Comando não reconhecido. Use o /start para o menu principal.")


# --- Função Principal ---

def main() -> None:
    """Inicia o bot."""
    if not TOKEN:
        logger.error("O token do Telegram não foi encontrado. Abortando.")
        return

    # 1. Inicializa o Firebase e Firestore
    firebase_init()
    if get_db() is None:
        logger.error("Falha ao inicializar o Firebase. O bot não pode funcionar sem o DB.")
        return

    # 2. Constrói o Application
    application = ApplicationBuilder.builder().token(TOKEN).concurrent_updates(True).build()
    
    # 3. Configura o JobQueue para lembretes recorrentes
    job_queue: JobQueue = application.job_queue
    # Agenda a verificação de lembretes para rodar a cada 30 minutos
    job_queue.run_repeating(
        lambda context: asyncio.create_task(schedule_recurring_reminders(application)),
        interval=timedelta(minutes=30), 
        first=0, # Roda na inicialização
        name="recurring_reminder_scheduler"
    )
    
    # Adiciona o job de keep-alive (se em modo webhook)
    if WEBHOOK_URL:
        job_queue.run_repeating(
            lambda context: asyncio.create_task(keep_alive()),
            interval=timedelta(minutes=15),
            name="keep_alive"
        )

    # Definição dos estados do ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^(criar_os_start|ver_os|atualizar_os_menu|excluir_os_menu|lembrete_menu|exportar_pdf_confirm)$'),
            ],
            
            # Fluxo de Criação de OS
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_descricao),
                CallbackQueryHandler(callback_handler, pattern='^menu$'), 
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(receive_os_tipo, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_STATUS: [
                CallbackQueryHandler(receive_os_status_and_save, pattern='^status_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],

            # Fluxo de Atualização/Exclusão (após pedir o ID)
            PROMPT_OS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_os_to_update),
                CallbackQueryHandler(callback_handler, pattern='^confirm_delete_'), 
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_ATUALIZACAO: [
                CallbackQueryHandler(update_os_callback, pattern='^update_(descricao|tipo|status)$'),
                CallbackQueryHandler(update_os_from_callback, pattern='^update_field_'), 
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
                # A nova descrição é tratada por receive_os_descricao se field_to_update estiver setado
            ],

            # Fluxo de Alertas (dentro da OS)
            PROMPT_ALERTA: [
                CallbackQueryHandler(callback_handler, pattern='^(incluir_alerta_|remover_alerta_menu_|remover_alerta_|gerir_alerta_|menu)$'),
            ],
            PROMPT_INCLUSAO: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao),
                CallbackQueryHandler(callback_handler, pattern='^gerir_alerta_'),
            ],
            PROMPT_ID_ALERTA: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_prazo_or_id),
                CallbackQueryHandler(callback_handler, pattern='^gerir_alerta_'),
            ],
            
            # Fluxo de Lembrete Manual
            LEMBRETE_MENU: [
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_ID_LEMBRETE: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_DATA: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_MSG: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start), 
            MessageHandler(filters.COMMAND, fallback_command),
            CallbackQueryHandler(callback_handler, pattern='^menu$'), 
        ],
    )

    # Adiciona o ConversationHandler
    application.add_handler(conv_handler)
    
    # 4. Configuração do Webhook ou Long Polling
    if WEBHOOK_URL and TOKEN:
        try:
            logger.info(f"A iniciar Webhook em http://0.0.0.0:{PORT}{WEBHOOK_PATH}")
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN, 
                webhook_url=WEBHOOK_URL + WEBHOOK_PATH, 
            )
            logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
        except Exception as e:
            logger.error(f"Falha ao iniciar o Webhook: {e}")
            logger.info("Tentando modo Long Polling.")
            application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        logger.info("WEBHOOK_URL ou TOKEN não configurado. Usando modo Long Polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
