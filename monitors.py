#!/usr/bin/env python3
"""
monitors.py – Account Monitor v9.6 (STABLE 24/7 EDITION)
- إصلاح تدوير الجلسة (إعادة تسجيل المعالج)
- تحسين إعادة الاتصال واكتشاف العميل الميت
- دعم كامل لـ IntentEngine
"""
from __future__ import annotations
import asyncio
import os
import secrets
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Set
from cachetools import LRUCache, TTLCache
from loguru import logger
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    AuthKeyDuplicatedError,
)
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    InputPeerChannel,
)
from config import CFG, InputSanitizer, fast_hash
from database import EnhancedDatabase, MessageRecord, AlertRecord, DeadLetterRecord
from filter_engine import EnhancedFilter

# (نفس الدوال المساعدة من النسخة الأصلية: resolve_chat_entity, build_telegram_links)
async def resolve_chat_entity(client: TelegramClient, data: Dict[str, Any]) -> Any:
    username = data.get("username") or data.get("sender_username") or data.get("chat_username")
    if username:
        try:
            return await client.get_entity(username)
        except Exception:
            pass
    access_hash = data.get("chat_access_hash") or data.get("access_hash")
    chat_id = data.get("chat_id")
    if access_hash and chat_id and str(chat_id).startswith("-100"):
        try:
            inner_id = int(str(chat_id)[4:])
            peer = InputPeerChannel(channel_id=inner_id, access_hash=access_hash)
            return await client.get_entity(peer)
        except Exception:
            pass
    if chat_id:
        try:
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.id == chat_id:
                    return dialog.entity
        except Exception:
            pass
        try:
            return await client.get_entity(chat_id)
        except Exception:
            pass
    return None

def build_telegram_links(chat_id: int, message_id: int, username: str = None) -> Dict[str, str]:
    links = {"group": "#", "message": "#"}
    if username:
        clean = username.lstrip('@')
        links["group"] = f"https://t.me/{clean}"
        links["message"] = f"https://t.me/{clean}/{message_id}"
        return links
    if str(chat_id).startswith("-100"):
        inner = str(chat_id)[4:]
        links["group"] = f"https://t.me/c/{inner}"
        links["message"] = f"https://t.me/c/{inner}/{message_id}"
    return links

# (CircuitBreaker و DeadLetterQueue كما هي في النسخة الأصلية - تم الحفاظ عليها)
class CircuitState:
    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"

class CircuitBreakerOpen(Exception): pass

class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.name = name; self._threshold = failure_threshold; self._recovery = recovery_timeout
        self._failures = 0; self._successes = 0; self._last_fail: Optional[float] = None
        self._state = CircuitState.CLOSED; self._half_open_reqs = 0; self._lock = asyncio.Lock()
    async def call(self, coro_factory):
        await self._check()
        try:
            result = await coro_factory(); await self._success(); return result
        except FloodWaitError:
            await self._failure(flood=True); raise
        except Exception:
            await self._failure(); raise
    async def _check(self):
        async with self._lock:
            now = time.monotonic()
            if self._state == CircuitState.OPEN:
                if now - (self._last_fail or 0) > self._recovery:
                    self._state = CircuitState.HALF_OPEN; self._half_open_reqs = 0
                    logger.info(f"Circuit '{self.name}' HALF_OPEN")
                else: raise CircuitBreakerOpen(f"'{self.name}' OPEN")
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_reqs += 1
                if self._half_open_reqs > 2: raise CircuitBreakerOpen(f"'{self.name}' HALF_OPEN limit")
    async def _success(self):
        async with self._lock:
            self._successes += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED; self._failures = 0; self._half_open_reqs = 0
    async def _failure(self, flood=False):
        async with self._lock:
            self._failures += 1; self._last_fail = time.monotonic()
            if flood: self._recovery = min(self._recovery * 1.5, 600)
            if self._state == CircuitState.HALF_OPEN: self._state = CircuitState.OPEN
            elif self._state == CircuitState.CLOSED and self._failures >= self._threshold:
                self._state = CircuitState.OPEN; logger.warning(f"Circuit '{self.name}' OPEN ({self._failures} failures)")
    @property
    def state(self): return self._state
    @property
    def failures(self): return self._failures

