import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import cloudscraper
import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL

# Estados utilizados pelo ConversationHandler do /config
CHOOSING = 1

CONFIG_KEYS = {
    "youtube_channel_url": "URL ou ID do canal principal do YouTube",
    "tiktok_username": "Username do TikTok sem @",
}

def load_env_file(path: Path) -> None:
    """Carrega pares KEY=VALUE de um arquivo `.env` sem sobrescrever variáveis já definidas."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


class ConfigManager:
    """Gerencia a leitura e escrita do arquivo config.json."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Arquivo de configuração '{self.path}' não encontrado. "
                "Crie um config.json baseado no config.json.example."
            )
        with self.path.open("r", encoding="utf-8") as fp:
            return json.load(fp)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def get_profile_value(self, key: str) -> Optional[str]:
        profiles = self.data.get("profiles", {})
        return profiles.get(key)

    async def update_profile_value(self, key: str, value: str) -> None:
        if key not in CONFIG_KEYS:
            raise KeyError(f"Chave desconhecida: {key}")
        async with self._lock:
            profiles = self.data.setdefault("profiles", {})
            profiles[key] = value
            await asyncio.to_thread(self._write)

    def _write(self) -> None:
        with self.path.open("w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2)


class TemporaryBlockError(RuntimeError):
    """Erro usado para identificar bloqueios temporários, CAPTCHAs etc."""




def format_number(value: Optional[float]) -> str:
    if value is None:
        return "indisponível"
    return f"{value:,.0f}".replace(",", ".")


class PlatformScraper:
    """Camada responsável por buscar as métricas em cada plataforma."""

    TOKCOUNT_BASE = "https://tiktok.tokcount.com"
    TOKCOUNT_ORIGIN = "https://tokcount.com"
    TOKCOUNT_REFERER = "https://tokcount.com/"
    TOKCOUNT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    TOKCOUNT_ANTIABUSE_IP = "1.1.1.1"

    def __init__(self, config: ConfigManager):
        self.config = config
        self.http = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self._tokcount_user_agent = (
            self.http.headers.get("User-Agent") or self.TOKCOUNT_UA
        )

    async def fetch_youtube_livecounts(self) -> Dict[str, Any]:
        url = self.config.get_profile_value("youtube_channel_url")
        if not url:
            return {"status": "not_configured"}
        return await asyncio.to_thread(self._youtube_stats, url)

    async def fetch_tiktok(self) -> Dict[str, Any]:
        username = self.config.get_profile_value("tiktok_username")
        if not username:
            return {"status": "not_configured"}
        return await asyncio.to_thread(self._tiktok_stats, username)
        # --- Implementacoes sincronas usadas dentro do executor ---

    def _resolve_channel_id(self, url: str) -> Optional[str]:
        candidate = url.strip()
        if candidate.startswith("UC") and len(candidate) >= 24:
            return candidate
        parsed = urlparse(candidate)
        path_segments = [seg for seg in parsed.path.split("/") if seg]
        for segment in reversed(path_segments):
            if segment.startswith("UC") and len(segment) >= 24:
                return segment
        try:
            opts = {"quiet": True, "skip_download": True, "no_warnings": True}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return (
                info.get("channel_id")
                or info.get("channel")
                or info.get("uploader_id")
                or info.get("uploader")
            )
        except Exception:
            return None

    def _youtube_stats(self, url: str) -> Dict[str, Any]:
        logging.info("Atualizando YouTube (scraping): %s", url)
        channel_id = self._resolve_channel_id(url)
        if not channel_id:
            return {"status": "error", "message": "ID do canal do YouTube nao identificado."}
        page_url = f"https://www.youtube.com/channel/{channel_id}/about"
        try:
            response = self.http.get(page_url, timeout=30)
        except Exception as exc:
            logging.exception("Erro buscando pagina do YouTube")
            return {"status": "error", "message": str(exc)}
        if response.status_code != 200:
            return {
                "status": "error",
                "message": f"YouTube respondeu com status {response.status_code}.",
            }
        try:
            payload = self._extract_yt_initial_data(response.text)
        except Exception as exc:
            logging.exception("Erro parseando dados do YouTube")
            return {"status": "error", "message": str(exc)}
        about = self._find_renderer(payload, "aboutChannelRenderer")
        if not about:
            return {"status": "error", "message": "Dados do canal nao encontrados."}
        view_model = about.get("metadata", {}).get("aboutChannelViewModel", {})
        identifier = (
            view_model.get("displayCanonicalChannelUrl")
            or view_model.get("canonicalChannelUrl")
            or channel_id
        )
        return {
            "status": "ok",
            "identifier": identifier,
            "followers": self._parse_count(view_model.get("subscriberCountText")),
            "videos": self._parse_count(view_model.get("videoCountText")),
            "views": self._parse_count(view_model.get("viewCountText")),
        }

    def _extract_yt_initial_data(self, text: str) -> Dict[str, Any]:
        match = re.search(r"var ytInitialData = (\{.*?\});", text, re.S)
        payload: Optional[str] = None
        if match:
            payload = match.group(1)
        else:
            match = re.search(r"var ytInitialData = '(.*?)';", text, re.S)
            if match:
                escaped = match.group(1)
                payload = bytes(escaped, "utf-8").decode("unicode_escape")
        if not payload:
            raise RuntimeError("ytInitialData nao encontrado.")
        return json.loads(payload)

    def _find_renderer(self, obj: Any, key: str) -> Optional[Dict[str, Any]]:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                result = self._find_renderer(value, key)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._find_renderer(item, key)
                if result:
                    return result
        return None

    def _parse_count(self, text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        candidate = text.split()[0]
        candidate = candidate.replace("\u00a0", "").replace(" ", "")
        candidate = candidate.replace(",", "")
        multiplier = 1
        suffix = ""
        match = re.match(r"(?P<number>[\d.]+)(?P<suffix>[KMkMbB]?)", candidate)
        if match:
            suffix = match.group("suffix").upper()
            number = float(match.group("number"))
            multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
            return int(number * multiplier)
        digits = "".join(ch for ch in candidate if ch.isdigit())
        return int(digits) if digits else None

    def _tokcount_headers(self, include_identity: bool = False) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        sha256_digest = hashlib.sha256((timestamp + "64").encode("utf-8")).digest()
        headers = {
            "Origin": self.TOKCOUNT_ORIGIN,
            "Referer": self.TOKCOUNT_REFERER,
            "User-Agent": self._tokcount_user_agent,
            "x-midas": hashlib.sha384(sha256_digest).hexdigest(),
            "x-ajay": hashlib.new("ripemd160", timestamp.encode("utf-8")).hexdigest(),
            "x-catto": timestamp,
        }
        if include_identity:
            headers["x-service"] = "TokCount"
            headers["x-user-agent"] = self._tokcount_user_agent
            headers["x-antiabuse-ip"] = self.TOKCOUNT_ANTIABUSE_IP
        return headers

    def _tokcount_get(self, path: str, include_identity: bool = False) -> Dict[str, Any]:
        url = f"{self.TOKCOUNT_BASE}{path}"
        headers = self._tokcount_headers(include_identity=include_identity)
        response = self.http.get(url, headers=headers, timeout=30)
        if response.status_code in (401, 403, 429):
            raise TemporaryBlockError("TokCount bloqueou o acesso.")
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("TokCount retornou resposta inválida.") from exc
        if not payload.get("success"):
            message = payload.get("message") or "TokCount retornou erro."
            if payload.get("challenge"):
                raise TemporaryBlockError(message)
            raise RuntimeError(message)
        return payload

    def _normalize_tiktok_stat(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return int(digits) if digits else None

    def _parse_tiktok_web_stats(self, html: str) -> Tuple[str, Dict[str, Optional[int]]]:
        match = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(?P<data>{.*?})</script>',
            html,
            re.S,
        )
        if not match:
            raise RuntimeError("TokCount falhou e os dados da página do TikTok não foram encontrados.")
        try:
            data = json.loads(match.group("data"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Erro parseando os dados do TikTok.") from exc
        scope = data.get("__DEFAULT_SCOPE__", {})
        user_detail = scope.get("webapp.user-detail")
        if not user_detail:
            raise RuntimeError("TikTok não retornou os dados esperados (webapp.user-detail ausente).")
        user_info = user_detail.get("userInfo", {})
        stats = user_info.get("stats")
        if not stats:
            raise RuntimeError("TikTok não retornou as estatísticas do usuário.")
        user = user_info.get("user", {})
        identifier = user.get("uniqueId") or ""
        parsed_stats = {
            "followers": self._normalize_tiktok_stat(stats.get("followerCount")),
            "likes": self._normalize_tiktok_stat(
                stats.get("heartCount") or stats.get("heart")
            ),
            "following": self._normalize_tiktok_stat(stats.get("followingCount")),
            "videos": self._normalize_tiktok_stat(stats.get("videoCount")),
        }
        return identifier, parsed_stats

    def _tiktok_stats_page(self, username: str) -> Dict[str, Any]:
        logging.info("Atualizando TikTok (fallback web) para @%s", username)
        url = f"https://www.tiktok.com/@{username}"
        response = self.http.get(url, timeout=30, headers={"Referer": "https://www.tiktok.com/"})
        if response.status_code != 200:
            raise RuntimeError(f"TikTok respondeu com status {response.status_code}.")
        identifier, parsed_stats = self._parse_tiktok_web_stats(response.text)
        return {
            "status": "ok",
            "identifier": f"@{identifier or username}",
            "followers": parsed_stats["followers"],
            "likes": parsed_stats["likes"],
            "following": parsed_stats["following"],
            "videos": parsed_stats["videos"],
        }

    def _tokcount_stats(self, username: str) -> Dict[str, Any]:
        logging.info("Atualizando TikTok (TokCount) para @%s", username)
        user_payload = self._tokcount_get(
            f"/user/data/{username}", include_identity=True
        )
        user_id = user_payload.get("userId")
        if not user_id:
            raise RuntimeError("TokCount nuo retornou o identificador do usuorio.")
        stats_payload = self._tokcount_get(
            f"/user/stats/{user_id}", include_identity=True
        )
        return {
            "status": "ok",
            "identifier": f"@{user_payload.get('username') or username}",
            "followers": stats_payload.get("followerCount"),
            "likes": stats_payload.get("likeCount"),
            "following": stats_payload.get("followingCount"),
            "videos": stats_payload.get("videoCount"),
        }

    def _tiktok_stats(self, username: str) -> Dict[str, Any]:
        fallback_reason: Optional[Tuple[str, str]] = None
        try:
            return self._tokcount_stats(username)
        except TemporaryBlockError as exc:
            logging.warning("TokCount bloqueado para TikTok: %s", exc)
            fallback_reason = ("blocked", str(exc))
        except requests.RequestException as exc:
            logging.warning("TokCount retornou HTTP error para TikTok: %s", exc)
            fallback_reason = ("error", str(exc))
        except Exception as exc:
            logging.exception("Erro coletando TikTok via TokCount")
            fallback_reason = ("error", str(exc))
        try:
            if fallback_reason:
                logging.info(
                    "Usando fallback de scraping do TikTok após falha no TokCount: %s",
                    fallback_reason[1],
                )
            return self._tiktok_stats_page(username)
        except Exception as fallback_exc:
            logging.exception("Erro coletando TikTok via fallback sem TokCount")
            if fallback_reason and fallback_reason[0] == "blocked":
                return {"status": "blocked", "message": fallback_reason[1]}
            return {"status": "error", "message": str(fallback_exc)}

class StatsCache:
    """Cache simples com TTL para evitar bloqueios desnecessários."""

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self.timestamp: Optional[datetime] = None
        self.payload: Optional[Dict[str, Any]] = None

    def get(self) -> Optional[Dict[str, Any]]:
        if not self.payload or not self.timestamp:
            return None
        if datetime.now(timezone.utc) - self.timestamp > timedelta(seconds=self.ttl):
            return None
        return self.payload

    def set(self, data: Dict[str, Any]) -> None:
        self.timestamp = datetime.now(timezone.utc)
        self.payload = data


class StatsCollector:
    """Orquestra a coleta dos dados e aplica cache com TTL."""

    def __init__(self, config: ConfigManager):
        ttl = config.get("cache_ttl_seconds", 600)
        self.cache = StatsCache(ttl_seconds=ttl)
        self.scraper = PlatformScraper(config)

    async def get_stats(self) -> Dict[str, Any]:
        cached = self.cache.get()
        if cached:
            return cached
        logging.info("Cache expirado, coletando estatísticas em tempo real.")
        results = await self._collect()
        results["generated_at"] = datetime.now(timezone.utc)
        if self._should_cache(results):
            self.cache.set(results)
        else:
            logging.info("Não armazenando cache porque uma plataforma retornou erro ou bloqueio.")
        return results

    def _should_cache(self, results: Dict[str, Any]) -> bool:
        for payload in results.values():
            if not isinstance(payload, dict):
                continue
            if payload.get("status") != "ok":
                return False
        return True

    async def _collect(self) -> Dict[str, Any]:
        tasks = [
            self._wrap("youtube", self.scraper.fetch_youtube_livecounts),
            self._wrap("tiktok", self.scraper.fetch_tiktok),
        ]
        results = await asyncio.gather(*tasks)
        return {name: payload for name, payload in results}

    async def _wrap(self, name: str, coro_factory):
        try:
            data = await coro_factory()
        except TemporaryBlockError as exc:
            logging.warning("%s bloqueado: %s", name, exc)
            data = {"status": "blocked", "message": str(exc)}
        except Exception as exc:
            logging.exception("Erro coletando %s", name)
            data = {"status": "error", "message": str(exc)}
        return name, data


def ensure_authorized(update: Update, config: ConfigManager) -> bool:
    """Verifica se o usuário chamando o bot é o autorizado."""

    authorized_id = config.get("authorized_user_id")
    user_id = update.effective_user.id if update.effective_user else None
    if authorized_id is None:
        return True
    return user_id == authorized_id


class TelegramBot:
    """Camada que expõe os handlers do bot."""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.collector = StatsCollector(config)

    def get_stats_chat_id(self) -> Optional[int]:
        """Retorna o chat id configurado para envio automático ou None."""
        chat_id = self.config.get("stats_chat_id")
        if chat_id is None:
            return None
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            logging.warning("stats_chat_id inválido: %s", chat_id)
            return None

    async def broadcast_stats_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = self.get_stats_chat_id()
        if not chat_id:
            return
        logging.info("Enviando estatísticas automáticas para %s", chat_id)
        try:
            data = await self.collector.get_stats()
            message = format_stats_message(data, self.config)
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logging.exception("Erro enviando estatísticas automáticas")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = (
            "👋 Olá! Use /stats para ver as métricas em tempo real.\n"
            "Para configurar URLs/usernames use /config.\n\n"
            "▶️ Este bot é privado. Informe seu ID numérico ao desenvolvedor e "
            "coloque-o em config.json (chave authorized_user_id).\n"
            "▶️ Atualize o arquivo config.json com o token fornecido pelo BotFather.\n"
            "▶️ O bot usa Selenium + undetected-chromedriver e APIs públicas; "
            "mantenha-o rodando em um servidor 24/7 (veja README)."
        )
        await safe_reply(update, context, message)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not ensure_authorized(update, self.config):
            await safe_reply(update, context, "🚫 Acesso negado. ID não autorizado.")
            return
        await safe_reply(update, context, "⏳ Buscando estatísticas atualizadas, aguarde...")
        data = await self.collector.get_stats()
        message = format_stats_message(data, self.config)
        await safe_reply(
            update,
            context,
            message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def config_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ensure_authorized(update, self.config):
            await safe_reply(update, context, "🚫 Acesso negado. ID não autorizado.")
            return ConversationHandler.END
        text = [
            "⚙️ Configuração das contas monitoradas",
            "Envie no formato <b>chave=valor</b> (ex.: <code>tiktok_username=seuperfil</code>).",
            "Chaves disponíveis:",
        ]
        for key, description in CONFIG_KEYS.items():
            current = self.config.get_profile_value(key) or "não definido"
            text.append(f"- <b>{key}</b>: {description} (atual: <code>{current}</code>)")
        text.append("Envie <b>cancelar</b> para sair.")
        await safe_reply(
            update,
            context,
            "\n".join(text), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        return CHOOSING

    async def save_config_value(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not ensure_authorized(update, self.config):
            await safe_reply(update, context, "🚫 Acesso negado. ID não autorizado.")
            return ConversationHandler.END
        if not update.message or not update.message.text:
            await safe_reply(update, context, "Mensagem vazia. Tente novamente.")
            return CHOOSING
        text = update.message.text.strip()
        if text.lower() in {"cancelar", "sair"}:
            await safe_reply(update, context, "✅ Configuração encerrada.")
            return ConversationHandler.END
        if "=" not in text:
            await safe_reply(
                update,
                context,
                "Formato inválido. Use <code>chave=valor</code>.",
                parse_mode=ParseMode.HTML,
            )
            return CHOOSING
        key, value = [part.strip() for part in text.split("=", 1)]
        if key not in CONFIG_KEYS:
            await safe_reply(
                update,
                context,
                f"Chave desconhecida: {key}. Consulte a lista enviada.",
                parse_mode=ParseMode.HTML,
            )
            return CHOOSING
        await self.config.update_profile_value(key, value)
        await safe_reply(update, context, f"✅ {key} atualizado para {value}.")
        return CHOOSING

    async def cancel_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await safe_reply(update, context, "❎ Configuração cancelada.")
        return ConversationHandler.END


def format_stats_message(data: Dict[str, Any], config: ConfigManager) -> str:
    generated_at = data.get("generated_at", datetime.now(timezone.utc))
    timestamp_str = generated_at.astimezone().strftime("%d/%m/%Y - %H:%M")
    ttl_seconds = config.get("cache_ttl_seconds", 600)
    next_refresh = generated_at + timedelta(seconds=ttl_seconds)
    ttl_minutes = max(1, int(ttl_seconds / 60))
    blocks = [
        format_platform_block(
            "YouTube",
            config.get_profile_value("youtube_channel_url") or "não configurado",
            data.get("youtube"),
            [
                ("Seguidores", "followers"),
                ("Vídeos", "videos"),
                ("Visualiza\u00e7\u00f5es totais", "views"),
            ],
        ),
        format_platform_block(
            "TikTok",
            (
                f"@{config.get_profile_value('tiktok_username')}"
                if config.get_profile_value("tiktok_username")
                else "não configurado"
            ),
            data.get("tiktok"),
            [("Seguidores", "followers"), ("Likes totais", "likes")],
        ),
    ]
    message_lines = [f"📊 <b>Suas estatísticas atualizadas</b> ({timestamp_str})", ""]
    for block in blocks:
        message_lines.append(block)
        message_lines.append("")
    message_lines.extend(
        [
            f"Última atualização: {timestamp_str}",
            f"Próxima atualização automática: {next_refresh.astimezone().strftime('%H:%M')} (cache máx. {ttl_minutes} min)",
        ]
    )
    return "\n".join(message_lines)


def format_platform_block(
    title: str,
    identifier: str,
    payload: Optional[Dict[str, Any]],
    fields: Iterable[Tuple[str, str]],
) -> str:
    if not payload:
        return f"{title}: {identifier}\n➡️ Sem dados."
    status = payload.get("status")
    if status == "not_configured":
        return f"{title}: {identifier}\n➡️ Configure com /config."
    if status == "blocked":
        return f"{title}: {identifier}\n➡️ Indisponível (bloqueio temporário)."
    if status == "error":
        return f"{title}: {identifier}\n➡️ Erro: {payload.get('message', 'desconhecido')}."
    lines = [f"{title}: {identifier}"]
    for label, key in fields:
        lines.append(f"{label}: {format_number(payload.get(key))}")
    return "\n".join(lines)


async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Garante que sempre consigamos responder mesmo sem update.message."""

    if update.message:
        return await update.message.reply_text(text, **kwargs)
    chat = update.effective_chat
    if chat:
        return await context.bot.send_message(chat_id=chat.id, text=text, **kwargs)
    logging.warning("Não foi possível enviar mensagem: chat ausente.")
    return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    load_env_file(Path(".env"))
    config_path = Path("config.json")
    config = ConfigManager(config_path)
    token = os.environ.get("TELEGRAM_TOKEN") or config.get("telegram_token")
    if not token:
        raise RuntimeError("Defina TELEGRAM_TOKEN em .env ou via variável de ambiente.")
    bot = TelegramBot(config)
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("stats", bot.stats))
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("config", bot.config_command)],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.save_config_value)],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel_config)],
    )
    application.add_handler(conv_handler)
    broadcast_interval = config.get("broadcast_interval_seconds")
    if broadcast_interval is None:
        broadcast_interval = config.get("cache_ttl_seconds", 600)
    try:
        broadcast_interval = int(broadcast_interval)
    except (TypeError, ValueError):
        broadcast_interval = config.get("cache_ttl_seconds", 600)
    broadcast_interval = max(1, broadcast_interval)
    stats_chat_id = bot.get_stats_chat_id()
    if stats_chat_id is not None:
        job_queue = application.job_queue
        if job_queue is None:
            logging.warning(
                "JobQueue não está configurado; instale python-telegram-bot[job-queue] para habilitar o envio automático."
            )
        else:
            job_queue.run_repeating(
                bot.broadcast_stats_job,
                interval=broadcast_interval,
                first=0,
            )

            logging.info(
                "Agendado envio automático de estatísticas a cada %d segundos",
                broadcast_interval,
            )
    logging.info("Bot iniciado. Pressione Ctrl+C para sair.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
