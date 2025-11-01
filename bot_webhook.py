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

# --- Imports para PDF (necessitam de instalação via pip) ---
try:
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    # Se PyMuPDF ou Pandas não estiverem disponíveis (como em ambientes limitados)
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    class MockDataFrame: # Placeholder para evitar erros
        def __init__(self, *args, **kwargs): pass
    pd = MockDataFrame()

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
)
from telegram.constants import ParseMode

# --- Configuração ---

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Define níveis de log mais altos para bibliotecas que usam muito log
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Estados para o ConversationHandler
(MENU, PROMPT_OS_ID, PROMPT_CHAMADO, PROMPT_PREFIXO, PROMPT_DISTANCIA, PROMPT_DESCRICAO, 
PROMPT_CRITICIDADE, PROMPT_TIPO, PROMPT_PRAZO, PROMPT_SITUACAO, PROMPT_TECNICO, PROMPT_TECNICO_NOME, 
RESUMO_INCLUSAO, PROMPT_OS_UPDATE, UPDATE_SELECTION, PROMPT_UPDATE_FIELD, PROMPT_OS_DELETE, 
CONFIRM_DELETE, LISTAR_TIPO, LISTAR_SITUACAO, LEMBRETE_MENU, PROMPT_ID_LEMBRETE, 
PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG, PROCESSAR_PDF, AJUDA_GERAL) = range(26)

# --- Firebase Init ---

# Usando as variáveis de ambiente para inicialização do Firebase Admin SDK
try:
    FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if FIREBASE_CREDENTIALS_JSON:
        cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
        if not firebase_admin._apps:
            initialize_app(cred, {'projectId': 'automatizacaoos'})
        db = firestore.client()
        logger.info("Firebase inicializado com sucesso.")
    else:
        logger.error("A variável de ambiente 'FIREBASE_CREDENTIALS_JSON' não foi definida.")
except Exception as e:
    logger.error(f"Erro ao inicializar o Firebase: {e}")
    db = None

# --- Variáveis de Ambiente e Auto-Ping ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
PORT = int(os.environ.get("PORT", "8080")) 
WEBHOOK_PATH = "/" + TOKEN 
PING_INTERVAL_SECONDS = 14 * 60 # 14 minutos

async def ping_self_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia um GET request para a URL do webhook para evitar que o Render durma."""
    if not WEBHOOK_URL or not TOKEN:
        logger.warning("Variáveis WEBHOOK_URL e/ou TELEGRAM_TOKEN não definidas. Não é possível realizar o auto-ping.")
        return

    ping_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}" 
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ping_url, timeout=10) as response:
                logger.info(f"Auto-ping concluído. Status da resposta: {response.status}")
    except Exception as e:
        logger.error(f"Erro durante o auto-ping: {e}")

# --- Funções de Formatação e Auxiliares ---

def format_os_data(os_data: dict) -> str:
    """Formata os dados de OS em uma string de resumo."""
    prazo = os_data.get('Prazo')
    agendamento = os_data.get('Agendamento')
    
    # Tentativa de formatar Prazo e Agendamento se forem objetos datetime ou strings válidas
    try:
        if isinstance(prazo, datetime):
            prazo_str = prazo.strftime('%d/%m/%Y')
        elif prazo:
             # Tenta converter string para datetime e formatar
            prazo_dt = datetime.strptime(str(prazo).split(' ')[0], '%Y-%m-%d')
            prazo_str = prazo_dt.strftime('%d/%m/%Y')
        else:
            prazo_str = 'Não informado'
    except:
        prazo_str = str(prazo) if prazo else 'Não informado'

    try:
        if isinstance(agendamento, datetime):
            agendamento_str = agendamento.strftime('%d/%m/%Y')
        elif agendamento:
            agendamento_dt = datetime.strptime(str(agendamento).split(' ')[0], '%Y-%m-%d')
            agendamento_str = agendamento_dt.strftime('%d/%m/%Y')
        else:
            agendamento_str = 'Não informado'
    except:
        agendamento_str = str(agendamento) if agendamento else 'Não informado'


    return (
        "📋 <b>RESUMO DA O.S.</b>\n\n"
        f"<b>Número:</b> <code>{os_data.get('Número da O.S.', 'N/A')}</code>\n"
        f"<b>Chamado:</b> {os_data.get('Chamado', 'N/A')}\n"
        f"<b>Prefixo/Dependência:</b> {os_data.get('Prefixo/Dependência', 'N/A')}\n"
        f"<b>Distância:</b> {os_data.get('Distância', 'N/A')}\n"
        f"<b>Descrição:</b> {os_data.get('Descrição', 'N/A')}\n"
        f"<b>Criticidade:</b> {os_data.get('Criticidade', 'N/A')}\n"
        f"<b>Tipo:</b> {os_data.get('Tipo', 'N/A')}\n"
        f"<b>Prazo:</b> {prazo_str}\n"
        f"<b>Situação:</b> {os_data.get('Situação', 'Pendente')}\n"
        f"<b>Técnico:</b> {os_data.get('Técnico', 'Não Definido')}\n"
        f"<b>Agendamento:</b> {agendamento_str}\n"
        f"<b>Lembrete:</b> {os_data.get('Lembrete', 'Nenhum agendado')}\n"
    )

def get_os_ref(os_number: str) -> firestore.DocumentReference:
    """Obtém a referência do documento da OS no Firestore."""
    if not db: raise Exception("Firestore não inicializado.")
    # Usando a convenção de IDs simples para a coleção
    return db.collection("ordens_servico").document(str(os_number))

async def fetch_os_by_number(os_number: str) -> dict | None:
    """Busca uma OS pelo seu número no Firestore."""
    if not db: return None
    try:
        doc_ref = get_os_ref(os_number)
        doc = await asyncio.to_thread(doc_ref.get)
        if doc.exists:
            data = doc.to_dict()
            # Certificar que o Número da OS está no formato correto (string)
            data['Número da O.S.'] = str(os_number) 
            return data
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar OS {os_number}: {e}")
        return None

# --- Funções do Menu Principal ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Retorna o teclado do menu principal."""
    keyboard = [
        [InlineKeyboardButton("📝 Incluir O.S.", callback_data="incluir_os")],
        [InlineKeyboardButton("🔄 Atualizar O.S.", callback_data="atualizar_os")],
        [InlineKeyboardButton("🗑️ Deletar O.S.", callback_data="deletar_os")],
        [InlineKeyboardButton("📋 Listar O.S.", callback_data="listar_os")],
        [InlineKeyboardButton("📄 Enviar PDF", callback_data="enviar_pdf")],
        [InlineKeyboardButton("🔔 Lembrete", callback_data="lembrete_menu")],
        [InlineKeyboardButton("❓ Ajuda Geral", callback_data="ajuda_geral")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e exibe o menu principal."""
    if update.effective_chat:
        user_name = update.effective_user.first_name if update.effective_user else "usuário"
        
        # Mensagem com o placeholder da imagem e as opções
        message = (
            "👋 Olá, <b>{user_name}</b>! \n"
            "[Imagem de Boas-Vindas - Substitua esta URL por uma imagem pública se desejar]\n\n"
            "Sou o seu <b>Bot de Gestão de Ordens de Serviço (OS)</b>. Escolha uma opção abaixo para começar:"
        ).format(user_name=user_name)
        
        # Responde à mensagem (se for um /start) ou edita (se for um retorno de fluxo)
        if update.message:
            await update.message.reply_text(
                message,
                reply_markup=get_main_menu_keyboard(),
                parse_mode=ParseMode.HTML
            )
        elif update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                message,
                reply_markup=get_main_menu_keyboard(),
                parse_mode=ParseMode.HTML
            )

        # Limpa dados de conversa anteriores
        context.user_data.clear()
        return MENU

# Função para cancelar a conversa
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa e termina a sessão."""
    if update.effective_message:
        await update.effective_message.reply_text(
            'Operação cancelada. Digite /start para iniciar uma nova conversa.'
        )
    context.user_data.clear()
    return ConversationHandler.END

# --- Fluxo de Inclusão/Edição de O.S. ---

# Passo 1: Solicitar o Número da O.S.
async def start_incluir_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de inclusão de OS."""
    query = update.callback_query
    await query.answer()
    context.user_data['os_data'] = {} # Inicializa o dicionário de dados da nova OS
    context.user_data['current_step'] = PROMPT_OS_ID # Rastreia o passo atual
    context.user_data['is_new_os'] = True # Sinaliza que é uma nova inclusão

    await query.edit_message_text(
        "📝 <b>INCLUSÃO DE NOVA O.S.</b>\n\n"
        "Por favor, digite o <b>Número da O.S.</b> (apenas números).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_OS_ID

# Verifica se a OS já existe
async def prompt_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Número da OS e verifica duplicidade."""
    os_number = update.message.text.strip()
    
    # Validação simples (apenas números)
    if not os_number.isdigit():
        await update.message.reply_text("❌ Por favor, digite apenas números para o Número da O.S.")
        return PROMPT_OS_ID

    os_data = await fetch_os_by_number(os_number)
    
    if os_data:
        # OS já cadastrada: Sugerir Atualização
        keyboard = [
            [InlineKeyboardButton("✅ Sim, Atualizar", callback_data=f"update_existing_{os_number}")],
            [InlineKeyboardButton("❌ Não (Cancelar)", callback_data="cancel")],
            [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")],
        ]
        context.user_data['os_data'] = os_data # Salva os dados existentes
        await update.message.reply_text(
            f"⚠️ O Número da O.S. <code>{os_number}</code> já está cadastrado.\n\n"
            f"Deseja atualizar as informações desta O.S.?\n\n{format_os_data(os_data)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        # O fluxo de atualização será tratado no callback_handler
        return MENU # Fica no menu esperando o callback
    else:
        # OS nova: Prossegue
        context.user_data['os_data']['Número da O.S.'] = os_number
        context.user_data['is_new_os'] = True
        
        # Próxima etapa
        return await prompt_prefixo(update, context, update.message.message_id)
        
# Sequência de prompts de texto (Chamado, Prefixo, Distância, Descrição, Prazo)
async def prompt_next_field(update: Update, context: ContextTypes.DEFAULT_TYPE, next_field: str, next_state: int, field_name: str) -> int:
    """Função genérica para capturar campo de texto."""
    text = update.message.text.strip()
    
    # Salva o dado da etapa anterior (se for a primeira vez)
    if context.user_data['current_step'] != PROMPT_OS_ID:
        context.user_data['os_data'][field_name] = text
    
    # Se estiver no modo de edição, salva o dado e volta para o resumo
    if context.user_data.get('editing_field'):
        await update.message.reply_text(f"✅ Campo <b>{context.user_data['editing_field']}</b> atualizado!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context) # Volta para o resumo de edição

    context.user_data['current_step'] = next_state
    
    # Pergunta o próximo campo
    await update.message.reply_text(
        f"👍 Entendido! Agora, por favor, digite o <b>{next_field}</b>:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data=f"back_{context.user_data['current_step']}")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return next_state


async def prompt_prefixo(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None) -> int:
    # A primeira etapa é especial (o Número da OS foi salvo em prompt_os_id)
    if not context.user_data.get('is_new_os'):
        text = update.message.text.strip()
        context.user_data['os_data']['Prefixo/Dependência'] = text
        if context.user_data.get('editing_field'):
            await update.message.reply_text("✅ Campo <b>Prefixo/Dependência</b> atualizado!", parse_mode=ParseMode.HTML)
            del context.user_data['editing_field']
            return await show_resumo_inclusao(update, context)
    
    context.user_data['current_step'] = PROMPT_CHAMADO
    
    # Tenta editar a mensagem original ou envia uma nova
    try:
        if update.callback_query:
             await update.callback_query.edit_message_text(
                "Por favor, digite o <b>Número do Chamado</b>:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_OS_ID")]
                ]),
                parse_mode=ParseMode.HTML
            )
        elif update.message and message_id:
             await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Por favor, digite o <b>Número do Chamado</b>:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_OS_ID")]
                ]),
                parse_mode=ParseMode.HTML
            )
        else: # Se veio do prompt_os_id
            await update.message.reply_text(
                "👍 OS validada! Por favor, digite o <b>Número do Chamado</b>:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_OS_ID")]
                ]),
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.warning(f"Erro ao editar mensagem: {e}")
        await update.message.reply_text(
            "👍 OS validada! Por favor, digite o <b>Número do Chamado</b>:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_OS_ID")]
            ]),
            parse_mode=ParseMode.HTML
        )
        
    return PROMPT_CHAMADO