class DeadLetterQueue:
    def __init__(self, db: EnhancedDatabase, max_retries: int = 3):
        self.db = db; self.max_retries = max_retries; self._lock = asyncio.Lock()
        self._retry_tasks: Set[asyncio.Task] = set(); self._task_lock = asyncio.Lock()
        self._stats: Dict[str, int] = defaultdict(int)
    async def push(self, event_data: Dict[str, Any], error: Exception, retry_count: int = 0):
        async with self._lock:
            if retry_count >= self.max_retries:
                await self.db.add_dead_letter(DeadLetterRecord(event_data=event_data, error_text=str(error), retry_count=retry_count, resolved=False, timestamp=time.time()))
                self._stats["dead_lettered"] += 1
            else:
                delay = min(2 ** retry_count, 60)
                task = asyncio.create_task(self._retry(event_data, retry_count + 1, delay))
                async with self._task_lock: self._retry_tasks.add(task)
                task.add_done_callback(lambda t: self._retry_tasks.discard(t))
                self._stats["retry_scheduled"] += 1
    async def _retry(self, event_data: Dict[str, Any], retry_count: int, delay: float):
        await asyncio.sleep(delay)
        self._stats["retry_executed"] += 1
    async def get_stats(self) -> Dict[str, int]: return dict(self._stats)
    async def stop(self):
        async with self._task_lock:
            for task in list(self._retry_tasks):
                if not task.done(): task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            self._retry_tasks.clear()

class ReconnectionManager:
    def __init__(self, monitor: "EnhancedAccountMonitor"):
        self._mon = monitor; self._backoff = 5.0; self._task: Optional[asyncio.Task] = None
        self._running = False; self._shutdown_event = asyncio.Event()
        self._last_error: Optional[str] = None; self._consecutive_failures = 0
        self._max_consecutive_failures = 10
    async def start(self):
        if self._running: return
        self._running = True; self._shutdown_event.clear()
        self._task = asyncio.create_task(self._loop(), name=f"reconnect_{self._mon.account['name']}")
    async def stop(self):
        self._running = False; self._shutdown_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
    async def reset(self):
        self._backoff = 5.0; self._consecutive_failures = 0; self._last_error = None
    async def _loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(CFG.RECONNECT_CHECK_INTERVAL)
                if not self._mon.is_connected:
                    last_error = self._mon._last_connect_error or ""
                    if any(x in last_error for x in ["AuthKeyDuplicatedError", "SessionPasswordNeededError"]):
                        logger.error(f"Unrecoverable error for {self._mon.account['name']}, suspending retries")
                        self._mon._last_connect_error = f"Unrecoverable: {last_error[:100]}"
                        await asyncio.sleep(3600); continue
                    self._consecutive_failures += 1
                    if self._consecutive_failures > self._max_consecutive_failures:
                        logger.error(f"Too many failures for {self._mon.account['name']}, marking as dead")
                        self._mon.is_connected = False; self._mon._last_connect_error = "Too many failures"
                        await asyncio.sleep(300); self._consecutive_failures = 0; continue
                    jitter = secrets.randbelow(3000) / 1000
                    sleep = min(self._backoff + jitter, CFG.MAX_RECONNECT_BACKOFF)
                    logger.info(f"Reconnecting {self._mon.account['name']} in {sleep:.1f}s (attempt {self._consecutive_failures})")
                    await asyncio.sleep(sleep)
                    ok = await self._mon.connect()
                    if ok: await self.reset(); logger.info(f"Reconnected: {self._mon.account['name']}")
                    else: self._backoff = min(self._backoff * CFG.RETRY_BACKOFF, CFG.MAX_RECONNECT_BACKOFF)
                else:
                    if self._mon.client:
                        try:
                            await asyncio.wait_for(self._mon.client.get_me(), timeout=15)
                            self._consecutive_failures = 0
                            # التحقق من وجود المعالج (إصلاح تدوير الجلسة)
                            if self._mon._handler_func is None:
                                logger.warning(f"Handler missing for {self._mon.account['name']}, re-registering...")
                                await self._mon._register_handler()
                        except Exception as e:
                            logger.warning(f"Heartbeat failed for {self._mon.account['name']}: {e}")
                            self._mon.is_connected = False; self._last_error = str(e)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"Reconnect loop error [{self._mon.account['name']}]: {e}")
                await asyncio.sleep(min(self._backoff, 30))

