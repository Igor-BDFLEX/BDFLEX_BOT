# bot_webhook.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK MODE)

# --- Imports e Setup ---

import logging
import json
import time
import os
import re # Para manipulação de texto
import uuid # Para IDs únicos
from datetime import datetime, timedelta
import asyncio # Adicionado para tarefas assíncronas
import aiohttp # Adicionado para requisições HTTP (Manter o bot ativo)
import io # Para manipulação de arquivos em memória

# Imports para PDF (necessitam de instalação via pip: PyMuPDF e pandas)
try:
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    
# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app

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
    CallbackContext,
)
from telegram.constants import ParseMode

# Python-dotenv
from dotenv import load_dotenv

# --- Configuração ---

load_dotenv()

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configurações do Webhook (ajustar conforme seu ambiente)
TOKEN = os.getenv("TELEGRAM_TOKEN")
# NOTE: Em ambientes como o Google Cloud Run, a porta é definida por variáveis de ambiente.
PORT = int(os.environ.get("PORT", "8443"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") # Ex: https://seu-app.com
WEBHOOK_PATH = '/' + TOKEN # Deve ser o mesmo que o URL_PATH

# Estados para o ConversationHandler (EXPANDIDOS)
(
    MENU, PROMPT_OS_NUMERO, PROMPT_OS_PREFIXO, PROMPT_OS_CHAMADO, PROMPT_OS_DISTANCIA,
    PROMPT_OS_DESCRICAO, PROMPT_OS_CRITICIDADE, PROMPT_OS_TIPO, PROMPT_OS_PRAZO, PROMPT_OS_SITUACAO,
    PROMPT_OS_TECNICO, PROMPT_OS_NOME_TECNICO, PROMPT_OS_RESUMO_INCLUSAO, PROMPT_DELECAO_OS, 
    PROMPT_DELECAO_CONFIRMACAO, MENU_LISTAGEM_TIPO, MENU_LISTAGEM_SITUACAO, PROMPT_ATUALIZACAO_OS,
    PROMPT_ATUALIZACAO_CAMPO, PROMPT_ATUALIZACAO_VALOR, RECEIVE_PDF, LEMBRETE_MENU, 
    PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG, AJUDA_GERAL
) = range(27)

# URL da imagem (use um URL público ou o file_id da imagem enviada para o Telegram)
MENU_IMAGE_URL = "https://i.imgur.com/kS5x87J.png" # Placeholder - Substitua pela sua imagem
# FILE_ID da sua imagem (para não precisar fazer upload toda vez)
# MENU_IMAGE_FILE_ID = "BAACAgIAAxk..." 

# --- Firebase Init ---

try:
    # A variável de ambiente FIREBASE_CREDENTIALS deve conter o JSON das credenciais
    if os.getenv("FIREBASE_CREDENTIALS"):
        cred_json = json.loads(os.getenv("FIREBASE_CREDENTIALS"))
        cred = credentials.Certificate(cred_json)
        initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase inicializado com sucesso.")
    else:
        logger.error("FIREBASE_CREDENTIALS não encontrada. O bot não salvará dados.")
        db = None
except Exception as e:
    logger.error(f"Erro ao inicializar Firebase: {e}")
    db = None

# --- Funções Auxiliares e de Dados ---

def get_os_ref(os_id):
    """Retorna a referência do documento de uma OS."""
    if not db: return None
    return db.collection("ordens_servico").document(str(os_id))

def get_all_os():
    """Retorna todas as ordens de serviço."""
    if not db: return []
    return db.collection("ordens_servico").stream()

async def fetch_os_by_num(os_num):
    """Busca uma OS pelo seu número."""
    if not db: return None
    os_num_int = int(os_num) # OS é armazenada como número
    query = db.collection("ordens_servico").where("Numero_da_OS", "==", os_num_int).limit(1)
    results = query.stream()
    
    # Retorna o primeiro resultado e o ID do documento
    for doc in results:
        data = doc.to_dict()
        data['doc_id'] = doc.id
        return data
    return None

def format_os_summary(data):
    """Formata os dados da OS para o resumo."""
    summary = (
        "📋 *RESUMO DA O.S.*\n"
        f"Número: `{data.get('Numero_da_OS', 'N/A')}`\n"
        f"Chamado: `{data.get('Chamado', 'N/A')}`\n"
        f"Prefixo/Dependência: `{data.get('Prefixo_Dependencia', 'N/A')}`\n"
        f"Distância: `{data.get('Distancia', 'N/A')}`\n"
        f"Descrição: _{data.get('Descricao', 'N/A')}_\n"
        f"Criticidade: *{data.get('Criticidade', 'N/A')}*\n"
        f"Tipo: `{data.get('Tipo', 'N/A')}`\n"
        f"Prazo: `{data.get('Prazo', 'N/A')}`\n"
        f"Situação: `{data.get('Situacao', 'N/A')}`\n"
        f"Técnico: `{data.get('Tecnico', 'NÃO DEFINIDO')}`\n"
        f"Agendamento: `{data.get('Agendamento', 'N/A')}`\n"
        f"Lembrete: `{data.get('Lembrete', 'Nenhum')}`"
    )
    return summary

def get_edit_keyboard(current_os):
    """Gera o teclado para edição de campos."""
    buttons = [
        [InlineKeyboardButton(f"1. Número: {current_os.get('Numero_da_OS', 'N/A')}", callback_data='edit_Numero_da_OS')],
        [InlineKeyboardButton(f"2. Chamado: {current_os.get('Chamado', 'N/A')}", callback_data='edit_Chamado')],
        [InlineKeyboardButton(f"3. Prefixo/Dependência: {current_os.get('Prefixo_Dependencia', 'N/A')}", callback_data='edit_Prefixo_Dependencia')],
        [InlineKeyboardButton(f"4. Distância: {current_os.get('Distancia', 'N/A')}", callback_data='edit_Distancia')],
        [InlineKeyboardButton(f"5. Descrição: {current_os.get('Descricao', 'N/A')}", callback_data='edit_Descricao')],
        [InlineKeyboardButton(f"6. Criticidade: {current_os.get('Criticidade', 'N/A')}", callback_data='edit_Criticidade')],
        [InlineKeyboardButton(f"7. Tipo: {current_os.get('Tipo', 'N/A')}", callback_data='edit_Tipo')],
        [InlineKeyboardButton(f"8. Prazo: {current_os.get('Prazo', 'N/A')}", callback_data='edit_Prazo')],
        [InlineKeyboardButton(f"9. Situação: {current_os.get('Situacao', 'N/A')}", callback_data='edit_Situacao')],
        [InlineKeyboardButton(f"10. Técnico: {current_os.get('Tecnico', 'NÃO DEFINIDO')}", callback_data='edit_Tecnico')],
        [InlineKeyboardButton(f"11. Agendamento: {current_os.get('Agendamento', 'N/A')}", callback_data='edit_Agendamento')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')]
    ]
    return InlineKeyboardMarkup(buttons)

async def check_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Verifica se o usuário tem permissão (exemplo simples)."""
    # Exemplo: Apenas um ID de usuário específico ou grupo
    if not db:
        await update.effective_message.reply_text("❌ Serviço de banco de dados indisponível.")
        return False
    return True # Permitindo acesso para demonstração

# --- Lógica de Vencimento Automático (Job Queue) ---

def format_vencimento_message(os_data, dias_restantes):
    """Formata a mensagem de alerta de vencimento."""
    
    if dias_restantes < 0:
        alerta = f"🔴 *VENCIDA HÁ {abs(dias_restantes)} DIAS!*"
    elif dias_restantes == 0:
        alerta = f"🔥 *VENCE HOJE!*"
    elif dias_restantes == 1:
        alerta = f"⚠️ *VENCE AMANHÃ!*"
    elif dias_restantes == 2:
        alerta = f"⏳ *Vence em 2 dias!*"
    else:
        return None # Não deve acontecer com o filtro

    return (
        f"🔔 *ALERTA DE VENCIMENTO* 🔔\n"
        f"{alerta}\n\n"
        f"📋 *O.S.*: `{os_data.get('Numero_da_OS', 'N/A')}`\n"
        f"📍 *Prefixo/Dependência*: `{os_data.get('Prefixo_Dependencia', 'N/A')}`\n"
        f"📝 *Descrição*: _{os_data.get('Descricao', 'N/A')}_\n"
        f"📅 *Prazo*: `{os_data.get('Prazo', 'N/A')}`\n"
        f"🛠️ *Situação*: `{os_data.get('Situacao', 'N/A')}`\n"
        f"👨‍🔧 *Técnico*: `{os_data.get('Tecnico', 'N/A')}`"
    )


async def verificar_vencimentos(context: CallbackContext):
    """Verifica O.S. próximas ao vencimento ou vencidas e notifica o chat."""
    if not db:
        logger.warning("Verificação de vencimentos ignorada: DB indisponível.")
        return

    chat_id = context.job.data # O ID do chat que iniciou o bot
    today = datetime.now().date()
    
    try:
        # Pega todas as OS para verificar
        docs = db.collection("ordens_servico").stream()
        
        for doc in docs:
            os_data = doc.to_dict()
            os_data['doc_id'] = doc.id
            
            # Ignorar se estiver Concluído
            if os_data.get("Situacao", "").lower() == "concluído":
                continue

            prazo_str = os_data.get("Prazo")
            if not prazo_str:
                continue

            try:
                # Tenta analisar a data no formato DD/MM/AAAA
                prazo_date = datetime.strptime(prazo_str, "%d/%m/%Y").date()
            except ValueError:
                # Tenta analisar a data no formato AAAA-MM-DD (se veio de algum outro processo)
                 try:
                    prazo_date = datetime.strptime(prazo_str, "%Y-%m-%d").date()
                 except ValueError:
                    logger.warning(f"Formato de prazo inválido para OS {os_data.get('Numero_da_OS')}: {prazo_str}")
                    continue

            # Calcula a diferença de dias
            delta = prazo_date - today
            days_diff = delta.days

            # Notificar se estiver vencida, vencendo hoje, amanhã ou em 2 dias
            if days_diff <= 2:
                # O limite inferior é arbitrário, mas evitar notificar coisas muito antigas que podem ser lixo
                if days_diff >= -30: 
                    message = format_vencimento_message(os_data, days_diff)
                    if message:
                        await context.bot.send_message(
                            chat_id=chat_id, 
                            text=message, 
                            parse_mode=ParseMode.MARKDOWN
                        )
                        # Opcional: Marcar a OS como notificada para o dia
                        # update_doc(os_data['doc_id'], {'ultima_notificacao': today.isoformat()})
                        
    except Exception as e:
        logger.error(f"Erro na verificação de vencimentos: {e}")

def schedule_vencimento_job(application: Application, chat_id):
    """Agenda a tarefa de verificação de vencimento."""
    job_name = f"vencimento_checker_{chat_id}"
    
    # Verifica se o Job já existe para evitar duplicatas
    if application.job_queue.get_jobs_by_name(job_name):
        return
        
    # Executa a cada 6 horas (pode ser ajustado)
    application.job_queue.run_repeating(
        verificar_vencimentos, 
        interval=timedelta(hours=6), 
        first=timedelta(seconds=10), # Primeira execução rápida
        data=chat_id,
        name=job_name
    )
    logger.info(f"Job de vencimento agendado para o chat {chat_id}.")

# --- Funções do Bot (Handlers) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Envia o menu de início e agenda o checker de vencimento."""
    if not await check_user_access(update, context):
        return ConversationHandler.END

    if update.effective_chat:
        schedule_vencimento_job(context.application, update.effective_chat.id)
        
    await show_menu(update, context)
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa e volta ao menu principal."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.delete()
    
    await show_menu(update, context, message="✅ Fluxo cancelado. Voltando ao Menu Principal.")
    return MENU

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str = "👋 *Olá! Como posso te ajudar hoje?*"):
    """Exibe o menu principal com a imagem."""
    
    if update.callback_query:
        await update.callback_query.answer()

    keyboard = [
        [InlineKeyboardButton("➕ Incluir O.S.", callback_data='incluir_os'),
         InlineKeyboardButton("🔄 Atualizar O.S.", callback_data='atualizar_os')],
        [InlineKeyboardButton("🗑️ Deletar O.S.", callback_data='deletar_os'),
         InlineKeyboardButton("📋 Listar O.S.", callback_data='listar_os')],
        [InlineKeyboardButton("📄 Enviar PDF", callback_data='enviar_pdf'),
         InlineKeyboardButton("⏰ Lembrete", callback_data='lembrete_manual_menu')],
        [InlineKeyboardButton("❓ Ajuda Geral", callback_data='ajuda_geral')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Verifica se é uma edição de mensagem (volta do fluxo)
    if update.callback_query and update.callback_query.message:
        try:
            # Tenta editar a mensagem existente se for uma callback
            await update.callback_query.message.edit_caption(
                caption=message, 
                reply_markup=reply_markup, 
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
             # Se a edição falhar (ex: mensagem muito antiga), envia uma nova
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=MENU_IMAGE_URL,
                caption=message,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    else:
        # Envia uma nova mensagem (início ou /start)
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=MENU_IMAGE_URL,
            caption=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )


# --- FLUXO DE INCLUSÃO (STEP-BY-STEP) ---

async def prompt_os_numero(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da O.S. (Passo 1)."""
    if update.callback_query:
        await update.callback_query.answer()
        
    # Inicializa o dicionário de dados da OS
    context.user_data['os_data'] = {
        'os_id': str(uuid.uuid4()), # ID para o Firestore
        'state_step': 1
    }
    
    await update.effective_message.reply_text(
        "📝 *NOVA O.S. - Passo 1/11*\n\n"
        "Qual é o *Número da O.S.*? (Somente números)",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_NUMERO

async def receive_os_numero(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da O.S. e verifica duplicidade."""
    os_num = update.message.text.strip()

    if not os_num.isdigit():
        await update.message.reply_text("❌ Número da O.S. deve conter apenas números. Por favor, tente novamente.")
        return PROMPT_OS_NUMERO
    
    # 1. Checagem de Duplicidade
    existing_os = await fetch_os_by_num(os_num)

    context.user_data['os_data']['Numero_da_OS'] = int(os_num)
    
    if existing_os:
        # OS já existe: Sugestão para Atualização
        context.user_data['os_to_update'] = existing_os
        
        keyboard = [
            [InlineKeyboardButton("✅ Sim, Atualizar", callback_data='atualizar_existente')],
            [InlineKeyboardButton("❌ Não, Voltar", callback_data='menu')],
            [InlineKeyboardButton("⬅️ Tentar Outro Número", callback_data='incluir_os')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"⚠️ O.S. de número *{os_num}* já foi cadastrada.\n\n"
            "Deseja *atualizar* as informações desta O.S. existente?\n\n"
            f"{format_os_summary(existing_os)}",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        # Permanece no estado para receber a decisão (CallbackQueryHandler para 'atualizar_existente')
        return PROMPT_OS_NUMERO
    
    # Se não existe, avança
    return await prompt_os_prefixo(update, context)

async def prompt_os_prefixo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Prefixo/Dependência (Passo 2)."""
    # Se veio do receive_os_numero, a OS já está no user_data.
    if context.user_data['os_data'].get('state_step') == 1:
        context.user_data['os_data']['state_step'] = 2
        
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 2/11*\n\n"
        f"Informe o *Prefixo/Dependência* (Ex: 1025 - Banco Teste):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_PREFIXO

async def receive_os_prefixo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Prefixo/Dependência e avança."""
    context.user_data['os_data']['Prefixo_Dependencia'] = update.message.text.strip()
    return await prompt_os_chamado(update, context)

async def prompt_os_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Chamado (Passo 3)."""
    context.user_data['os_data']['state_step'] = 3
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 3/11*\n\n"
        f"Informe o *Número do Chamado*:",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_CHAMADO

async def receive_os_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Chamado e avança."""
    context.user_data['os_data']['Chamado'] = update.message.text.strip()
    return await prompt_os_distancia(update, context)

async def prompt_os_distancia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a Distância (Passo 4)."""
    context.user_data['os_data']['state_step'] = 4
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 4/11*\n\n"
        f"Informe a *Distância/Quilometragem* (Ex: 25 Km):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_DISTANCIA

async def receive_os_distancia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Distância e avança."""
    context.user_data['os_data']['Distancia'] = update.message.text.strip()
    return await prompt_os_descricao(update, context)

async def prompt_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a Descrição (Passo 5)."""
    context.user_data['os_data']['state_step'] = 5
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 5/11*\n\n"
        f"Informe a *Descrição do Serviço*:",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_DESCRICAO

async def receive_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Descrição e avança."""
    context.user_data['os_data']['Descricao'] = update.message.text.strip()
    return await prompt_os_criticidade(update, context)

async def prompt_os_criticidade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a Criticidade (Passo 6)."""
    context.user_data['os_data']['state_step'] = 6
    keyboard = [
        [InlineKeyboardButton("🚨 Emergencial", callback_data='crit_Emergencial')],
        [InlineKeyboardButton("⚠️ Urgente", callback_data='crit_Urgente')],
        [InlineKeyboardButton("🟢 Normal", callback_data='crit_Normal')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='cancel_flow')], # Retorna ao menu
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 6/11*\n\n"
        f"Selecione a *Criticidade*:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_CRITICIDADE

async def receive_os_criticidade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Criticidade (Callback) e avança."""
    query = update.callback_query
    await query.answer()
    
    criticidade = query.data.split('_')[1]
    context.user_data['os_data']['Criticidade'] = criticidade
    await query.edit_message_text(
        f"✅ Criticidade selecionada: *{criticidade}*",
        parse_mode=ParseMode.MARKDOWN
    )
    return await prompt_os_tipo(update, context)

async def prompt_os_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Tipo (Passo 7)."""
    context.user_data['os_data']['state_step'] = 7
    keyboard = [
        [InlineKeyboardButton("🔧 Corretiva", callback_data='tipo_Corretiva')],
        [InlineKeyboardButton("🧹 Preventiva", callback_data='tipo_Preventiva')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='cancel_flow')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 7/11*\n\n"
        f"Selecione o *Tipo* de serviço:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_TIPO

async def receive_os_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Tipo (Callback) e avança."""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.split('_')[1]
    context.user_data['os_data']['Tipo'] = tipo
    await query.edit_message_text(
        f"✅ Tipo de serviço selecionado: *{tipo}*",
        parse_mode=ParseMode.MARKDOWN
    )
    return await prompt_os_prazo(update, context)

async def prompt_os_prazo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Prazo (Passo 8)."""
    context.user_data['os_data']['state_step'] = 8
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 8/11*\n\n"
        f"Informe o *Prazo* final (DD/MM/AAAA):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_PRAZO

async def receive_os_prazo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Prazo e avança."""
    prazo_str = update.message.text.strip()
    
    # Validação simples de formato DD/MM/AAAA
    if not re.match(r"^\d{2}/\d{2}/\d{4}$", prazo_str):
        await update.message.reply_text("❌ Formato inválido. Por favor, use o formato DD/MM/AAAA (ex: 25/10/2025).")
        return PROMPT_OS_PRAZO
    
    context.user_data['os_data']['Prazo'] = prazo_str
    return await prompt_os_situacao(update, context)

async def prompt_os_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a Situação (Passo 9)."""
    context.user_data['os_data']['state_step'] = 9
    keyboard = [
        [InlineKeyboardButton("Pendente", callback_data='sit_Pendente')],
        [InlineKeyboardButton("Aguardando agendamento", callback_data='sit_Aguardando_agendamento')],
        [InlineKeyboardButton("Agendado", callback_data='sit_Agendado')],
        [InlineKeyboardButton("Concluído", callback_data='sit_Concluido')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='cancel_flow')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 9/11*\n\n"
        f"Selecione a *Situação* da O.S.:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_SITUACAO

async def receive_os_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Situação (Callback) e avança."""
    query = update.callback_query
    await query.answer()
    
    situacao = query.data.split('_')[1].replace('Aguardando', 'Aguardando ').replace('Concluido', 'Concluído')
    context.user_data['os_data']['Situacao'] = situacao
    await query.edit_message_text(
        f"✅ Situação selecionada: *{situacao}*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Adiciona Agendamento (por enquanto como 'N/A' se não informado)
    if 'Agendamento' not in context.user_data['os_data']:
         context.user_data['os_data']['Agendamento'] = 'N/A'
         
    return await prompt_os_tecnico(update, context)

async def prompt_os_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Técnico (Passo 10)."""
    context.user_data['os_data']['state_step'] = 10
    keyboard = [
        [InlineKeyboardButton("👷 DEFINIDO", callback_data='tec_DEFINIDO')],
        [InlineKeyboardButton("🚫 NÃO DEFINIDO", callback_data='tec_NAO_DEFINIDO')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='cancel_flow')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 10/11*\n\n"
        f"O *Técnico Responsável* está definido?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_TECNICO

async def receive_os_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a definição do Técnico (Callback)."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'tec_DEFINIDO':
        await query.edit_message_text(
            "Qual é o *nome do técnico responsável*?",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_OS_NOME_TECNICO
    elif query.data == 'tec_NAO_DEFINIDO':
        context.user_data['os_data']['Tecnico'] = 'NÃO DEFINIDO'
        await query.edit_message_text("✅ Técnico: *NÃO DEFINIDO*.", parse_mode=ParseMode.MARKDOWN)
        return await prompt_os_agendamento(update, context)

async def receive_os_nome_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o nome do Técnico e avança para Agendamento."""
    context.user_data['os_data']['Tecnico'] = update.message.text.strip()
    return await prompt_os_agendamento(update, context)

async def prompt_os_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a data de Agendamento (Passo 11)."""
    context.user_data['os_data']['state_step'] = 11
    await update.effective_message.reply_text(
        f"📝 *NOVA O.S. - Passo 11/11*\n\n"
        f"Informe a data de *Agendamento* (DD/MM/AAAA) ou digite 'N/A':",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_RESUMO_INCLUSAO # Proxima etapa é o resumo, mas o estado muda para o resumo

async def show_os_resumo_inclusao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o resumo final da OS antes da inclusão/confirmação."""
    agendamento_str = update.message.text.strip()
    
    # Validação simples de data ou N/A
    if not (agendamento_str.upper() == 'N/A' or re.match(r"^\d{2}/\d{2}/\d{4}$", agendamento_str)):
         await update.message.reply_text("❌ Formato inválido. Por favor, use o formato DD/MM/AAAA ou digite 'N/A'.")
         return PROMPT_OS_RESUMO_INCLUSAO
         
    context.user_data['os_data']['Agendamento'] = agendamento_str

    os_data = context.user_data['os_data']
    summary = format_os_summary(os_data)
    
    keyboard = [
        [InlineKeyboardButton("✏️ Editar informações", callback_data='editar_inclusao')],
        [InlineKeyboardButton("✅ Confirmar inclusão", callback_data='salvar_os')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_flow')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👍 *Quase lá! Revise as informações antes de salvar:*\n\n{summary}",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS_RESUMO_INCLUSAO

async def save_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva a nova OS no Firestore."""
    query = update.callback_query
    await query.answer("Salvando O.S...")
    
    os_data = context.user_data.get('os_data')
    if not os_data:
        await query.edit_message_text("❌ Erro: Dados da O.S. perdidos. Voltando ao menu.")
        return await show_menu(update, context)

    try:
        # Prepara os dados para salvar (limpa a chave de controle de estado)
        data_to_save = {k: v for k, v in os_data.items() if k not in ['state_step', 'os_id']}
        data_to_save['Numero_da_OS'] = int(data_to_save['Numero_da_OS']) # Garante que é int no DB
        data_to_save['Criacao'] = firestore.SERVER_TIMESTAMP
        
        doc_id = os_data.get('os_id', str(uuid.uuid4()))
        await get_os_ref(doc_id).set(data_to_save)

        await query.edit_message_text(
            f"🎉 *Sucesso!* O.S. de número `{data_to_save['Numero_da_OS']}` incluída com sucesso!",
            parse_mode=ParseMode.MARKDOWN
        )
        # Limpa os dados temporários
        context.user_data.pop('os_data', None)
        
    except Exception as e:
        logger.error(f"Erro ao salvar OS: {e}")
        await query.edit_message_text(
            f"❌ Erro ao salvar a O.S. no banco de dados: {e}",
            parse_mode=ParseMode.MARKDOWN
        )

    return await show_menu(update, context)
    
# --- FLUXO DE ATUALIZAÇÃO ---

async def prompt_os_atualizacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da OS para atualização."""
    if update.callback_query: await update.callback_query.answer()
    await update.effective_message.reply_text(
        "🔄 *ATUALIZAÇÃO DE O.S.*\n\n"
        "Qual é o *Número da O.S.* que você deseja atualizar? (Somente números)",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ATUALIZACAO_OS

async def receive_os_atualizacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da OS, mostra o resumo e botões de edição."""
    os_num = update.message.text.strip()
    
    if not os_num.isdigit():
        await update.message.reply_text("❌ Número da O.S. deve conter apenas números. Tente novamente.")
        return PROMPT_ATUALIZACAO_OS

    existing_os = await fetch_os_by_num(os_num)
    
    if not existing_os:
        keyboard = [[InlineKeyboardButton("⬅️ Tentar Outro Número", callback_data='atualizar_os')],
                    [InlineKeyboardButton("🏠 Menu Principal", callback_data='menu')]]
        await update.message.reply_text(
            f"❌ O.S. de número `{os_num}` não foi encontrada no sistema.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO_OS

    # Armazena a OS no contexto para edição
    context.user_data['os_to_update'] = existing_os
    
    await update.message.reply_text(
        f"🛠️ *O.S. Selecionada para Atualização:*\n\n"
        f"{format_os_summary(existing_os)}\n\n"
        "Selecione o campo que deseja alterar:",
        reply_markup=get_edit_keyboard(existing_os),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ATUALIZACAO_CAMPO

async def prompt_atualizacao_campo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o novo valor para o campo selecionado."""
    query = update.callback_query
    await query.answer()

    action, field_key = query.data.split('_', 1) # ex: edit_Numero_da_OS
    
    # Armazena a chave do campo que será editado
    context.user_data['field_to_edit'] = field_key
    
    current_os = context.user_data['os_to_update']
    
    # Se for um campo de múltipla escolha (Criticidade, Tipo, Situação, Técnico), mostra botões.
    
    # Mapeamento de botões para campos específicos
    if field_key == 'Criticidade':
        keyboard = [
            [InlineKeyboardButton("🚨 Emergencial", callback_data='update_val_Emergencial')],
            [InlineKeyboardButton("⚠️ Urgente", callback_data='update_val_Urgente')],
            [InlineKeyboardButton("🟢 Normal", callback_data='update_val_Normal')],
            [InlineKeyboardButton("⬅️ Voltar ao Resumo", callback_data='atualizar_existente')],
        ]
        await query.edit_message_text(
            f"Selecione a nova *{field_key}*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO_VALOR
        
    elif field_key == 'Tipo':
        keyboard = [
            [InlineKeyboardButton("🔧 Corretiva", callback_data='update_val_Corretiva')],
            [InlineKeyboardButton("🧹 Preventiva", callback_data='update_val_Preventiva')],
            [InlineKeyboardButton("⬅️ Voltar ao Resumo", callback_data='atualizar_existente')],
        ]
        await query.edit_message_text(
            f"Selecione o novo *Tipo*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO_VALOR
        
    elif field_key == 'Situacao':
        keyboard = [
            [InlineKeyboardButton("Pendente", callback_data='update_val_Pendente')],
            [InlineKeyboardButton("Aguardando agendamento", callback_data='update_val_Aguardando_agendamento')],
            [InlineKeyboardButton("Agendado", callback_data='update_val_Agendado')],
            [InlineKeyboardButton("Concluído", callback_data='update_val_Concluído')],
            [InlineKeyboardButton("⬅️ Voltar ao Resumo", callback_data='atualizar_existente')],
        ]
        await query.edit_message_text(
            f"Selecione a nova *Situação*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO_VALOR

    elif field_key == 'Tecnico':
        keyboard = [
            [InlineKeyboardButton("👷 DEFINIDO", callback_data='update_val_DEFINIDO')],
            [InlineKeyboardButton("🚫 NÃO DEFINIDO", callback_data='update_val_NAO_DEFINIDO')],
            [InlineKeyboardButton("⬅️ Voltar ao Resumo", callback_data='atualizar_existente')],
        ]
        await query.edit_message_text(
            f"O *Técnico Responsável* está definido?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO_VALOR
        
    # Campos de texto livre
    await query.edit_message_text(
        f"Informe o *novo valor* para o campo '{field_key}' (Valor atual: {current_os.get(field_key, 'N/A')}):\n\n"
        f"⬅️ Ou clique /cancel para voltar ao menu principal.",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ATUALIZACAO_VALOR

async def receive_atualizacao_valor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (texto ou callback) e atualiza o Firestore."""
    field_key = context.user_data.get('field_to_edit')
    current_os = context.user_data.get('os_to_update')
    
    if not field_key or not current_os:
        await update.effective_message.reply_text("❌ Erro: Informações de edição perdidas. Voltando ao menu.")
        return await show_menu(update, context)

    is_callback = bool(update.callback_query)
    
    if is_callback:
        query = update.callback_query
        await query.answer("Atualizando...")
        
        # Lógica para botões (Criticidade, Tipo, Situação, Técnico)
        new_value_raw = query.data.split('_val_')[1]
        
        if field_key == 'Situacao':
            new_value = new_value_raw.replace('_agendamento', ' agendamento').replace('_Concluido', ' Concluído')
        elif field_key == 'Tecnico':
            if new_value_raw == 'DEFINIDO':
                # Pede o nome do técnico
                context.user_data['field_to_edit'] = 'Tecnico_Nome' # Estado temporário
                await query.edit_message_text("Qual é o *nome* do novo técnico?", parse_mode=ParseMode.MARKDOWN)
                return PROMPT_ATUALIZACAO_VALOR # Permanece no estado para receber o nome
            else: # NÃO DEFINIDO
                new_value = 'NÃO DEFINIDO'
        else:
            new_value = new_value_raw
            
        if field_key != 'Tecnico' or new_value_raw == 'NAO_DEFINIDO':
             # Atualiza no Firestore e volta para o resumo
            await update_and_show_resumo(query.effective_message, context, field_key, new_value)
            return PROMPT_ATUALIZACAO_CAMPO

    else: # Recebendo valor por texto (Mensagem)
        new_value = update.message.text.strip()
        
        # Trata o caso de receber o nome do técnico (depois de clicar em DEFINIDO)
        if field_key == 'Tecnico_Nome':
            field_key = 'Tecnico' # Volta a chave original
            await update_and_show_resumo(update.message, context, field_key, new_value)
            return PROMPT_ATUALIZACAO_CAMPO
        
        # Validação para Número da O.S. (deve ser número e único)
        if field_key == 'Numero_da_OS':
            if not new_value.isdigit():
                await update.message.reply_text("❌ O novo Número da O.S. deve ser um número. Tente novamente.")
                return PROMPT_ATUALIZACAO_VALOR
            
            # Checagem de duplicidade do novo número
            existing = await fetch_os_by_num(new_value)
            if existing and existing['doc_id'] != current_os['doc_id']:
                await update.message.reply_text("❌ Este Número da O.S. já pertence a outra OS. Tente outro.")
                return PROMPT_ATUALIZACAO_VALOR
            
            new_value = int(new_value)

        # Validação de Prazo/Agendamento
        if field_key in ('Prazo', 'Agendamento'):
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", new_value):
                 await update.message.reply_text("❌ Formato inválido. Por favor, use o formato DD/MM/AAAA.")
                 return PROMPT_ATUALIZACAO_VALOR

        # Atualiza no Firestore e volta para o resumo
        await update_and_show_resumo(update.message, context, field_key, new_value)
        return PROMPT_ATUALIZACAO_CAMPO

async def update_and_show_resumo(message, context, field_key, new_value):
    """Função central para atualizar o Firestore e reenviar o resumo."""
    current_os = context.user_data.get('os_to_update')
    os_id = current_os['doc_id']
    
    update_data = {field_key: new_value}
    
    try:
        await get_os_ref(os_id).update(update_data)
        
        # Atualiza o objeto no contexto para refletir a mudança no resumo
        current_os[field_key] = new_value
        
        # Retorna ao resumo de atualização
        await message.reply_text(
            f"✅ Campo *{field_key.replace('_', ' ')}* atualizado para: *{new_value}*\n\n"
            f"🛠️ *Resumo Atualizado:*\n\n"
            f"{format_os_summary(current_os)}\n\n"
            "Selecione outro campo para editar ou volte ao menu:",
            reply_markup=get_edit_keyboard(current_os),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao atualizar campo {field_key}: {e}")
        await message.reply_text(
            f"❌ Erro ao salvar a alteração no campo {field_key}: {e}",
            parse_mode=ParseMode.MARKDOWN
        )

# --- FLUXO DE DELEÇÃO ---

async def prompt_os_delecao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da OS para deleção."""
    if update.callback_query: await update.callback_query.answer()
    await update.effective_message.reply_text(
        "🗑️ *DELETAR O.S.*\n\n"
        "Qual é o *Número da O.S.* que você deseja deletar? (Somente números)",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_DELECAO_OS

async def receive_os_delecao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da OS, mostra resumo e pede confirmação."""
    os_num = update.message.text.strip()
    
    if not os_num.isdigit():
        await update.message.reply_text("❌ Número da O.S. deve conter apenas números. Tente novamente.")
        return PROMPT_DELECAO_OS

    existing_os = await fetch_os_by_num(os_num)
    
    if not existing_os:
        keyboard = [[InlineKeyboardButton("⬅️ Tentar Outro Número", callback_data='deletar_os')],
                    [InlineKeyboardButton("🏠 Menu Principal", callback_data='menu')]]
        await update.message.reply_text(
            f"❌ O.S. de número `{os_num}` não foi encontrada no sistema.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_DELECAO_OS

    context.user_data['os_to_delete'] = existing_os
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar exclusão", callback_data='confirmar_delecao')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_flow')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *Confirma a exclusão desta O.S.?*\n\n"
        f"{format_os_summary(existing_os)}\n\n"
        "Esta ação é *irreversível*.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_DELECAO_CONFIRMACAO

async def confirm_delecao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirma e executa a deleção."""
    query = update.callback_query
    await query.answer("Excluindo O.S...")
    
    os_to_delete = context.user_data.get('os_to_delete')
    if not os_to_delete or not os_to_delete.get('doc_id'):
        await query.edit_message_text("❌ Erro: Dados de exclusão perdidos. Voltando ao menu.")
        return await show_menu(update, context)

    try:
        os_id = os_to_delete['doc_id']
        os_num = os_to_delete['Numero_da_OS']
        
        await get_os_ref(os_id).delete()
        
        await query.edit_message_text(
            f"🗑️ *Sucesso!* O.S. de número `{os_num}` excluída.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.pop('os_to_delete', None)
        
    except Exception as e:
        logger.error(f"Erro ao deletar OS {os_id}: {e}")
        await query.edit_message_text(
            f"❌ Erro ao deletar a O.S.: {e}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    return await show_menu(update, context)

# --- FLUXO DE LISTAGEM ---

async def prompt_listagem_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a listagem e pede o Tipo."""
    if update.callback_query: await update.callback_query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔧 Corretiva", callback_data='tipo_Corretiva')],
        [InlineKeyboardButton("🧹 Preventiva", callback_data='tipo_Preventiva')],
        [InlineKeyboardButton("Tudo", callback_data='tipo_Todas')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        "📋 *LISTAGEM DE O.S.*\n\n"
        "Qual *Tipo* de O.S. você deseja listar?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU_LISTAGEM_TIPO

async def prompt_listagem_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede a Situação para filtrar."""
    query = update.callback_query
    await query.answer()
    
    tipo_filtro = query.data.split('_')[1]
    context.user_data['listagem_tipo'] = tipo_filtro
    
    keyboard = [
        [InlineKeyboardButton("Pendente", callback_data='sit_Pendente')],
        [InlineKeyboardButton("Aguardando agendamento", callback_data='sit_Aguardando_agendamento')],
        [InlineKeyboardButton("Agendado", callback_data='sit_Agendado')],
        [InlineKeyboardButton("Concluído", callback_data='sit_Concluído')],
        [InlineKeyboardButton("Tudo", callback_data='sit_Todas')],
        [InlineKeyboardButton("⬅️ Voltar ao Filtro", callback_data='listar_os')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ Tipo selecionado: *{tipo_filtro}*\n\n"
        "Agora, selecione a *Situação* das O.S. que você deseja ver:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU_LISTAGEM_SITUACAO

async def list_os_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executa a query e lista os resultados."""
    query = update.callback_query
    await query.answer("Buscando O.S. no banco...")

    tipo_filtro = context.user_data.get('listagem_tipo')
    situacao_filtro_raw = query.data.split('_')[1]
    
    situacao_filtro = situacao_filtro_raw.replace('_agendamento', ' agendamento').replace('_Concluido', ' Concluído')
    
    if not db:
        await query.edit_message_text("❌ Serviço de banco de dados indisponível.")
        return await show_menu(update, context)

    try:
        # Constroi a query
        db_query = db.collection("ordens_servico")
        
        if tipo_filtro != 'Todas':
            db_query = db_query.where("Tipo", "==", tipo_filtro)
            
        if situacao_filtro != 'Todas':
            db_query = db_query.where("Situacao", "==", situacao_filtro)
            
        # Ordena pelo número da OS
        db_query = db_query.order_by("Numero_da_OS")

        results = db_query.stream()
        
        list_items = []
        for doc in results:
            data = doc.to_dict()
            list_items.append(
                f"• OS `{data.get('Numero_da_OS', 'N/A')}` | {data.get('Prefixo_Dependencia', 'N/A')} "
                f"| Situação: *{data.get('Situacao', 'N/A')}* | Prazo: `{data.get('Prazo', 'N/A')}`"
            )
            
        if not list_items:
            message = (
                f"⚠️ Nenhuma O.S. encontrada para o filtro:\n"
                f"Tipo: *{tipo_filtro}* e Situação: *{situacao_filtro}*."
            )
        else:
            header = (
                f"✅ *{len(list_items)} O.S. encontradas* (Tipo: *{tipo_filtro}*, Situação: *{situacao_filtro}*):\n\n"
            )
            message = header + "\n".join(list_items)
            
        keyboard = [[InlineKeyboardButton("⬅️ Novo Filtro", callback_data='listar_os')],
                    [InlineKeyboardButton("🏠 Menu Principal", callback_data='menu')]]

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Erro na listagem: {e}")
        await query.edit_message_text(
            f"❌ Erro ao listar as O.S.: {e}",
            parse_mode=ParseMode.MARKDOWN
        )

    return MENU

# --- FLUXO DE PDF ---

# Funções auxiliares de extração de PDF (adaptadas do seu código original)

def limpar_valor_bruto(v):
    if v is None: return None
    v = v.strip()
    if re.fullmatch(r'[\(\-\s]*\)?', v) or v in ('()', '-', '—', ''):
        return None
    return v

def tratar_texto(valor, linha_unica=False):
    if not valor: return None
    valor = valor.replace('\r', '\n').strip()
    if '\n' in valor: partes = re.split(r'\n+', valor)
    else: partes = re.split(r'(?<=[.;:])\s+', valor)
    partes = [re.sub(r'\s+', ' ', p).strip() for p in partes if p and p.strip()]
    if not partes: return None
    if linha_unica: return " ".join(partes)
    return "\n\n".join(partes)

def extrair_dados_pdf(pdf_bytes):
    """Extrai dados da OS de bytes de PDF em memória."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    texto = "".join(pagina.get_text("text") for pagina in doc)

    dados = {}
    padroes = {
        # Adaptação dos padrões de Regex para o seu PDF. O campo 'Endereço' foi removido conforme sua lista.
        "Numero_da_OS": r"Número da O\.S\.\s*([\d]+)",
        "Chamado": r"Chamado:\s*([A-Z0-9\-]+)",
        "Prefixo_Dependencia": r"Dependência:\s*(.+?)(?=\s*Endereço:)",
        "Distancia": r"Distância:\s*(.+?)(?=\s*Ambiente:)",
        "Descricao": r"Descrição:\s*(.+?)(?=\s*(?:Sinistro:|Criticidade:|Tipo:|$))",
        "Criticidade": r"Criticidade:\s*(.+?)(?=\s*(?:Tipo:|Prazo:|Solicitante:|$))",
        "Tipo": r"Tipo:\s*(.+?)(?=\s*(?:Prazo:|Solicitante:|Matrícula:|$))",
        "Prazo": r"Prazo:\s*(.+?)(?=\s*(?:Solicitante:|Matrícula:|Telefone:|$))"
    }
    campos_tratamento = {"descricao", "criticidade", "tipo", "prazo"}

    for campo, regex in padroes.items():
        m = re.search(regex, texto, re.DOTALL)
        if not m:
            dados[campo] = None
            continue
        valor = m.group(1).strip()
        valor = limpar_valor_bruto(valor)

        if campo == "Numero_da_OS" and valor is not None:
            try: valor = int(valor)
            except ValueError: valor = None

        if valor is not None:
            if campo.lower() == "descricao":
                valor = tratar_texto(valor, linha_unica=True)
            elif campo.lower() in campos_tratamento:
                valor = tratar_texto(valor)

        if valor is not None and isinstance(valor, str) and not valor.strip(): valor = None
        dados[campo] = valor
        
    # Remove aspas/aspas duplas em excesso
    for k, v in dados.items():
        if isinstance(v, str):
            dados[k] = v.strip().strip('"').strip("'").strip()

    return dados

# Handler do PDF
async def prompt_receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Instrui o usuário a enviar o PDF."""
    if update.callback_query: await update.callback_query.answer()
    
    if not PDF_PROCESSOR_AVAILABLE:
        await update.effective_message.reply_text(
            "❌ *Recurso Indisponível*\n\n"
            "Os módulos `PyMuPDF` e `pandas` não estão instalados neste ambiente. "
            "O recurso 'Enviar PDF' não pode ser executado.",
            parse_mode=ParseMode.MARKDOWN
        )
        return MENU
        
    await update.effective_message.reply_text(
        "📄 *ENVIO DE PDF*\n\n"
        "Por favor, envie o arquivo PDF da Ordem de Serviço.\n"
        "O bot tentará extrair os campos automaticamente.",
        parse_mode=ParseMode.MARKDOWN
    )
    return RECEIVE_PDF


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o PDF, extrai os dados e processa no banco."""
    message = update.effective_message
    
    if not message.document or not message.document.file_name.lower().endswith('.pdf'):
        await message.reply_text("❌ Por favor, envie um arquivo PDF válido.")
        return RECEIVE_PDF

    file_id = message.document.file_id
    new_file = await context.bot.get_file(file_id)
    pdf_bytes = io.BytesIO()
    
    await message.reply_text("⏳ Recebendo e processando PDF...")
    
    try:
        await new_file.download_to_memory(pdf_bytes)
        dados_extraidos = extrair_dados_pdf(pdf_bytes.getvalue())
        
        # 1. Validação de dados mínimos
        os_num = dados_extraidos.get("Numero_da_OS")
        if not os_num:
            await message.reply_text("❌ Não foi possível extrair o *Número da O.S.* do PDF. Processamento cancelado.", parse_mode=ParseMode.MARKDOWN)
            return await show_menu(update, context)
            
        # 2. Prepara o objeto para o Firestore
        dados_os = {k: v for k, v in dados_extraidos.items() if v is not None}
        
        # 3. Adiciona campos padrão se não existirem
        if 'Situacao' not in dados_os: dados_os['Situacao'] = 'Pendente'
        if 'Tecnico' not in dados_os: dados_os['Tecnico'] = 'NÃO DEFINIDO'
        if 'Agendamento' not in dados_os: dados_os['Agendamento'] = 'N/A'
        
        # 4. Checagem de Duplicidade
        existing_os = await fetch_os_by_num(os_num)
        
        if existing_os:
            # ATUALIZAÇÃO
            os_id = existing_os['doc_id']
            await get_os_ref(os_id).update(dados_os)
            
            summary = format_os_summary({**existing_os, **dados_os}) # Merge dos dados
            
            await message.reply_text(
                f"🔄 *Sucesso!* O.S. `{os_num}` *atualizada* com dados do PDF.\n\n"
                f"{summary}",
                parse_mode=ParseMode.MARKDOWN
            )
            
        else:
            # INCLUSÃO
            doc_id = str(uuid.uuid4())
            dados_os['Criacao'] = firestore.SERVER_TIMESTAMP
            
            await get_os_ref(doc_id).set(dados_os)
            
            summary = format_os_summary(dados_os)
            
            await message.reply_text(
                f"🎉 *Sucesso!* O.S. `{os_num}` *incluída* com dados do PDF.\n\n"
                f"{summary}",
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        logger.error(f"Erro ao processar PDF: {e}")
        await message.reply_text(
            f"❌ Erro grave ao processar o PDF. Detalhe: {e}",
            parse_mode=ParseMode.MARKDOWN
        )

    return await show_menu(update, context)

# --- FLUXO DE LEMBRETE MANUAL ---

async def prompt_lembrete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de lembrete manual."""
    if update.callback_query: await update.callback_query.answer()
    
    await update.effective_message.reply_text(
        "⏰ *AGENDAR LEMBRETE MANUAL*\n\n"
        "Qual *Número da O.S.* ou Chamado você deseja associar a este lembrete? "
        "(Se não for para uma OS, digite 'GERAL')",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID e solicita a data/hora."""
    os_id_or_geral = update.message.text.strip()
    context.user_data['lembrete_target'] = os_id_or_geral
    
    await update.message.reply_text(
        f"📅 Informe a *data e hora* do lembrete no formato DD/MM/AAAA HH:MM "
        f"(Ex: 30/11/2025 10:30):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_DATA

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora e solicita a mensagem."""
    date_time_str = update.message.text.strip()
    
    try:
        # Tenta parsear a data
        agendamento = datetime.strptime(date_time_str, "%d/%m/%Y %H:%M")
        if agendamento < datetime.now():
            await update.message.reply_text("❌ A data e hora devem ser no futuro. Tente novamente.")
            return PROMPT_LEMBRETE_DATA
            
        context.user_data['lembrete_datetime'] = agendamento
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Use o formato DD/MM/AAAA HH:MM (Ex: 30/11/2025 10:30). "
            "Tente novamente."
        )
        return PROMPT_LEMBRETE_DATA

    await update.message.reply_text(
        "💬 Por fim, qual a *mensagem personalizada* para o lembrete?",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_MSG

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a mensagem e agenda o lembrete via JobQueue."""
    message_text = update.message.text.strip()
    target = context.user_data.get('lembrete_target')
    agendamento = context.user_data.get('lembrete_datetime')
    chat_id = update.effective_chat.id
    
    job_name = f"lembrete_{uuid.uuid4()}"
    
    lembrete_msg = (
        f"🔔 *LEMBRETE AGENDADO* 🔔\n\n"
        f"Assunto: `{target}`\n"
        f"Mensagem: _{message_text}_"
    )

    async def send_reminder(context: CallbackContext):
        await context.bot.send_message(
            chat_id=context.job.data['chat_id'],
            text=context.job.data['msg'],
            parse_mode=ParseMode.MARKDOWN
        )
        # Opcional: Remover o lembrete agendado do Firestore se estivesse salvo lá

    # Agenda o Job
    context.application.job_queue.run_once(
        send_reminder,
        agendamento,
        name=job_name,
        data={'chat_id': chat_id, 'msg': lembrete_msg}
    )
    
    await update.message.reply_text(
        f"✅ Lembrete agendado para o dia *{agendamento.strftime('%d/%m/%Y às %H:%M')}*!",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Limpa dados temporários
    context.user_data.pop('lembrete_target', None)
    context.user_data.pop('lembrete_datetime', None)

    return await show_menu(update, context)
    
# --- FLUXO DE AJUDA GERAL ---

async def show_ajuda_geral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe a tela de ajuda geral."""
    if update.callback_query: await update.callback_query.answer()
    
    help_text = (
        "❓ *AJUDA GERAL - Guia do Bot O.S.*\n\n"
        "Este bot ajuda você a gerenciar suas Ordens de Serviço (O.S.) no Firebase Firestore.\n\n"
        "*Comandos Principais:*\n"
        "• `/start`: Inicia ou reinicia o bot, exibindo o Menu Principal.\n"
        "• `/cancel`: Cancela o fluxo atual e retorna ao Menu Principal.\n\n"
        "*Funcionalidades:*\n"
        "1. *Incluir O.S.*: Fluxo guiado de 11 passos para cadastrar uma nova O.S., com validação de duplicidade.\n"
        "2. *Atualizar O.S.*: Permite buscar uma O.S. pelo número e editar *qualquer campo* individualmente.\n"
        "3. *Deletar O.S.*: Solicita o número e a confirmação para exclusão total.\n"
        "4. *Listar O.S.*: Permite filtrar O.S. por *Tipo* (Corretiva/Preventiva) e *Situação* (Pendente, Agendado, etc.).\n"
        "5. *Enviar PDF*: Analisa um PDF de O.S. (no formato BB) para extrair e salvar/atualizar automaticamente os campos essenciais.\n"
        "6. *Lembrete*: Permite agendar alertas personalizados com data e hora para qualquer O.S. ou assunto geral.\n\n"
        "🔄 *Alertas Automáticos:*\n"
        "O bot verifica automaticamente O.S. com prazo de vencimento em 1, 2 dias, ou já vencidas (exceto se 'Concluído') e envia notificações periódicas."
    )
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Voltar ao Menu Principal", callback_data='menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text(
        help_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return AJUDA_GERAL

# --- Funções de Callback Genéricas ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gerencia callbacks que redirecionam a fluxos específicos."""
    query = update.callback_query
    action = query.data
    
    if action == 'menu' or action == 'cancel_flow':
        return await cancel(update, context)
        
    elif action == 'incluir_os':
        return await prompt_os_numero(update, context)
        
    elif action == 'atualizar_os':
        return await prompt_os_atualizacao(update, context)
        
    elif action == 'deletar_os':
        return await prompt_os_delecao(update, context)
        
    elif action == 'listar_os':
        return await prompt_listagem_tipo(update, context)

    elif action == 'enviar_pdf':
        return await prompt_receive_pdf(update, context)
        
    elif action == 'lembrete_manual_menu':
        return await prompt_lembrete_menu(update, context)
        
    elif action == 'ajuda_geral':
        return await show_ajuda_geral(update, context)
        
    # Lógica de atualização a partir de duplicidade ou resumo final
    elif action == 'atualizar_existente' or action == 'editar_inclusao':
        if action == 'atualizar_existente': # Veio da checagem de duplicidade
            os_data = context.user_data.get('os_to_update')
        else: # Veio do resumo de inclusão
            os_data = context.user_data.get('os_data')
        
        if not os_data:
            await query.edit_message_text("❌ Erro: Dados de atualização perdidos. Voltando ao menu.")
            return await show_menu(update, context)
        
        # O fluxo de 'editar_inclusao' usa o mesmo menu de edição do fluxo de atualização
        await query.edit_message_text(
            f"🛠️ *Selecione o campo para edição:* (OS {os_data.get('Numero_da_OS', 'N/A')})\n\n"
            f"{format_os_summary(os_data)}\n\n"
            "Escolha o campo para editar:",
            reply_markup=get_edit_keyboard(os_data),
            parse_mode=ParseMode.MARKDOWN
        )
        # O estado muda para o de edição de campo, que é onde o get_edit_keyboard leva
        return PROMPT_ATUALIZACAO_CAMPO

    elif action == 'salvar_os':
        return await save_os(update, context)
        
    elif action == 'confirmar_delecao':
        return await confirm_delecao(update, context)
        
    return MENU # fallback

# --- Main ---

def main() -> None:
    """Inicia o bot usando Webhook."""
    if not TOKEN:
        logger.error("TOKEN do Telegram não encontrado em .env. Encerrando.")
        return

    application = Application.builder().token(TOKEN).build()
    
    # --- Conversation Handler ---
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^incluir_os$|^atualizar_os$|^deletar_os$|^listar_os$|^enviar_pdf$|^lembrete_manual_menu$|^ajuda_geral$|^menu$'),
                MessageHandler(filters.COMMAND, fallback_command),
            ],
            
            # Fluxo de Inclusão Detalhado
            PROMPT_OS_NUMERO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_numero),
                CallbackQueryHandler(callback_handler, pattern='^atualizar_existente$|^menu$|^incluir_os$'),
            ],
            PROMPT_OS_PREFIXO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_prefixo)],
            PROMPT_OS_CHAMADO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_chamado)],
            PROMPT_OS_DISTANCIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_distancia)],
            PROMPT_OS_DESCRICAO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_descricao)],
            PROMPT_OS_CRITICIDADE: [
                CallbackQueryHandler(receive_os_criticidade, pattern='^crit_'),
                CallbackQueryHandler(callback_handler, pattern='^cancel_flow$'),
            ],
            PROMPT_OS_TIPO: [
                CallbackQueryHandler(receive_os_tipo, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^cancel_flow$'),
            ],
            PROMPT_OS_PRAZO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_prazo)],
            PROMPT_OS_SITUACAO: [
                CallbackQueryHandler(receive_os_situacao, pattern='^sit_'),
                CallbackQueryHandler(callback_handler, pattern='^cancel_flow$'),
            ],
            PROMPT_OS_TECNICO: [
                CallbackQueryHandler(receive_os_tecnico, pattern='^tec_'),
                CallbackQueryHandler(callback_handler, pattern='^cancel_flow$'),
            ],
            PROMPT_OS_NOME_TECNICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_nome_tecnico)],
            PROMPT_OS_RESUMO_INCLUSAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_os_resumo_inclusao),
                CallbackQueryHandler(callback_handler, pattern='^salvar_os$|^editar_inclusao$|^cancel_flow$'),
            ],
            
            # Fluxo de Atualização/Edição
            PROMPT_ATUALIZACAO_OS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_atualizacao)],
            PROMPT_ATUALIZACAO_CAMPO: [CallbackQueryHandler(prompt_atualizacao_campo, pattern='^edit_')],
            PROMPT_ATUALIZACAO_VALOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_atualizacao_valor),
                CallbackQueryHandler(receive_atualizacao_valor, pattern='^update_val_'),
                CallbackQueryHandler(callback_handler, pattern='^atualizar_existente$'), # Voltar ao resumo
            ],

            # Fluxo de Deleção
            PROMPT_DELECAO_OS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_delecao)],
            PROMPT_DELECAO_CONFIRMACAO: [CallbackQueryHandler(callback_handler, pattern='^confirmar_delecao$|^cancel_flow$')],

            # Fluxo de Listagem
            MENU_LISTAGEM_TIPO: [
                CallbackQueryHandler(prompt_listagem_situacao, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            MENU_LISTAGEM_SITUACAO: [
                CallbackQueryHandler(list_os_results, pattern='^sit_'),
                CallbackQueryHandler(callback_handler, pattern='^listar_os$'),
            ],

            # Fluxo de PDF
            RECEIVE_PDF: [
                MessageHandler(filters.Document.PDF, handle_pdf),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            
            # Fluxo de Lembrete
            PROMPT_ID_LEMBRETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data)],
            PROMPT_LEMBRETE_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg)],
            PROMPT_LEMBRETE_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete)],
            
            # Ajuda Geral
            AJUDA_GERAL: [CallbackQueryHandler(callback_handler, pattern='^menu$')],
            
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(callback_handler, pattern='^menu$'), # Última chance para voltar ao menu
        ],
    )
    
    # Função de fallback para comandos não esperados
    async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "❌ Comando não reconhecido neste ponto. Por favor, utilize os botões ou /cancel para voltar ao Menu Principal."
        )

    # Adiciona o ConversationHandler e o start
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start)) 

    # 4. Configuração do Webhook
    if WEBHOOK_URL and TOKEN:
        try:
            # Remove o webhook anterior se houver
            # application.bot.delete_webhook() 
            
            # Define a URL do webhook no Telegram
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN, 
                webhook_url=WEBHOOK_URL + WEBHOOK_PATH, 
            )
            logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
            logger.info(f"Webhook URL configurada no Telegram: {WEBHOOK_URL + WEBHOOK_PATH}")

        except Exception as e:
            logger.error(f"Erro ao configurar/iniciar Webhook: {e}")
    else:
        logger.error("Variáveis de Webhook (WEBHOOK_URL e/ou TELEGRAM_TOKEN) ausentes. Certifique-se de que estão definidas no ambiente.")
        logger.info("Bot rodando em modo polling (fallback)...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