async def prompt_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await prompt_next_field(update, context, "Prefixo/Dependência", PROMPT_PREFIXO, 'Chamado')

async def prompt_distancia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await prompt_next_field(update, context, "Distância (em Km)", PROMPT_DISTANCIA, 'Prefixo/Dependência')

async def prompt_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await prompt_next_field(update, context, "Descrição do Serviço", PROMPT_DESCRICAO, 'Distância')

# Passo 6: Criticidade (Botões)
async def prompt_criticidade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Descrição e solicita a Criticidade (Botões)."""
    # Salva a Descrição
    text = update.message.text.strip()
    context.user_data['os_data']['Descrição'] = text
    
    # Se estiver no modo de edição, salva o dado e volta para o resumo
    if context.user_data.get('editing_field'):
        await update.message.reply_text("✅ Campo <b>Descrição</b> atualizado!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)

    context.user_data['current_step'] = PROMPT_CRITICIDADE
    
    keyboard = [
        [InlineKeyboardButton("🚨 Emergencial", callback_data="criticidade_Emergencial")],
        [InlineKeyboardButton("⚠️ Urgente", callback_data="criticidade_Urgente")],
        [InlineKeyboardButton("🟢 Normal", callback_data="criticidade_Normal")],
        [InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_DESCRICAO")],
    ]
    await update.message.reply_text(
        "🛠️ Qual é a <b>Criticidade</b> desta O.S.?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_CRITICIDADE

# Passo 7: Tipo (Botões)
async def prompt_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE, value=None) -> int:
    """Recebe a Criticidade (ou edita) e solicita o Tipo (Botões)."""
    if not value:
        query = update.callback_query
        await query.answer()
        value = query.data.split('_')[1]

    context.user_data['os_data']['Criticidade'] = value
    
    if context.user_data.get('editing_field'):
        await update.callback_query.edit_message_text(f"✅ Campo <b>Criticidade</b> atualizado para {value}!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)

    context.user_data['current_step'] = PROMPT_TIPO
    
    keyboard = [
        [InlineKeyboardButton("🔧 Corretiva", callback_data="tipo_Corretiva")],
        [InlineKeyboardButton("🧹 Preventiva", callback_data="tipo_Preventiva")],
        [InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_CRITICIDADE")],
    ]
    await update.callback_query.edit_message_text(
        "⚙️ Qual é o <b>Tipo</b> de Serviço?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_TIPO

# Passo 8: Prazo
async def prompt_prazo(update: Update, context: ContextTypes.DEFAULT_TYPE, value=None) -> int:
    """Recebe o Tipo e solicita o Prazo."""
    if not value:
        query = update.callback_query
        await query.answer()
        value = query.data.split('_')[1]

    context.user_data['os_data']['Tipo'] = value
    
    if context.user_data.get('editing_field'):
        await update.callback_query.edit_message_text(f"✅ Campo <b>Tipo</b> atualizado para {value}!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)

    context.user_data['current_step'] = PROMPT_PRAZO
    
    await update.callback_query.edit_message_text(
        "📅 Por favor, digite o <b>Prazo Final</b> para a conclusão da O.S. (Formato: DD/MM/AAAA):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_TIPO")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_PRAZO

# Passo 9: Situação (Botões)
async def prompt_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Prazo e solicita a Situação (Botões)."""
    text = update.message.text.strip()
    
    # Validação do Prazo (DD/MM/AAAA)
    try:
        # Tenta parsear para datetime
        prazo_dt = datetime.strptime(text, '%d/%m/%Y')
        context.user_data['os_data']['Prazo'] = prazo_dt
    except ValueError:
        await update.message.reply_text("❌ Formato de Prazo inválido. Use DD/MM/AAAA (ex: 25/10/2025).")
        return PROMPT_PRAZO
    
    if context.user_data.get('editing_field'):
        await update.message.reply_text("✅ Campo <b>Prazo</b> atualizado!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)

    context.user_data['current_step'] = PROMPT_SITUACAO
    
    keyboard = [
        [InlineKeyboardButton("🔴 Pendente", callback_data="situacao_Pendente")],
        [InlineKeyboardButton("🟡 Aguardando Agendamento", callback_data="situacao_Aguardando Agendamento")],
        [InlineKeyboardButton("🔵 Agendado", callback_data="situacao_Agendado")],
        [InlineKeyboardButton("🟢 Concluído", callback_data="situacao_Concluído")],
        [InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_PRAZO")],
    ]
    await update.message.reply_text(
        "🚦 Qual é a <b>Situação</b> atual da O.S.?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_SITUACAO

# Passo 10: Técnico (Botões)
async def prompt_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE, value=None) -> int:
    """Recebe a Situação (ou edita) e solicita o Técnico (Botões)."""
    if not value:
        query = update.callback_query
        await query.answer()
        value = query.data.split('_')[1]

    context.user_data['os_data']['Situação'] = value
    
    if context.user_data.get('editing_field'):
        await update.callback_query.edit_message_text(f"✅ Campo <b>Situação</b> atualizado para {value}!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)

    context.user_data['current_step'] = PROMPT_TECNICO
    
    keyboard = [
        [InlineKeyboardButton("👷 DEFINIDO", callback_data="tecnico_definido")],
        [InlineKeyboardButton("🚫 NÃO DEFINIDO", callback_data="tecnico_nao_definido")],
        [InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_SITUACAO")],
    ]
    await update.callback_query.edit_message_text(
        "👤 O <b>Técnico Responsável</b> já está definido?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_TECNICO

# Passo 11: Nome do Técnico / Próximo Passo
async def handle_tecnico_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manipula a seleção de Técnico."""
    query = update.callback_query
    await query.answer()
    
    selection = query.data.split('_')[1]
    
    if selection == 'nao':
        context.user_data['os_data']['Técnico'] = 'Não Definido'
        context.user_data['os_data']['Agendamento'] = 'Não Informado' # Adiciona Agendamento
        
        if context.user_data.get('editing_field'):
            await query.edit_message_text("✅ Campo <b>Técnico</b> atualizado para 'Não Definido'!", parse_mode=ParseMode.HTML)
            del context.user_data['editing_field']
            return await show_resumo_inclusao(update, context)

        # Se NÃO DEFINIDO, pula para o Resumo
        return await show_resumo_inclusao(update, context)
    
    elif selection == 'definido':
        context.user_data['current_step'] = PROMPT_TECNICO_NOME
        
        await query.edit_message_text(
            "✍️ Qual é o <b>nome do técnico</b> responsável?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_TECNICO")]
            ]),
            parse_mode=ParseMode.HTML
        )
        return PROMPT_TECNICO_NOME
    
    # Tratamento de edição de campo
    elif selection == 'definido_update':
        context.user_data['editing_field'] = 'Técnico'
        context.user_data['current_step'] = PROMPT_TECNICO_NOME
        await query.edit_message_text(
            "✍️ Qual é o <b>novo nome do técnico</b> responsável?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Voltar ao Resumo", callback_data="show_resumo")]
            ]),
            parse_mode=ParseMode.HTML
        )
        return PROMPT_TECNICO_NOME


# Passo 12: Agendamento / Resumo
async def prompt_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o nome do técnico e solicita o Agendamento."""
    text = update.message.text.strip()
    context.user_data['os_data']['Técnico'] = text

    # Se estiver editando, vai para o resumo
    if context.user_data.get('editing_field') == 'Técnico':
        await update.message.reply_text("✅ Campo <b>Técnico</b> atualizado!", parse_mode=ParseMode.HTML)
        del context.user_data['editing_field']
        return await show_resumo_inclusao(update, context)
        
    context.user_data['current_step'] = RESUMO_INCLUSAO
    
    await update.message.reply_text(
        "📅 Por favor, digite a <b>Data de Agendamento</b> (Formato: DD/MM/AAAA) ou 'N/A' se não agendado:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="back_PROMPT_TECNICO_NOME")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return RESUMO_INCLUSAO # Usa RESUMO_INCLUSAO para capturar o Agendamento


# Passo 13: Exibir Resumo e Opções (Confirmação/Edição/Cancelamento)
async def show_resumo_inclusao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe o resumo da OS e pede confirmação/edição."""
    
    # Se veio do prompt_agendamento, salva o dado
    if context.user_data['current_step'] == RESUMO_INCLUSAO and update.message:
        text = update.message.text.strip()
        if text.upper() == 'N/A':
            context.user_data['os_data']['Agendamento'] = 'Não Informado'
        else:
            try:
                # Tenta parsear para datetime
                agendamento_dt = datetime.strptime(text, '%d/%m/%Y')
                context.user_data['os_data']['Agendamento'] = agendamento_dt
            except ValueError:
                await update.message.reply_text("❌ Formato de Agendamento inválido. Use DD/MM/AAAA ou 'N/A'.")
                return RESUMO_INCLUSAO
    
    os_data = context.user_data.get('os_data', {})

    # Adiciona valores default se estiver faltando algo essencial para o resumo
    if 'Lembrete' not in os_data: os_data['Lembrete'] = 'Nenhum'
    if 'Situação' not in os_data: os_data['Situação'] = 'Pendente'

    resumo_text = format_os_data(os_data)
    
    # Teclado para Resumo
    keyboard = [
        [InlineKeyboardButton("✏️ Editar informações", callback_data="edit_resumo")],
        [InlineKeyboardButton("✅ Confirmar inclusão", callback_data="confirm_save")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")],
    ]
    
    # Enviar mensagem ou editar a última
    if update.callback_query:
        await update.callback_query.edit_message_text(
            resumo_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    elif update.message:
        await update.message.reply_text(
            resumo_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    return RESUMO_INCLUSAO

# Salvar no Firestore
async def save_os_to_firestore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva a OS no Firestore."""
    query = update.callback_query
    await query.answer()
    
    os_data = context.user_data.get('os_data')
    os_number = os_data.get('Número da O.S.')
    
    if not os_data or not os_number:
        await query.edit_message_text("❌ Erro: Dados da O.S. incompletos. Por favor, reinicie com /start.")
        context.user_data.clear()
        return ConversationHandler.END
        
    try:
        os_ref = get_os_ref(os_number)
        
        # O campo 'Lembrete' é apenas para exibição no resumo. Os alertas reais serão em outra coleção.
        if 'Lembrete' in os_data and os_data['Lembrete'] == 'Nenhum':
            del os_data['Lembrete']
        
        # Adiciona timestamp de criação e atualização
        os_data['created_at'] = datetime.now()
        os_data['updated_at'] = datetime.now()
        
        await asyncio.to_thread(os_ref.set, os_data) # Salva/Atualiza
        
        action = "atualizada" if context.user_data.get('is_update') else "incluída"
        
        await query.edit_message_text(
            f"✅ O.S. <code>{os_number}</code> {action} com sucesso no sistema!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Erro ao salvar OS {os_number}: {e}")
        await query.edit_message_text(
            f"❌ Erro ao salvar a O.S. {os_number}. Tente novamente ou contate o suporte. Erro: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ])
        )

    context.user_data.clear()
    return MENU

# --- Fluxo de Edição de Campo ---

def get_edit_keyboard(os_data: dict) -> InlineKeyboardMarkup:
    """Cria o teclado para edição de campos."""
    keys = list(os_data.keys())
    # Exclui campos de controle
    keys = [k for k in keys if k not in ['created_at', 'updated_at', 'Lembrete', 'Número da O.S.']]

    buttons = []
    current_row = []
    
    # Criar botões para cada campo
    for k in keys:
        if k == 'Técnico' and os_data.get('Técnico') == 'Não Definido':
            # Se Técnico Não Definido, dar opção de definir
            button = InlineKeyboardButton(f"👤 {k}: {os_data.get(k, 'N/A')}", callback_data="edit_Técnico_nao_definido")
        elif k in ['Criticidade', 'Tipo', 'Situação']:
            # Campos com botões, usam um callback especial
            button = InlineKeyboardButton(f"⚙️ {k}: {os_data.get(k, 'N/A')}", callback_data=f"edit_select_{k}")
        else:
            # Campos de texto/data simples
            button = InlineKeyboardButton(f"✏️ {k}: {os_data.get(k, 'N/A')}", callback_data=f"edit_field_{k}")
            
        current_row.append(button)
        if len(current_row) == 2:
            buttons.append(current_row)
            current_row = []
    
    if current_row:
        buttons.append(current_row)
        
    # Botões de controle
    buttons.append([InlineKeyboardButton("💾 Salvar Alterações", callback_data="confirm_save")])
    buttons.append([InlineKeyboardButton("❌ Cancelar Edição", callback_data="menu")])
    
    return InlineKeyboardMarkup(buttons)

async def start_edit_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o modo de edição a partir do resumo."""
    query = update.callback_query
    await query.answer()
    
    os_data = context.user_data.get('os_data')
    if not os_data:
        await query.edit_message_text("❌ Erro: Dados de OS não encontrados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]]))
        return MENU
        
    await query.edit_message_text(
        "✏️ <b>MODO DE EDIÇÃO ATIVO</b>\n\n"
        "Selecione o campo que deseja alterar:",
        reply_markup=get_edit_keyboard(os_data),
        parse_mode=ParseMode.HTML
    )
    return UPDATE_SELECTION # Novo estado para o modo de seleção

async def handle_edit_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manipula a seleção de campo para edição (Texto/Data)."""
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    
    action = data[0]
    field_name = data[1]
    
    # Define o campo que está sendo editado
    context.user_data['editing_field'] = field_name
    
    if action == 'edit' and field_name == 'Técnico':
        # Caso especial para re-definir o técnico
        return await handle_tecnico_selection(update, context) # Vai para o fluxo de Técnico
    
    if action == 'edit_select':
        # Edição de campos que usam botões (Criticidade, Tipo, Situação)
        context.user_data['editing_field'] = field_name
        
        if field_name == 'Criticidade':
            return await prompt_criticidade(update, context) # Reutiliza a função de prompt
        elif field_name == 'Tipo':
            return await prompt_tipo(update, context)
        elif field_name == 'Situação':
            return await prompt_situacao(update, context)
            
    elif action == 'edit_field':
        # Edição de campos de texto/data
        prompt_map = {
            'Chamado': "o novo Chamado",
            'Prefixo/Dependência': "o novo Prefixo/Dependência",
            'Distância': "a nova Distância",
            'Descrição': "a nova Descrição",
            'Prazo': "o novo Prazo (DD/MM/AAAA)",
            'Agendamento': "a nova Data de Agendamento (DD/MM/AAAA ou 'N/A')",
            # Adicione outros campos de texto aqui
        }
        
        prompt_text = prompt_map.get(field_name, f"o novo valor para o campo {field_name}")
        
        await query.edit_message_text(
            f"✏️ Digite {prompt_text}:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Voltar ao Resumo", callback_data="show_resumo")]
            ]),
            parse_mode=ParseMode.HTML
        )
        # O próximo handler (via MessageHandler) fará a validação e salvará o dado,
        # retornando para o show_resumo_inclusao.
        return PROMPT_UPDATE_FIELD


async def handle_update_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o input do campo em edição."""
    field_name = context.user_data.get('editing_field')
    text = update.message.text.strip()
    
    if not field_name:
        await update.message.reply_text("❌ Erro: Campo de edição não definido. Voltando ao resumo.")
        return await show_resumo_inclusao(update, context)

    # Validação especial para Datas
    if field_name in ['Prazo', 'Agendamento']:
        if field_name == 'Agendamento' and text.upper() == 'N/A':
             context.user_data['os_data']['Agendamento'] = 'Não Informado'
        else:
            try:
                date_dt = datetime.strptime(text, '%d/%m/%Y')
                context.user_data['os_data'][field_name] = date_dt
            except ValueError:
                await update.message.reply_text("❌ Formato de data inválido. Use DD/MM/AAAA ou 'N/A' (para Agendamento).")
                return PROMPT_UPDATE_FIELD
    else:
        context.user_data['os_data'][field_name] = text
        
    await update.message.reply_text(f"✅ Campo <b>{field_name}</b> atualizado!", parse_mode=ParseMode.HTML)
    del context.user_data['editing_field']
    return await show_resumo_inclusao(update, context) # Volta ao Resumo

# --- Fluxo de Atualização de O.S. (Entry Point) ---

async def start_atualizar_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da OS para atualização."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔄 <b>ATUALIZAÇÃO DE O.S.</b>\n\n"
        "Por favor, digite o <b>Número da O.S.</b> que deseja atualizar (apenas números).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_OS_UPDATE

async def prompt_os_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Número da OS e mostra o resumo para atualização."""
    os_number = update.message.text.strip()
    
    if not os_number.isdigit():
        await update.message.reply_text("❌ Por favor, digite apenas números para o Número da O.S.")
        return PROMPT_OS_UPDATE

    os_data = await fetch_os_by_number(os_number)
    
    if not os_data:
        await update.message.reply_text(
            f"❌ O.S. <code>{os_number}</code> não encontrada. Verifique o número e tente novamente.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
        return PROMPT_OS_UPDATE
    
    context.user_data['os_data'] = os_data
    context.user_data['is_update'] = True # Sinaliza que o fluxo é de update
    
    # Redireciona para o modo de edição
    return await start_edit_resumo(update, context)

# --- Fluxo de Deleção de O.S. ---

async def start_deletar_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da OS para deleção."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🗑️ <b>DELEÇÃO DE O.S.</b>\n\n"
        "Por favor, digite o <b>Número da O.S.</b> que deseja excluir (apenas números).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_OS_DELETE

async def prompt_os_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Número da OS e solicita confirmação de deleção."""
    os_number = update.message.text.strip()
    
    if not os_number.isdigit():
        await update.message.reply_text("❌ Por favor, digite apenas números para o Número da O.S.")
        return PROMPT_OS_DELETE

    os_data = await fetch_os_by_number(os_number)
    
    if not os_data:
        await update.message.reply_text(
            f"❌ O.S. <code>{os_number}</code> não encontrada. Verifique o número e tente novamente.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
        return PROMPT_OS_DELETE
    
    context.user_data['os_data'] = os_data
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar exclusão", callback_data=f"confirm_delete_{os_number}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
    ]
    
    await update.message.reply_text(
        f"⚠️ Você tem certeza que deseja <b>EXCLUIR</b> esta O.S.?\n\n{format_os_data(os_data)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return CONFIRM_DELETE

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Deleta a OS confirmada do Firestore."""
    query = update.callback_query
    await query.answer()
    
    os_number = context.user_data['os_data']['Número da O.S.']
    
    try:
        os_ref = get_os_ref(os_number)
        await asyncio.to_thread(os_ref.delete)
        
        await query.edit_message_text(
            f"✅ O.S. <code>{os_number}</code> excluída com sucesso.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Erro ao excluir OS {os_number}: {e}")
        await query.edit_message_text(
            f"❌ Erro ao excluir a O.S. {os_number}. Tente novamente. Erro: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ])
        )

    context.user_data.clear()
    return MENU

# --- Fluxo de Listagem de O.S. ---

async def start_listar_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de listagem, pedindo o Tipo."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔧 Corretiva", callback_data="list_tipo_Corretiva")],
        [InlineKeyboardButton("🧹 Preventiva", callback_data="list_tipo_Preventiva")],
        [InlineKeyboardButton("✅ Todas", callback_data="list_tipo_Todas")],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")],
    ]
    
    await query.edit_message_text(
        "📋 <b>LISTAGEM DE O.S.</b>\n\n"
        "Selecione o <b>Tipo</b> de O.S. que deseja listar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return LISTAR_TIPO

async def prompt_listar_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Tipo e solicita a Situação para listar."""
    query = update.callback_query
    await query.answer()
    tipo = query.data.split('_')[2]
    
    context.user_data['list_tipo'] = tipo
    
    keyboard = [
        [InlineKeyboardButton("🔴 Pendente", callback_data="list_situacao_Pendente")],
        [InlineKeyboardButton("🟡 Aguardando Agendamento", callback_data="list_situacao_Aguardando Agendamento")],
        [InlineKeyboardButton("🔵 Agendado", callback_data="list_situacao_Agendado")],
        [InlineKeyboardButton("🟢 Concluído", callback_data="list_situacao_Concluído")],
        [InlineKeyboardButton("✅ Todas", callback_data="list_situacao_Todas")],
        [InlineKeyboardButton("↩️ Etapa Anterior", callback_data="list_os")],
    ]
    
    await query.edit_message_text(
        f"✅ Tipo <b>{tipo}</b> selecionado.\n\n"
        "Selecione a <b>Situação</b> das O.S. que deseja listar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return LISTAR_SITUACAO

async def execute_listagem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executa a consulta no Firestore e exibe os resultados."""
    query = update.callback_query
    await query.answer()
    situacao = query.data.split('_')[2]
    tipo = context.user_data['list_tipo']
    
    try:
        os_collection = db.collection("ordens_servico")
        q = os_collection.order_by('Número da O.S.')
        
        # Filtro por Tipo
        if tipo != "Todas":
            q = q.where("Tipo", "==", tipo)
            
        # Filtro por Situação
        if situacao != "Todas":
            q = q.where("Situação", "==", situacao)
        
        # O Firestore não suporta queries complexas de `where` seguido de `orderBy`
        # sem um índice composto. Para simplificar, faremos a ordenação in-memory
        # se houver filtros. Caso contrário, apenas fetch e format.
        
        docs = await asyncio.to_thread(q.get)
        results = [doc.to_dict() for doc in docs]
        
        if not results:
            message = (f"🔍 Não foram encontradas O.S. do Tipo <b>{tipo}</b> "
                       f"na Situação <b>{situacao}</b>.")
        else:
            # Ordenação final in-memory (se necessário, o Firestore já ordenou por Número)
            
            list_items = []
            for os_item in results:
                 list_items.append(
                    f"• OS <code>{os_item.get('Número da O.S.')}</code>: "
                    f"Tipo {os_item.get('Tipo')}, Situação <b>{os_item.get('Situação')}</b>. "
                    f"Prazo: {os_item.get('Prazo', 'N/A')}"
                )
                
            message = (
                f"✅ <b>Resultado da Listagem ({len(results)} O.S.):</b>\n"
                f"<i>Tipo: {tipo} | Situação: {situacao}</i>\n\n"
                f"{'\n'.join(list_items)}"
            )
            
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Erro ao listar OS: {e}")
        await query.edit_message_text(
            f"❌ Erro ao executar a listagem. Tente novamente.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ])
        )
        
    context.user_data.clear()
    return MENU

# --- Fluxo de Processamento de PDF ---

# Funções de extração de PDF adaptadas para usar bytes em memória
def limpar_valor_bruto(v):
    if v is None: return None
    v = v.strip()
    # Adiciona a lógica de limpeza do seu código original
    if re.fullmatch(r'[\(\-\s]*\)?', v) or v in ('()', '-', '—', ''):
        return None
    return v

def tratar_texto(valor, linha_unica=False):
    """Normaliza textos, especialmente campo Descrição."""
    if not valor: return None
    valor = valor.replace('\r', '\n').strip()
    if '\n' in valor:
        partes = re.split(r'\n+', valor)
    else:
        partes = re.split(r'(?<=[.;:])\s+', valor)
    partes = [re.sub(r'\s+', ' ', p).strip() for p in partes if p and p.strip()]
    if not partes: return None
    if linha_unica: return " ".join(partes)
    return "\n\n".join(partes)

def extrair_dados_pdf_bytes(pdf_bytes: bytes) -> dict:
    """Extrai dados de OS de um PDF em formato de bytes (PyMuPDF)."""
    if not PDF_PROCESSOR_AVAILABLE:
        logger.error("PyMuPDF (fitz) não disponível para extração.")
        return {"Número da O.S.": None}

    try:
        # Usa PyMuPDF para abrir o arquivo em memória
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texto = "".join(pagina.get_text("text") for pagina in doc)
        doc.close()

        dados = {}
        padroes = {
            "Número da O.S.": r"Número da O\.S\.\s*([\d]+)",
            "Chamado": r"Chamado:\s*([A-Z0-9\-]+)",
            # Adaptação dos padrões para capturar Prefix/Dep de forma mais robusta
            "Prefixo/Dependência": r"Dependência:\s*(.+?)(?=\s*Endereço:)",
            "Distância": r"Distância:\s*(.+?)(?=\s*Ambiente:)",
            "Descrição": r"Descrição:\s*(.+?)(?=\s*(?:Sinistro:|Criticidade:|Tipo:|$))",
            "Criticidade": r"Criticidade:\s*(.+?)(?=\s*(?:Tipo:|Prazo:|Solicitante:|$))",
            "Tipo": r"Tipo:\s*(.+?)(?=\s*(?:Prazo:|Solicitante:|Matrícula:|$))",
            "Prazo": r"Prazo:\s*(.+?)(?=\s*(?:Solicitante:|Matrícula:|Telefone:|$))"
        }
        
        campos_tratamento = {"descrição", "criticidade", "tipo", "prazo", "solicitante"}

        for campo, regex in padroes.items():
            m = re.search(regex, texto, re.DOTALL | re.IGNORECASE) # Ignora Case para robustez
            if not m:
                dados[campo] = None
                continue
            
            valor = m.group(1).strip()
            valor = limpar_valor_bruto(valor)

            if campo == "Número da O.S." and valor is not None:
                valor = str(valor) # Garante que seja string para usar como ID do documento
            elif campo.lower() == "descrição" and valor is not None:
                valor = tratar_texto(valor, linha_unica=True) 
            elif campo.lower() in campos_tratamento and valor is not None:
                valor = tratar_texto(valor)

            dados[campo] = valor
            
        # Adiciona Situação e Técnico padrão para a nova OS
        if 'Situação' not in dados or not dados['Situação']:
            dados['Situação'] = 'Pendente'
        if 'Técnico' not in dados or not dados['Técnico']:
             dados['Técnico'] = 'Não Definido'
        
        return dados
    except Exception as e:
        logger.error(f"Erro durante a extração do PDF: {e}")
        return {"Número da O.S.": None}


async def start_enviar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prepara o bot para receber o arquivo PDF."""
    query = update.callback_query
    await query.answer()

    if not PDF_PROCESSOR_AVAILABLE:
        await query.edit_message_text(
            "❌ <b>RECURSO INDISPONÍVEL:</b>\n\n"
            "O módulo de processamento de PDF (`fitz` / PyMuPDF) não está disponível neste ambiente. "
            "Por favor, instale as dependências necessárias para usar este recurso.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]])
        )
        return MENU
        
    await query.edit_message_text(
        "📄 <b>ENVIO DE PDF PARA INCLUSÃO DE OS</b>\n\n"
        "Por favor, envie o arquivo PDF da Ordem de Serviço. "
        "Irei extrair automaticamente as informações e salvar no sistema.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROCESSAR_PDF

async def processar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o PDF, extrai dados e salva/atualiza a OS."""
    
    if not update.message.document or update.message.document.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Por favor, envie um <b>arquivo PDF</b> válido.", parse_mode=ParseMode.HTML)
        return PROCESSAR_PDF
        
    document = update.message.document
    
    await update.message.reply_text("⏳ Recebido! Processando o arquivo...")

    try:
        # 1. Baixar o arquivo em memória
        file_id = document.file_id
        file = await context.bot.get_file(file_id)
        
        # Faz o download do arquivo para um objeto BytesIO
        pdf_file_bytes = await file.download_as_bytes()
        
        # 2. Extrair dados
        dados_os = extrair_dados_pdf_bytes(pdf_file_bytes)
        os_number = dados_os.get("Número da O.S.")
        
        if not os_number:
            await update.message.reply_text("❌ Não foi possível extrair o <b>Número da O.S.</b> do PDF. Verifique o formato do documento.", parse_mode=ParseMode.HTML)
            return PROCESSAR_PDF
            
        # 3. Salvar/Atualizar no Firestore
        os_ref = get_os_ref(os_number)
        
        # Verifica se já existe
        doc = await asyncio.to_thread(os_ref.get)
        
        # Limpa o Lembrete do modelo (se houver) antes de salvar
        if 'Lembrete' in dados_os: del dados_os['Lembrete']
        
        if doc.exists:
            # Atualiza
            os_data = doc.to_dict()
            os_data.update(dados_os) # Mescla com os novos dados
            os_data['updated_at'] = datetime.now()
            
            await asyncio.to_thread(os_ref.set, os_data)
            action = "atualizada"
        else:
            # Novo
            dados_os['created_at'] = datetime.now()
            dados_os['updated_at'] = datetime.now()
            dados_os['Número da O.S.'] = os_number
            await asyncio.to_thread(os_ref.set, dados_os)
            action = "incluída"

        await update.message.reply_text(
            f"✅ O.S. <code>{os_number}</code> {action} com sucesso via PDF!\n\n"
            f"{format_os_data(dados_os)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Erro ao processar PDF: {e}")
        await update.message.reply_text(
            f"❌ Erro interno ao processar o PDF. Erro: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ])
        )

    context.user_data.clear()
    return MENU


# --- Fluxo de Lembretes (Básico) ---
async def start_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu de gestão de Lembretes/Alertas."""
    query = update.callback_query
    await query.answer()

    # O usuário pode querer gerenciar alertas automáticos (Job) ou alertas manuais
    keyboard = [
        [InlineKeyboardButton("⏰ Criar Lembrete Manual", callback_data="lembrete_manual_start")],
        # [InlineKeyboardButton("⚙️ Configurar Alertas Automáticos (Futuro)", callback_data="lembrete_auto_config")],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")],
    ]
    
    await query.edit_message_text(
        "🔔 <b>GERENCIAMENTO DE LEMBRETES</b>\n\n"
        "Você pode criar lembretes personalizados para uma O.S. específica.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return LEMBRETE_MENU

async def start_lembrete_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da OS para criar o lembrete."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "✍️ Para qual <b>Número da O.S.</b> você deseja criar um lembrete?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da OS e solicita data/hora."""
    os_number = update.message.text.strip()
    
    if not os_number.isdigit():
        await update.message.reply_text("❌ Por favor, digite apenas números para o Número da O.S.")
        return PROMPT_ID_LEMBRETE

    os_data = await fetch_os_by_number(os_number)
    
    if not os_data:
        await update.message.reply_text(
            f"❌ O.S. <code>{os_number}</code> não encontrada.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
        return PROMPT_ID_LEMBRETE
    
    context.user_data['os_data'] = os_data
    
    await update.message.reply_text(
        "⏰ Digite a <b>Data e Hora</b> do lembrete (Formato: DD/MM/AAAA HH:MM):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="lembrete_manual_start")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_LEMBRETE_DATA

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora e solicita a mensagem."""
    date_time_str = update.message.text.strip()
    
    try:
        # Tenta parsear para datetime
        lembrete_dt = datetime.strptime(date_time_str, '%d/%m/%Y %H:%M')
        if lembrete_dt < datetime.now():
            await update.message.reply_text("❌ A data/hora do lembrete deve ser no futuro.")
            return PROMPT_LEMBRETE_DATA
            
        context.user_data['lembrete_dt'] = lembrete_dt
    except ValueError:
        await update.message.reply_text("❌ Formato de data/hora inválido. Use DD/MM/AAAA HH:MM (ex: 25/10/2025 10:30).")
        return PROMPT_LEMBRETE_DATA
        
    await update.message.reply_text(
        "📝 Digite a <b>mensagem personalizada</b> para este lembrete:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Etapa Anterior", callback_data="lembrete_manual_start")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return PROMPT_LEMBRETE_MSG

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o lembrete manual e agenda o job."""
    mensagem = update.message.text.strip()
    os_data = context.user_data['os_data']
    lembrete_dt = context.user_data['lembrete_dt']
    
    try:
        # Salva o lembrete em uma coleção separada
        lembrete_doc = {
            'os_number': os_data['Número da O.S.'],
            'user_id': update.effective_user.id,
            'chat_id': update.effective_chat.id,
            'message': f"🔔 <b>Lembrete OS {os_data['Número da O.S.']}</b>: {mensagem}",
            'run_time': lembrete_dt,
            'created_at': datetime.now(),
            'status': 'Pendente'
        }
        
        await asyncio.to_thread(db.collection("lembretes_manuais").add, lembrete_doc)
        
        # Agenda o job no Job Queue do Telegram
        context.job_queue.run_once(
            send_manual_alert_job, 
            when=lembrete_dt,
            data=lembrete_doc,
            name=f"manual_{os_data['Número da O.S.']}_{uuid.uuid4().hex[:6]}"
        )
        
        await update.message.reply_text(
            f"✅ Lembrete agendado com sucesso para <b>{lembrete_dt.strftime('%d/%m/%Y às %H:%M')}</b>!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Erro ao salvar/agendar lembrete: {e}")
        await update.message.reply_text(
            f"❌ Erro ao agendar o lembrete. Erro: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
            ])
        )

    context.user_data.clear()
    return MENU

async def send_manual_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que envia o alerta manual."""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    message = job_data['message']
    
    try:
        await context.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
        # Tenta atualizar o status no Firestore (assumindo que o doc_ref pode ser reconstruído ou passado)
        # Simplificando: o job foi executado.
        logger.info(f"Lembrete manual enviado para chat {chat_id}.")
    except Exception as e:
        logger.error(f"Erro ao enviar alerta manual: {e}")

# --- Job de Alerta Automático de Prazo ---

async def check_automatic_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifica OS com prazo de 1 ou 2 dias e vencidas (exceto Concluídas)."""
    
    if not db: 
        logger.warning("Firestore não disponível para checagem de alertas automáticos.")
        return

    # Definir as datas de corte
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    day_after_tomorrow = today + timedelta(days=2)
    
    alert_messages = []

    try:
        # Busca todas as OS que não estejam "Concluído"
        os_collection = db.collection("ordens_servico")
        
        # A API do Firestore não permite buscar por "not in" ou "less than date" 
        # sem índices complexos ou se a condição de Situação estiver no mesmo campo.
        # Estratégia: Buscamos as que não são "Concluído" (se for possível configurar o índice)
        # OU buscamos todas e filtramos in-memory. Devido à limitação do ambiente, vamos buscar
        # o que for viável e filtrar o restante.
        
        docs = await asyncio.to_thread(os_collection.get)
        
        # Buscar todos os users únicos que precisam ser notificados (para evitar spam)
        # Neste modelo, o alerta é enviado para um chat ID específico. 
        # Vou usar um chat ID fixo (ex: o do desenvolvedor/admin) ou o chat ID salvo em context (se fosse um bot multi-usuário)
        # Como não temos um chat ID de administração, vou pular o envio e apenas logar.
        
        # ASSUMINDO que o chat ID de notificação é passado no context.job.data
        notification_chat_id = context.job.data.get('notification_chat_id')
        if not notification_chat_id:
            logger.warning("Chat ID de notificação não definido para alertas automáticos.")
            return

        for doc in docs:
            os_data = doc.to_dict()
            situacao = os_data.get('Situação')
            prazo = os_data.get('Prazo')
            
            if situacao == 'Concluído':
                continue

            # Converter Prazo para datetime (se for timestamp do Firestore)
            if prazo and hasattr(prazo, 'replace'):
                prazo_dt = prazo.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                 continue # Pula se Prazo não for um objeto de data válido
            
            os_number = os_data.get('Número da O.S.')
            
            # Checagem de Alertas
            alert_type = None
            if prazo_dt < today:
                alert_type = "🔴 VENCIDA"
            elif prazo_dt == tomorrow:
                alert_type = "⚠️ VENCE AMANHÃ"
            elif prazo_dt == day_after_tomorrow:
                alert_type = "🟡 VENCE EM 2 DIAS"
                
            if alert_type:
                alert_messages.append(
                    f"{alert_type} | OS <code>{os_number}</code> ({os_data.get('Tipo')}) | "
                    f"Prazo: {prazo_dt.strftime('%d/%m/%Y')} | Situação: {situacao}"
                )

        if alert_messages:
            final_message = "🚨 <b>ALERTAS DE O.S. - " + today.strftime('%d/%m/%Y') + "</b> 🚨\n\n"
            final_message += "\n".join(alert_messages)
            
            # Enviar a mensagem para o chat de notificação
            await context.bot.send_message(notification_chat_id, final_message, parse_mode=ParseMode.HTML)
            logger.info(f"Enviado {len(alert_messages)} alertas automáticos para chat {notification_chat_id}")
        
    except Exception as e:
        logger.error(f"Erro no job de alerta automático: {e}")

# --- Ajuda Geral ---

async def ajuda_geral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe o menu de ajuda."""
    query = update.callback_query
    await query.answer()

    help_text = (
        "❓ <b>AJUDA GERAL DO BOT DE GESTÃO DE O.S.</b>\n\n"
        "Este bot ajuda você a gerenciar Ordens de Serviço (O.S.).\n\n"
        "<b>/start</b>: Volta ao Menu Principal.\n\n"
        "<b>📝 Incluir O.S.</b>: Inicia um formulário passo a passo para cadastrar uma nova O.S.\n"
        "<b>🔄 Atualizar O.S.</b>: Permite buscar uma O.S. pelo número e editar qualquer campo.\n"
        "<b>🗑️ Deletar O.S.</b>: Exclui permanentemente uma O.S. do sistema após confirmação.\n"
        "<b>📋 Listar O.S.</b>: Filtra e exibe O.S. por Tipo (Corretiva/Preventiva) e Situação.\n"
        "<b>📄 Enviar PDF</b>: Processa o PDF da OS, extrai dados (Número, Chamado, etc.) e salva/atualiza automaticamente.\n"
        "<b>🔔 Lembrete</b>: Cria alertas manuais ou gerencia o sistema de alertas automáticos.\n"
        "<b>❌ Cancelar</b>: Cancela o fluxo de conversação atual."
    )
    
    await query.edit_message_text(
        help_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="menu")]
        ]),
        parse_mode=ParseMode.HTML
    )
    return AJUDA_GERAL


# --- Handlers de navegação ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manipula todos os callbacks de navegação e botões."""
    query = update.callback_query
    data = query.data
    
    if data == "menu":
        return await start(update, context)

    # --- Fluxo de Inclusão/Update ---
    if data == "incluir_os":
        return await start_incluir_os(update, context)
    if data.startswith("criticidade_"):
        return await prompt_tipo(update, context)
    if data.startswith("tipo_"):
        return await prompt_prazo(update, context)
    if data.startswith("situacao_"):
        return await prompt_tecnico(update, context)
    if data.startswith("tecnico_"):
        return await handle_tecnico_selection(update, context)
    
    # Resumo / Confirmação
    if data == "edit_resumo":
        return await start_edit_resumo(update, context)
    if data.startswith("edit_field_") or data.startswith("edit_select_"):
        return await handle_edit_selection(update, context)
    if data == "confirm_save":
        return await save_os_to_firestore(update, context)
    if data == "show_resumo":
        return await show_resumo_inclusao(update, context)
        
    # --- Fluxo de Atualização ---
    if data == "atualizar_os":
        return await start_atualizar_os(update, context)
    if data.startswith("update_existing_"): # Callback de "Sim, Atualizar"
        os_number = data.split('_')[-1]
        os_data = context.user_data.get('os_data')
        if not os_data or os_data.get('Número da O.S.') != os_number:
            os_data = await fetch_os_by_number(os_number)
            context.user_data['os_data'] = os_data
        
        context.user_data['is_update'] = True
        return await start_edit_resumo(update, context)
        
    # --- Fluxo de Deleção ---
    if data == "deletar_os":
        return await start_deletar_os(update, context)
    if data.startswith("confirm_delete_"):
        return await confirm_delete(update, context)

    # --- Fluxo de Listagem ---
    if data == "listar_os":
        return await start_listar_os(update, context)
    if data.startswith("list_tipo_"):
        return await prompt_listar_situacao(update, context)
    if data.startswith("list_situacao_"):
        return await execute_listagem(update, context)

    # --- Fluxo de PDF ---
    if data == "enviar_pdf":
        return await start_enviar_pdf(update, context)

    # --- Fluxo de Lembrete ---
    if data == "lembrete_menu":
        return await start_lembrete(update, context)
    if data == "lembrete_manual_start":
        return await start_lembrete_manual(update, context)

    # --- Ajuda ---
    if data == "ajuda_geral":
        return await ajuda_geral(update, context)
        
    # --- Navegação de Etapa Anterior (Back) ---
    if data.startswith("back_"):
        await query.answer()
        # Mapeamento reverso dos estados para voltar
        target_state_name = data.split('_')[1]
        
        # Simplesmente reinicia o fluxo de inclusão a partir do ponto inicial
        # Complexo de reverter o state, vamos para o menu.
        await query.edit_message_text(
            "↩️ Voltando ao Menu Principal para reiniciar o fluxo de inclusão.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
        context.user_data.clear()
        return MENU # Simplifica a navegação de "voltar" para o menu principal

    # Fallback
    await query.answer("Opção não reconhecida.")
    return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde a comandos que não são reconhecidos pelo bot."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "Comando não reconhecido. Por favor, use as opções do menu ou digite /start para recomeçar."
        )

# --- Main ---

def main() -> None:
    """Inicia o bot usando Webhooks."""
    if not TOKEN:
        logger.error("Token do Telegram não encontrado. Verifique a variável TELEGRAM_TOKEN.")
        return

    application = Application.builder().token(TOKEN).build()
    job_queue = application.job_queue
    
    # 1. Configura o Job Queue para auto-ping (Manutenção)
    job_queue.run_repeating(
        ping_self_job, 
        interval=PING_INTERVAL_SECONDS, 
        first=60, 
        name="self_ping_job"
    )
    logger.info(f"Auto-ping agendado a cada {PING_INTERVAL_SECONDS} segundos.")
    
    # 2. Configura o Job para Alerta Automático de Prazo (Roda diariamente às 9h)
    # ATENÇÃO: É necessário ter um chat ID de administração/notificação para isso funcionar
    # Aqui, usamos um placeholder de chat ID que você DEVE substituir.
    NOTIFICATION_CHAT_ID = os.environ.get("NOTIFICATION_CHAT_ID", "SEU_CHAT_ID_DE_ADMIN_AQUI")
    
    if NOTIFICATION_CHAT_ID != "SEU_CHAT_ID_DE_ADMIN_AQUI":
        job_queue.run_daily(
            check_automatic_alerts_job,
            time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc), # 9h UTC (ajuste para seu fuso)
            data={'notification_chat_id': NOTIFICATION_CHAT_ID},
            name="automatic_alert_check"
        )
        logger.info(f"Alerta automático agendado diariamente às 9h UTC para o chat {NOTIFICATION_CHAT_ID}.")
    else:
        logger.warning("Variável NOTIFICATION_CHAT_ID não definida. Alertas automáticos desativados.")

    # 3. Configuração do ConversationHandler (Com novos estados)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^incluir_os$|^atualizar_os$|^deletar_os$|^listar_os$|^enviar_pdf$|^lembrete_menu$|^ajuda_geral$'),
                CallbackQueryHandler(callback_handler, pattern='^update_existing_'), # Para OS duplicada
            ],
            PROMPT_OS_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_os_id),
            ],
            PROMPT_CHAMADO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_chamado),
            ],
            PROMPT_PREFIXO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_distancia),
            ],
            PROMPT_DISTANCIA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_descricao),
            ],
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_criticidade),
            ],
            PROMPT_CRITICIDADE: [
                CallbackQueryHandler(prompt_tipo, pattern='^criticidade_'),
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(prompt_prazo, pattern='^tipo_'),
            ],
            PROMPT_PRAZO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_situacao),
            ],
            PROMPT_SITUACAO: [
                CallbackQueryHandler(prompt_tecnico, pattern='^situacao_'),
            ],
            PROMPT_TECNICO: [
                CallbackQueryHandler(handle_tecnico_selection, pattern='^tecnico_'),
            ],
            PROMPT_TECNICO_NOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_agendamento),
            ],
            RESUMO_INCLUSAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_resumo_inclusao), # Captura Agendamento
                CallbackQueryHandler(callback_handler, pattern='^edit_resumo$|^confirm_save$|^cancel$'),
            ],
            
            # Fluxo de Atualização (Entry point e Seleção de Campo)
            PROMPT_OS_UPDATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_os_update),
            ],
            UPDATE_SELECTION: [
                CallbackQueryHandler(handle_edit_selection, pattern='^edit_field_|^edit_select_|^edit_Técnico_|^confirm_save$|^show_resumo$'),
            ],
            PROMPT_UPDATE_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update_field_input),
            ],
            
            # Fluxo de Deleção
            PROMPT_OS_DELETE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_os_delete),
            ],
            CONFIRM_DELETE: [
                CallbackQueryHandler(confirm_delete, pattern='^confirm_delete_|^menu$'),
            ],
            
            # Fluxo de Listagem
            LISTAR_TIPO: [
                CallbackQueryHandler(prompt_listar_situacao, pattern='^list_tipo_'),
            ],
            LISTAR_SITUACAO: [
                CallbackQueryHandler(execute_listagem, pattern='^list_situacao_'),
            ],
            
            # Fluxo de PDF
            PROCESSAR_PDF: [
                MessageHandler(filters.Document.PDF, processar_pdf),
            ],
            
            # Fluxo de Lembrete
            LEMBRETE_MENU: [
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$'),
            ],
            PROMPT_ID_LEMBRETE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data),
            ],
            PROMPT_LEMBRETE_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
            ],
            PROMPT_LEMBRETE_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(callback_handler, pattern='^menu$'), # Voltar ao menu
            MessageHandler(filters.COMMAND, fallback_command), # Comandos não reconhecidos
        ],
    )

    # Adiciona o ConversationHandler
    application.add_handler(conv_handler)
    
    # 4. Configuração do Webhook
    try:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN, 
            webhook_url=WEBHOOK_URL + WEBHOOK_PATH, 
        )
        logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
        logger.info(f"Webhook URL configurada no Telegram: {WEBHOOK_URL + WEBHOOK_PATH}")
    except Exception as e:
        logger.error(f"Erro ao iniciar o webhook: {e}")


if __name__ == "__main__":
    main()