class EnhancedAccountMonitor:
    def __init__(self, account: Dict[str, Any], db: EnhancedDatabase, flt: EnhancedFilter, main_client: Optional[TelegramClient] = None):
        self.account = account; self.db = db; self.filter = flt; self.main_client = main_client
        self.client: Optional[TelegramClient] = None; self.is_connected = False; self.started_at: float = 0.0
        self._connect_lock = asyncio.Lock(); self._reconnect = ReconnectionManager(self)
        self._send_cb = CircuitBreaker(f"send_{account['name']}", failure_threshold=5, recovery_timeout=60.0)
        self._entity_cb = CircuitBreaker(f"entity_{account['name']}", failure_threshold=3, recovery_timeout=30.0)
        self._bot_ref: Any = None; self._handler_func = None
        self._stats: Dict[str, Any] = {
            "messages_processed": 0, "alerts_sent": 0, "errors": 0, "duplicates": 0, "rate_limited": 0,
            "send_errors": 0, "queue_processed": 0, "media_processed": 0, "avg_processing_time_ms": 0.0,
            "total_processing_time_ms": 0.0, "last_error": None, "last_alert_time": 0.0,
            "connect_attempts": 0, "reconnect_attempts": 0,
            "accepted": 0, "reviewed": 0, "ignored": 0, "avg_confidence": 0.0, "total_confidence": 0.0, "decisions_count": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._entity_cache: TTLCache = TTLCache(maxsize=CFG.ENTITY_CACHE_MAX_SIZE, ttl=600)
        self._cache_lock = asyncio.Lock()
        self._processed_hashes: LRUCache = LRUCache(maxsize=CFG.PROCESSED_HASHES_MAX_SIZE)
        self._processed_lock = asyncio.Lock()
        self._get_entity_sem = asyncio.Semaphore(5); self._pipeline_sem = asyncio.Semaphore(20)
        self._dlq = DeadLetterQueue(db, max_retries=CFG.DEAD_LETTER_MAX_RETRIES)
        self._connect_attempts = 0; self._last_connect_error: Optional[str] = None
        self._processing_times: List[float] = []; self._max_processing_times = 100
        self._session_rotate_task: Optional[asyncio.Task] = None; self._last_rotation = 0.0

    def set_bot(self, bot: Any) -> None: self._bot_ref = bot

    async def _inc_stat(self, name: str, delta: int = 1):
        async with self._stats_lock:
            if name in self._stats: self._stats[name] += delta

    async def _update_avg_time(self, time_ms: float):
        async with self._stats_lock:
            self._processing_times.append(time_ms)
            if len(self._processing_times) > self._max_processing_times: self._processing_times.pop(0)
            self._stats["avg_processing_time_ms"] = sum(self._processing_times) / len(self._processing_times) if self._processing_times else 0.0

    async def _update_decision_stats(self, decision: str, confidence: float):
        async with self._stats_lock:
            if decision == "accept": self._stats["accepted"] += 1
            elif decision == "review": self._stats["reviewed"] += 1
            else: self._stats["ignored"] += 1
            self._stats["total_confidence"] += confidence; self._stats["decisions_count"] += 1
            if self._stats["decisions_count"] > 0: self._stats["avg_confidence"] = self._stats["total_confidence"] / self._stats["decisions_count"]

    async def _chat_info(self, client: TelegramClient, chat_id: int, message_id: int, chat_access_hash: Optional[int] = None, chat_username: Optional[str] = None) -> Dict[str, Any]:
        cache_key = chat_id; entity = None
        async with self._cache_lock: entity = self._entity_cache.get(cache_key)
        if entity is None:
            entity = await resolve_chat_entity(client, {"chat_id": chat_id, "chat_access_hash": chat_access_hash, "username": chat_username})
            async with self._cache_lock: self._entity_cache[cache_key] = entity
        uname = getattr(entity, "username", None) if entity else None
        links = build_telegram_links(chat_id, message_id, username=uname)
        title = None
        if entity: title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
        return {"entity": entity, "title": title, "group_link": links["group"], "msg_link": links["message"]}

    def _build_alert(self, sender: Dict, chat: Dict, keyword: str, text: str, analysis: Dict = None) -> Tuple[str, Optional[List]]:
        safe_text = InputSanitizer.escape_html(InputSanitizer.truncate(text, 400))
        sender_id = sender.get("id", 0); display_name = sender.get("display", "مستخدم")
        username = sender.get("username", None); access_hash = sender.get("access_hash", None)
        message_html = safe_text; sender_link = display_name
        if username:
            clean_uname = username.lstrip('@'); sender_link = f'<a href="https://t.me/{clean_uname}">{display_name}</a>'
        elif sender_id:
            if access_hash: sender_link = f'<a href="tg://openmessage?user_id={sender_id}">{display_name}</a>'
            else: sender_link = f'<a href="tg://user?id={sender_id}">{display_name}</a>'
        group_link = chat.get("group_link", "#"); chat_title = chat.get("title"); msg_link = chat.get("msg_link", "#")
        if chat_title and chat_title != "غير معروف" and group_link != "#":
            group_html = f'<a href="{group_link}">{chat_title}</a>'
            msg_html = f'<a href="{msg_link}"><b>عرض الرسالة الأصلية</b></a>' if msg_link != "#" else "الرابط غير متاح"
            group_card = f'<blockquote dir="rtl">{group_html}\n\n{msg_html}</blockquote>'
        else:
            msg_html = f'<a href="{msg_link}"><b>عرض الرسالة الأصلية</b></a>' if msg_link != "#" else "الرابط غير متاح"
            group_card = f'<blockquote dir="rtl">{msg_html}</blockquote>'
        alert = (f"<b>الرسالة:</b>\n{message_html}\n\n👤: {sender_link}\n\n{group_card}")
        buttons = None
        if CFG.ALERT_WITH_BUTTONS:
            row = []
            if username: row.append(Button.url("💬 مراسلة", f"https://t.me/{username.lstrip('@')}"))
            row.append(Button.url("👤 فتح الحساب", f"tg://user?id={sender_id}"))
            if CFG.ALERT_WITH_COPY_BUTTON: row.append(Button.inline("📋 نسخ النص", f"copy_{analysis.get('msg_hash', '')}"))
            if row: buttons = [row]
        return alert, buttons

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.is_connected and self.client and self._is_client_alive(self.client): return True
            await self._cleanup_old_client()
            self._connect_attempts += 1; self._stats["connect_attempts"] += 1
            account = self.account; session_name = account["session"]
            for attempt in range(1, 4):
                client: Optional[TelegramClient] = None
                try:
                    logger.info(f"Connecting {account['name']} (attempt {attempt}/3)")
                    from telethon.sessions import StringSession
                    session_string = (account.get("session_string") or "").strip()
                    secure_path = f"{session_name}.session.enc"
                    plain_path = str(CFG.SESSIONS_DIR / f"{session_name}.session")
                    if CFG.SECURE_SESSIONS and os.path.exists(secure_path):
                        from config import SecretManager; SecretManager.decrypt_session(secure_path, plain_path)
                    client_kwargs = dict(
                        api_id=account["api_id"], api_hash=account["api_hash"],
                        device_model="Render Cloud", system_version="Linux", app_version="13.0",
                        timeout=CFG.CONNECTION_TIMEOUT, connection_retries=5, retry_delay=5,
                        auto_reconnect=False,
                    )
                    if session_string:
                        # Render/Cloud: non-interactive StringSession from env var
                        client = TelegramClient(StringSession(session_string), **client_kwargs)
                    elif os.path.exists(plain_path) or os.path.exists(f"{session_name}.session"):
                        session_ref = plain_path if os.path.exists(plain_path) else session_name
                        client = TelegramClient(session=session_ref, **client_kwargs)
                    else:
                        logger.error(
                            f"No session for {account['name']} - set its *_SESSION_STRING env var "
                            f"or login via the dashboard /login page"
                        )
                        self._last_connect_error = "No session - login via /login"
                        self._stats["last_error"] = "No session - login via /login"
                        return False
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.error(
                            f"Session for {account['name']} is expired/revoked - "
                            f"re-login via the dashboard /login page"
                        )
                        try: await client.disconnect()
                        except Exception: pass
                        self._last_connect_error = "Session expired - re-login via /login"
                        self._stats["last_error"] = "Session expired - re-login via /login"
                        return False
                    me = await client.get_me()
                    logger.info(f"Connected {account['name']} as @{me.username or me.id}")
                    self.client = client; self.is_connected = True; self.started_at = time.time()
                    self._last_connect_error = None; self._stats["last_error"] = None
                    await self._register_handler()  # تسجيل المعالج
                    await self._reconnect.start()
                    self._start_session_rotation()
                    return True
                except SessionPasswordNeededError:
                    logger.error(f"2FA required for {account['name']} - skipping")
                    if client: await client.disconnect()
                    self._last_connect_error = "2FA required"; self._stats["last_error"] = "2FA required"; return False
                except AuthKeyDuplicatedError:
                    logger.error(f"Session duplicated for {account['name']} - another active session is using the same key")
                    if client: await client.disconnect()
                    for p in (f"{session_name}.session", str(CFG.SESSIONS_DIR / f"{session_name}.session")):
                        try: os.remove(p)
                        except FileNotFoundError: pass
                    self._last_connect_error = "AuthKeyDuplicated"; self._stats["last_error"] = "AuthKeyDuplicated"; return False
                except Exception as e:
                    error_msg = str(e); self._last_connect_error = error_msg[:200]; self._stats["last_error"] = error_msg[:200]
                    logger.error(f"Connect error {account['name']} [{attempt}/3]: {type(e).__name__}: {error_msg[:150]}")
                    if client:
                        try: await client.disconnect()
                        except Exception: pass
                    if attempt < 3: await asyncio.sleep(5 * attempt)
            return False

    async def _cleanup_old_client(self):
        if self.client:
            if self._handler_func is not None:
                try: self.client.remove_event_handler(self._handler_func)
                except Exception: pass
            self._handler_func = None
            try: await self.client.disconnect()
            except Exception: pass
            self.client = None
        self.is_connected = False

    async def _register_handler(self):
        # إزالة أي معالج سابق لتجنب التكرار
        if self._handler_func is not None and self.client:
            try: self.client.remove_event_handler(self._handler_func)
            except Exception: pass
        @self.client.on(events.NewMessage())
        async def _handler(event: events.NewMessage.Event):
            start_time = time.perf_counter()
            try:
                if event.out: return
                if event.chat_id == CFG.ADMIN_CHAT_ID: return
                if event.sender and getattr(event.sender, "bot", False): return
                msg_date = event.message.date
                if msg_date and self.started_at > 0:
                    if msg_date.timestamp() < self.started_at - 5: return
                event_data = await self._event_to_dict(event)
                if event_data.get("has_media"):
                    media = event.message.media
                    event_data["media_object"] = media if event_data["media_type"] in ("photo", "document") else None
                    await self._process_event_pipeline(event_data)
                    await self._inc_stat("media_processed"); await self._inc_stat("messages_processed"); return
                priority = 7 if event_data.get("has_text") else 3
                await self.db.add_to_queue(event_data, priority=priority)
                processing_time = (time.perf_counter() - start_time) * 1000
                await self._update_avg_time(processing_time)
                await self._inc_stat("messages_processed"); await self._inc_stat("queue_processed")
            except Exception as e:
                logger.error(f"Handler error [{self.account['name']}]: {e}")
                await self._inc_stat("errors"); self._stats["last_error"] = str(e)
        self._handler_func = _handler
        logger.info(f"Event handler registered for {self.account['name']}")

    async def _event_to_dict(self, event: events.NewMessage.Event) -> Dict[str, Any]:
        sender = event.sender; chat = event.chat
        text = event.message.text or ""; caption = getattr(event.message, "message", "") or ""
        full_text = (text or caption).strip()
        media = event.message.media; media_type = self._get_media_type(media)
        has_media = media_type in ("photo", "document")
        chat_username = getattr(chat, "username", None) if chat else None
        return {
            "chat_id": event.chat_id, "message_id": event.message.id,
            "sender_id": getattr(event, "sender_id", 0) or 0,
            "sender_username": getattr(sender, "username", None), "sender_first_name": getattr(sender, "first_name", None),
            "sender_last_name": getattr(sender, "last_name", None), "sender_access_hash": getattr(sender, "access_hash", None),
            "chat_access_hash": getattr(chat, "access_hash", None), "chat_username": chat_username,
            "text": full_text, "has_text": bool(full_text), "has_media": has_media, "media_type": media_type,
            "account_name": self.account["name"], "timestamp": time.time(),
        }

    @staticmethod
    def _get_media_type(media: Any) -> Optional[str]:
        if media is None: return None
        if isinstance(media, MessageMediaPhoto): return "photo"
        if isinstance(media, MessageMediaDocument): return "document"
        if isinstance(media, MessageMediaWebPage): return "webpage"
        return "other"

    def _start_session_rotation(self):
        if self._session_rotate_task and not self._session_rotate_task.done(): return
        self._session_rotate_task = asyncio.create_task(self._session_rotate_loop())

    async def _session_rotate_loop(self):
        while self.is_connected:
            await asyncio.sleep(CFG.SESSION_REFRESH_INTERVAL)
            if self.is_connected and self.client:
                try: await self._rotate_session()
                except Exception as e: logger.error(f"Session rotation error [{self.account['name']}]: {e}")

    async def _rotate_session(self) -> bool:
        """إصلاح: إعادة تسجيل المعالج بعد التدوير"""
        if not self.client or not self.is_connected: return False
        try:
            logger.info(f"Rotating session for {self.account['name']}")
            session_string = self.client.session.save()
            # إزالة المعالج القديم قبل قطع الاتصال
            if self._handler_func:
                try: self.client.remove_event_handler(self._handler_func)
                except Exception: pass
            await self.client.disconnect()
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            new_client = TelegramClient(StringSession(session_string), self.account["api_id"], self.account["api_hash"])
            await new_client.start()
            self.client = new_client; self.is_connected = True
            self._last_rotation = time.time()
            await self._register_handler()  # ✅ تسجيل المعالج على العميل الجديد
            logger.info(f"Session rotated successfully for {self.account['name']}")
            return True
        except Exception as e:
            logger.error(f"Session rotation failed for {self.account['name']}: {e}")
            self.is_connected = False
            return False

    async def process_event_from_queue(self, event_data: Dict[str, Any]):
        start_time = time.perf_counter()
        async with self._pipeline_sem:
            try:
                await self._process_event_pipeline(event_data)
                processing_time = (time.perf_counter() - start_time) * 1000
                await self._update_avg_time(processing_time); await self._inc_stat("queue_processed")
            except Exception as e:
                logger.error(f"process_event_from_queue error [{self.account['name']}]: {e}")
                await self._inc_stat("errors"); self._stats["last_error"] = str(e)
                await self._dlq.push(event_data, e)

    async def _process_event_pipeline(self, data: Dict[str, Any]):
        if not await self._validate_event(data): return
        msg_hash, validated_text, is_new = await self._store_message(data)
        if not is_new: await self._inc_stat("duplicates"); return
        await self._analyze_and_alert(data, msg_hash, validated_text)

    async def _validate_event(self, data: Dict[str, Any]) -> bool:
        sender_id = data.get("sender_id", 0); chat_id = data.get("chat_id", 0)
        if await self.db.is_blocked_sender(sender_id): return False
        if await self.db.is_blocked_chat(chat_id): return False
        text = data.get("text", "")
        if text and not (CFG.MIN_MESSAGE_LENGTH <= len(text) <= CFG.MAX_MESSAGE_LENGTH):
            await self._inc_stat("errors"); return False
        return True

    async def _store_message(self, data: Dict[str, Any]) -> Tuple[str, Optional[str], bool]:
        text = data.get("text", ""); chat_id = data.get("chat_id", 0); message_id = data.get("message_id", 0)
        media_type = data.get("media_type", "media"); timestamp = data.get("timestamp", time.time())
        validated_text = text if (text and CFG.MIN_MESSAGE_LENGTH <= len(text) <= CFG.MAX_MESSAGE_LENGTH) else None
        norm = (text or "").lower().strip(); msg_hash = fast_hash(f"{chat_id}_{message_id}_{norm}")
        async with self._processed_lock:
            if msg_hash in self._processed_hashes: return msg_hash, validated_text, False
            self._processed_hashes[msg_hash] = True
        rec = MessageRecord(message_hash=msg_hash, chat_id=chat_id, sender_id=data.get("sender_id", 0),
            message_text=validated_text or f"[{media_type}]", timestamp=timestamp)
        is_new = await self.db.try_insert_message(rec)
        return msg_hash, validated_text, is_new

    async def _analyze_and_alert(self, data: Dict[str, Any], msg_hash: str, validated_text: Optional[str]):
        sender_id = data.get("sender_id", 0); has_media = bool(data.get("media_object"))
        start_time = time.perf_counter()
        if validated_text:
            analysis = await self.filter.analyze(validated_text)
            decision = analysis.get("decision", "ignore"); confidence = analysis.get("confidence", 0.0)
            reasons = analysis.get("reasons", [])
            intent_verb = analysis.get("intent_verb"); academic_object = analysis.get("academic_object")
            keyword = analysis.get("keyword")
            if isinstance(keyword, (tuple, list)): keyword = keyword[0] if keyword else None
            if isinstance(intent_verb, (tuple, list)): intent_verb = intent_verb[0] if intent_verb else None
            if isinstance(academic_object, (tuple, list)): academic_object = academic_object[0] if academic_object else None
            analysis.update({"decision": decision, "confidence": confidence, "reasons": reasons,
                             "intent_verb": intent_verb, "academic_object": academic_object, "msg_hash": msg_hash})
            await self._update_decision_stats(decision, confidence)
            if analysis.get("valid") and has_media: analysis["has_media"] = True
            elif has_media: analysis = {"valid": False, "reason": "media_only_no_text", "keyword": None, "decision": "ignore"}
        else:
            analysis = {"valid": False, "reason": "no_content", "keyword": None, "decision": "ignore"}
        is_valid = analysis.get("valid", False)
        try: await self.db.update_sender_reputation(sender_id, is_valid)
        except Exception: pass
        if is_valid and await self.db.can_send_alert(sender_id):
            await self._send_alert(data, keyword=analysis.get("keyword") or "طلب", text=validated_text or "",
                                   msg_hash=msg_hash, analysis=analysis)
            await self._inc_stat("alerts_sent")
            async with self._stats_lock: self._stats["last_alert_time"] = time.time()
        try:
            await self.db.upsert_sender_contact({"sender_id": sender_id, "access_hash": data.get("sender_access_hash"),
                "username": data.get("sender_username"), "first_name": data.get("sender_first_name"),
                "last_name": data.get("sender_last_name"), "chat_id": data["chat_id"], "message_id": data["message_id"]})
        except Exception: pass
        processing_time = (time.perf_counter() - start_time) * 1000
        await self._update_avg_time(processing_time)

    async def _resolve_send_client(self) -> Optional[TelegramClient]:
        candidates = []
        mc = self._bot_ref.main_client if self._bot_ref else None
        if mc and self._is_client_alive(mc): candidates.append(mc)
        if self.client and self._is_client_alive(self.client): candidates.append(self.client)
        if self._bot_ref:
            for mon in self._bot_ref.monitors:
                if mon is self: continue
                if mon.client and mon.is_connected and self._is_client_alive(mon.client): candidates.append(mon.client)
        for c in candidates:
            if self._is_client_alive(c): return c
        return None

    @staticmethod
    def _is_client_alive(client: TelegramClient) -> bool:
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception: return False

    async def _send_alert(self, data: Dict[str, Any], keyword: str, text: str, msg_hash: str, analysis: Dict[str, Any]):
        if not self._bot_ref: return
        account_name = data.get("account_name", self.account["name"])
        if not await self._bot_ref.rate_limiter.can_proceed(account_name):
            await self._inc_stat("rate_limited"); return
        chat_id = data["chat_id"]; message_id = data["message_id"]; sender_id = data["sender_id"]
        sender_username = data.get("sender_username"); sender_first_name = data.get("sender_first_name")
        sender_last_name = data.get("sender_last_name"); sender_access_hash = data.get("sender_access_hash")
        chat_access_hash = data.get("chat_access_hash"); chat_username = data.get("chat_username")
        display_name = f"{sender_first_name or ''} {sender_last_name or ''}".strip() or f"مستخدم ({sender_id})"
        send_client = await self._resolve_send_client()
        if not send_client:
            logger.error(f"No available client to send alert [{account_name}]"); await self._inc_stat("send_errors"); return
        chat_info = await self._chat_info(send_client, chat_id, message_id, chat_access_hash=chat_access_hash, chat_username=chat_username)
        analysis["msg_hash"] = msg_hash
        sender = {"id": sender_id, "display": display_name, "username": sender_username, "access_hash": sender_access_hash}
        alert_text, buttons = self._build_alert(sender, chat_info, keyword, text, analysis)
        user_media = data.get("media_object")
        async def do_send():
            sent = False
            if user_media is not None:
                try:
                    await send_client.send_file(CFG.TARGET_GROUP_ID, file=user_media, caption=alert_text, buttons=buttons, parse_mode="html", link_preview=False)
                    sent = True
                except Exception as e: logger.debug(f"User media send failed: {e}")
            if not sent:
                chat_entity = chat_info.get("entity")
                if chat_entity and getattr(chat_entity, 'id', 0) != 0:
                    try:
                        result = await send_client.get_profile_photos(chat_entity, limit=1)
                        if result and hasattr(result, 'photos') and len(result.photos) > 0:
                            await send_client.send_file(CFG.TARGET_GROUP_ID, file=result.photos[0], caption=alert_text, parse_mode="html", link_preview=False)
                            sent = True
                    except Exception: pass
            if not sent:
                await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode="html", link_preview=False)
        try:
            await self._send_cb.call(do_send)
            safe_keyword = keyword
            if isinstance(safe_keyword, (tuple, list)): safe_keyword = safe_keyword[0] if safe_keyword else ""
            if not isinstance(safe_keyword, str): safe_keyword = str(safe_keyword) if safe_keyword is not None else ""
            await self.db.add_alert(AlertRecord(message_hash=msg_hash, chat_id=chat_id, sender_id=sender_id,
                account_name=account_name, keyword=safe_keyword, alert_text=alert_text, timestamp=time.time()))
            logger.info(f"Alert sent by {account_name} | kw={keyword!r} | sender={display_name}")
        except (FloodWaitError, CircuitBreakerOpen) as e:
            await self._dlq.push({"type": "alert", "meta": {"msg_hash": msg_hash, "chat_id": chat_id, "sender_id": sender_id, "account_name": account_name}}, e)
            await self._inc_stat("send_errors"); raise
        except Exception as e:
            logger.error(f"Send alert error [{account_name}]: {e} - trying fallback")
            try:
                await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode=None, link_preview=False)
            except Exception as fe:
                logger.error(f"Fallback failed: {fe}")
                await self._inc_stat("send_errors")
                await self._dlq.push({"type": "alert", "meta": {"msg_hash": msg_hash, "chat_id": chat_id, "sender_id": sender_id, "account_name": account_name}}, e)

    async def disconnect(self):
        async with self._connect_lock:
            self.is_connected = False
            await self._reconnect.stop(); await self._dlq.stop()
            if self._session_rotate_task:
                self._session_rotate_task.cancel()
                try: await self._session_rotate_task
                except asyncio.CancelledError: pass
            self._entity_cache.clear(); self._processed_hashes.clear(); self._processing_times.clear()
            if self.client:
                if self._handler_func:
                    try: self.client.remove_event_handler(self._handler_func)
                    except Exception: pass
                self._handler_func = None
                if CFG.SECURE_SESSIONS:
                    try:
                        session_path = f"{self.account['session']}.session"
                        secure_path = f"{self.account['session']}.session.enc"
                        if os.path.exists(session_path):
                            from config import SecretManager
                            SecretManager.encrypt_session(session_path, secure_path); os.remove(session_path)
                    except Exception as e: logger.error(f"Session encrypt error: {e}")
                try: await self.client.disconnect()
                except Exception: pass
            self.client = None
            logger.info(f"Disconnected {self.account['name']}")

    async def get_stats(self) -> Dict[str, Any]:
        async with self._stats_lock:
            stats = dict(self._stats)
        return {
            "name": self.account["name"], "phone": self.account["phone"], "connected": self.is_connected,
            "priority": self.account.get("priority", 0), "send_cb_state": self._send_cb.state,
            "send_cb_failures": self._send_cb.failures, "entity_cb_state": self._entity_cb.state,
            "entity_cb_failures": self._entity_cb.failures, "connect_attempts": self._connect_attempts,
            "last_error": self._last_connect_error, "messages_processed": stats.get("messages_processed", 0),
            "alerts_sent": stats.get("alerts_sent", 0), "errors": stats.get("errors", 0),
            "duplicates": stats.get("duplicates", 0), "rate_limited": stats.get("rate_limited", 0),
            "send_errors": stats.get("send_errors", 0), "queue_processed": stats.get("queue_processed", 0),
            "media_processed": stats.get("media_processed", 0), "avg_processing_time_ms": round(stats.get("avg_processing_time_ms", 0), 2),
            "last_alert_time": stats.get("last_alert_time", 0), "dlq_stats": await self._dlq.get_stats(),
            "cache_size": len(self._entity_cache), "processed_hashes_size": len(self._processed_hashes),
            "accepted": stats.get("accepted", 0), "reviewed": stats.get("reviewed", 0), "ignored": stats.get("ignored", 0),
            "avg_confidence": round(stats.get("avg_confidence", 0.0), 2), "decisions_count": stats.get("decisions_count", 0),
        }

# (HealthMonitor كما هو في النسخة الأصلية - تم الحفاظ عليه)
class HealthStatus:
    def __init__(self, is_healthy: bool, checks: Dict[str, Any], details: Optional[Dict[str, Any]] = None):
        self.is_healthy = is_healthy; self.checks = checks; self.details = details or {}; self.timestamp = time.monotonic()

class HealthMonitor:
    def __init__(self, bot_ref: Any = None): self._bot = bot_ref; self._consecutive_fail = 0
    @staticmethod
    async def client_ok(client: Any) -> bool:
        if client is None: return False
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception: return False
    async def check(self) -> HealthStatus:
        checks = {}; details = {}
        if self._bot:
            try:
                if self._bot.db.is_connected:
                    await self._bot.db._fetchone("SELECT 1 AS ping"); checks["database"] = "ok"
                else: checks["database"] = "disconnected"
                up = sum(1 for m in self._bot.monitors if m.is_connected); total = len(self._bot.monitors)
                checks["monitors_up"] = up; checks["monitors_total"] = total
                monitors_stats = []
                for m in self._bot.monitors:
                    s = await m.get_stats(); monitors_stats.append({"name": s.get("name"), "connected": s.get("connected"),
                        "messages_processed": s.get("messages_processed", 0), "alerts_sent": s.get("alerts_sent", 0),
                        "errors": s.get("errors", 0), "avg_time": s.get("avg_processing_time_ms", 0), "last_error": s.get("last_error"),
                        "accepted": s.get("accepted", 0), "reviewed": s.get("reviewed", 0), "ignored": s.get("ignored", 0), "avg_confidence": s.get("avg_confidence", 0)})
                details["monitors"] = monitors_stats
                checks["monitors_ok"] = up >= max(1, total * CFG.HEALTH_MIN_MONITORS_RATIO)
                main_ok = await self.client_ok(self._bot.main_client); checks["main_client"] = "up" if main_ok else "down"
                qsize = await self._bot.db.queue_size(); checks["queue_size"] = qsize; checks["queue_ok"] = qsize < CFG.MESSAGE_QUEUE_SIZE
                filter_tele = await self._bot.filter.get_telemetry(); checks["filter_processed"] = filter_tele.get("processed", 0)
                checks["filter_valid"] = filter_tele.get("valid", 0); checks["filter_accepted"] = filter_tele.get("accepted", 0); checks["filter_review"] = filter_tele.get("review", 0)
            except Exception as e: checks["database"] = f"error: {e}"
        is_healthy = checks.get("database") == "ok" and checks.get("monitors_ok", False) and checks.get("main_client") == "up"
        self._consecutive_fail = 0 if is_healthy else self._consecutive_fail + 1
        details["consecutive_unhealthy"] = self._consecutive_fail
        return HealthStatus(is_healthy=is_healthy, checks=checks, details=details)
