import sys
import os
import asyncio
import random
import logging
import time
import threading
import uvicorn
import re
from datetime import datetime

# Принудительная вставка пути для корректного поиска локальных модулей
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Импорты Telethon
from telethon import TelegramClient, events, functions, types, errors
from telethon.tl.functions.messages import DeleteHistoryRequest, ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest

# Импорты БД и Конфига
from config import *
from models import SessionLocal, User, SessionConfig, TaskQueue, SystemLog, engine

# Импорты Воркеров
from warmer import AccountWarmer
from deep_parser import TargetParser
from session_add import SessionManager
from web_server import app as web_app
from utils import print_status

# Глобальная блокировка логов в консоли (пишем только критику)
logging.basicConfig(level=logging.ERROR)

class DragonKernel:
    def __init__(self):
        # Структура: {user_id: {session_filename: ClientObject}}
        self.clients_map = {} 
        self.admin_bot = None
        self.is_running = True
        self.session_mgr = SessionManager(self)
        
        # Реестр активных фоновых задач asyncio для возможности их отмены
        self.active_tasks = {} # {task_id: asyncio.Task}

    # ================= СИСТЕМА ЛОГИРОВАНИЯ (DB & CONSOLE) =================
    
    def db_log(self, user_id, level, message):
        """Потокобезопасная запись лога в базу данных"""
        db = SessionLocal()
        try:
            new_log = SystemLog(
                user_id=user_id,
                level=level,
                message=message,
                timestamp=time.time()
            )
            db.add(new_log)
            db.commit()
            # Визуализация в консоли сервера/Termux
            print_status(f"[UID:{user_id}] {message}", level.lower())
        except Exception as e:
            print(f"CRITICAL LOG ERROR: {e}")
        finally:
            db.close()

    # ================= МОДУЛЬ RADICAL WATCHER (ANTI-SPONSOR) =================

    async def radical_watcher_handler(self, client, event, user_id):
        """
        Автоматическое вступление в каналы, ссылки на которые 
        встречаются в сообщениях или кнопках (обход 'Подпишись, чтобы увидеть').
        """
        try:
            text = (event.message.message or "").lower()
            # Поиск ссылок типа t.me/ или @username
            links = re.findall(r"(?:https?://)?t\.me/[\w\+\-]+|@[\w\d_]+", text)
            
            # Также проверяем кнопки (Inline Buttons)
            if event.message.reply_markup:
                if hasattr(event.message.reply_markup, 'rows'):
                    for row in event.message.reply_markup.rows:
                        for btn in row.buttons:
                            if hasattr(btn, 'url') and btn.url:
                                links.append(btn.url)

            if not links:
                return

            for link in set(links):
                # Проверка на триггеры спонсорства
                if any(trigger in text for trigger in SPONSOR_TRIGGERS):
                    await self.join_target(client, link, user_id)
                    self.db_log(user_id, "SUCCESS", f"Radical Watcher: Вступил в {link} для обхода проверки.")
        except:
            pass

    # ================= УМНОЕ ВСТУПЛЕНИЕ В ЧАТЫ =================

    async def join_target(self, client, link, user_id):
        """Универсальный метод вступления: чаты, каналы, приватные ссылки"""
        try:
            if not link: return None
            
            # Очистка ссылки
            link = link.strip().replace("https://", "").replace("http://", "").replace("t.me/", "")
            
            try:
                if "joinchat/" in link or "+" in link:
                    # Приватная ссылка
                    hash_val = link.replace("joinchat/", "").replace("+", "").split("/")[0]
                    await client(ImportChatInviteRequest(hash=hash_val))
                else:
                    # Публичная ссылка/юзернейм
                    target = await client.get_entity(link)
                    await client(JoinChannelRequest(target))
                
                return True
            except errors.UserAlreadyParticipantError:
                return True
            except Exception as e:
                self.db_log(user_id, "ERROR", f"Ошибка вступления в {link}: {str(e)}")
                return False
        except:
            return False

    # ================= СИНХРОНИЗАЦИЯ СЕССИЙ =================

    async def sync_sessions(self):
        """Полная инвентаризация файлов сессий и их подключение"""
        db = SessionLocal()
        try:
            users = db.query(User).all()
            total_loaded = 0
            
            for u in users:
                if u.id not in self.clients_map:
                    self.clients_map[u.id] = {}
                
                sessions_dir = os.path.join(u.folder_path, "sessions")
                if not os.path.exists(sessions_dir):
                    os.makedirs(sessions_dir, exist_ok=True)

                files = [f for f in os.listdir(sessions_dir) if f.endswith('.session')]
                
                for f_name in files:
                    config = db.query(SessionConfig).filter_by(user_id=u.id, filename=f_name).first()
                    if not config:
                        config = SessionConfig(user_id=u.id, filename=f_name)
                        db.add(config); db.commit()
                    
                    if not config.is_active:
                        if f_name in self.clients_map[u.id]:
                            await self.clients_map[u.id][f_name].disconnect()
                            del self.clients_map[u.id][f_name]
                        continue

                    if f_name not in self.clients_map[u.id]:
                        try:
                            s_path = os.path.join(sessions_dir, f_name).replace('.session', '')
                            client = TelegramClient(s_path, API_ID, API_HASH)
                            await client.connect()
                            
                            if await client.is_user_authorized():
                                # Вешаем Radical Watcher на каждый активный клиент
                                @client.on(events.NewMessage(incoming=True))
                                async def handler(ev, _cl=client, _uid=u.id):
                                    await self.radical_watcher_handler(_cl, ev, _uid)
                                    
                                self.clients_map[u.id][f_name] = client
                                total_loaded += 1
                            else:
                                await client.disconnect()
                        except:
                            pass
            return total_loaded
        finally:
            db.close()

    # ================= ПРОВЕРКА ПРАВ (RBAC) =================

    def get_authorized_workers(self, user_id, capability):
        """Возвращает только те сессии, которым разрешена конкретная роль"""
        db = SessionLocal()
        try:
            configs = db.query(SessionConfig).filter_by(user_id=user_id).all()
            allowed = {c.filename for c in configs if getattr(c, capability, False) and c.is_active}
        finally:
            db.close()
        
        workers = []
        if user_id in self.clients_map:
            for fname, cl in self.clients_map[user_id].items():
                if fname in allowed and cl.is_connected():
                    workers.append(cl)
        return workers

# ================= ИСПОЛНИТЕЛЬ: SPAM GROUPS =================

    async def task_spam_chat(self, task_id, user_id):
        """Массовая рассылка по всем группам, в которых состоят боты"""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).get(task_id)
            user = db.query(User).get(user_id)
            
            # Чтение текста сообщения
            msg_file = os.path.join(user.folder_path, USER_FILES["msg"])
            if not os.path.exists(msg_file):
                self.db_log(user_id, "ERROR", "Рассылка отменена: raskid.txt не найден.")
                task.status = 'failed'; db.commit(); return

            with open(msg_file, "r", encoding="utf-8") as f: content = f.read()
            if not content.strip():
                self.db_log(user_id, "ERROR", "Текст для рассылки пуст.")
                task.status = 'failed'; db.commit(); return

            workers = self.get_authorized_workers(user_id, 'can_spam')
            if not workers:
                self.db_log(user_id, "ERROR", "Нет сессий с правами на СПАМ.")
                task.status = 'failed'; db.commit(); return

            self.db_log(user_id, "INFO", f"Запуск Spam Groups через {len(workers)} аккаунтов.")

            for cl in workers:
                # Проверка на прерывание задачи пользователем
                db.refresh(task)
                if task.status == 'stopped': break
                
                try:
                    async for dialog in cl.iter_dialogs(limit=100):
                        if dialog.is_group or dialog.is_channel:
                            # Проверка задержек из настроек юзера
                            await asyncio.sleep(random.uniform(user.config_min_delay, user.config_max_delay))
                            
                            # Typing эмуляция
                            if user.config_humanize:
                                async with cl.action(dialog.entity, 'typing'):
                                    await asyncio.sleep(3)
                            
                            await cl.send_message(dialog.entity, content)
                            user.sent_count += 1
                            db.commit()
                except errors.FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except:
                    continue

            task.status = 'completed'
            db.commit()
            self.db_log(user_id, "SUCCESS", "Рассылка по группам завершена.")
        finally:
            db.close()

    # ================= ИСПОЛНИТЕЛЬ: DM SPAM (ПО СПИСКУ) =================

    async def task_dm_spam(self, task_id, user_id):
        """Рассылка в личные сообщения по файлу parsed_users.txt"""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).get(task_id)
            user = db.query(User).get(user_id)
            
            # Чтение списка целей
            users_file = os.path.join(user.folder_path, USER_FILES["prs"])
            msg_file = os.path.join(user.folder_path, USER_FILES["msg"])
            
            if not os.path.exists(users_file) or os.path.getsize(users_file) == 0:
                self.db_log(user_id, "ERROR", "Список юзеров для DM пуст или не найден.")
                task.status = 'failed'; db.commit(); return

            with open(users_file, "r", encoding="utf-8") as f:
                targets = [l.strip() for l in f if l.strip()]
            
            with open(msg_file, "r", encoding="utf-8") as f:
                content = f.read()

            workers = self.get_authorized_workers(user_id, 'can_spam')
            if not workers:
                self.db_log(user_id, "ERROR", "Нет сессий для DM рассылки.")
                task.status = 'failed'; db.commit(); return

            self.db_log(user_id, "INFO", f"Запуск DM Spam на {len(targets)} целей.")

            for i, target in enumerate(targets):
                db.refresh(task)
                if task.status == 'stopped': break
                
                # Распределяем цели между воркерами (Round Robin)
                cl = workers[i % len(workers)]
                
                try:
                    # Рандомная задержка для безопасности (увеличена для DM)
                    await asyncio.sleep(random.uniform(30, 90))
                    
                    await cl.send_message(target, content)
                    user.sent_count += 1
                    db.commit()
                except errors.PeerFloodError:
                    self.db_log(user_id, "WARNING", "Ограничение на рассылку (Flood) на одной из сессий.")
                except:
                    continue

            task.status = 'completed'; db.commit()
            self.db_log(user_id, "SUCCESS", "Рассылка в ЛС завершена.")
        finally:
            db.close()

    # ================= ИСПОЛНИТЕЛЬ: PARSER =================

    async def task_parser(self, task_id, user_id):
        """Парсинг аудитории чата"""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).get(task_id)
            user = db.query(User).get(user_id)
            target = task.target_value
            
            if not target:
                self.db_log(user_id, "ERROR", "Для парсинга нужна ссылка."); task.status = 'failed'; db.commit(); return

            workers = self.get_authorized_workers(user_id, 'can_parse')
            if not workers:
                self.db_log(user_id, "ERROR", "Нет сессий с правами на парсинг."); task.status = 'failed'; db.commit(); return

            # Вступление лидера
            await self.join_target(workers[0], target, user_id)

            engine_parser = TargetParser(workers, self.admin_bot, self.db_log, user.folder_path)
            
            count = await engine_parser.run_distributed_parsing(
                target_link=target,
                limit=user.config_parse_limit,
                deep_scan=user.config_parse_depth
            )
            
            user.parsed_count += count
            task.status = 'completed'; db.commit()
            self.db_log(user_id, "SUCCESS", f"Парсинг окончен. Найдено {count} активных аккаунтов.")
        except Exception as e:
            self.db_log(user_id, "ERROR", f"Сбой парсера: {str(e)}"); task.status = 'failed'; db.commit()
        finally:
            db.close()

    # ================= МОНИТОР ОЧЕРЕДИ ЗАДАЧ =================

    async def task_monitor_loop(self):
        """Главный управляющий цикл ядра"""
        print_status("System Ready. Monitoring DB TaskQueue...", "success")
        
        while self.is_running:
            await self.sync_sessions()
            db = SessionLocal()
            try:
                # Берем первую задачу, которая ждет
                task = db.query(TaskQueue).filter_by(status='pending').order_by(TaskQueue.created_at.asc()).first()
                
                if task:
                    # МГНОВЕННО метим задачу как в работе, чтобы избежать дублей
                    task.status = 'processing'
                    db.commit()
                    
                    tid, uid, cmd = task.id, task.user_id, task.command
                    self.db_log(uid, "SYSTEM", f"Взята задача #{tid}: {cmd.upper()}")

                    if cmd == 'spam_chat':
                        self.active_tasks[tid] = asyncio.create_task(self.task_spam_chat(tid, uid))
                    elif cmd == 'spam_dm':
                        self.active_tasks[tid] = asyncio.create_task(self.task_dm_spam(tid, uid))
                    elif cmd == 'parse':
                        self.active_tasks[tid] = asyncio.create_task(self.task_parser(tid, uid))
                    elif cmd == 'warmup':
                        # Прогрев (реализован в warmer.py)
                        task.status = 'completed'; db.commit()
                    elif cmd == 'stop_all':
                        # Останавливаем все задачи пользователя
                        user_tasks = db.query(TaskQueue).filter_by(user_id=uid, status='processing').all()
                        for ut in user_tasks: ut.status = 'stopped'
                        task.status = 'completed'; db.commit()
                
                await asyncio.sleep(user.config_loop_wait if 'user' in locals() else 2)
            except:
                await asyncio.sleep(3)
            finally:
                db.close()

    # ================= ЗАПУСК СИСТЕМЫ =================

    async def run(self):
        # 1. Запуск Web-сервера
        web_thread = threading.Thread(
            target=uvicorn.run, 
            args=(web_app,), 
            kwargs={"host": WEB_HOST, "port": WEB_PORT, "log_level": "critical"},
            daemon=True
        )
        web_thread.start()
        
        # 2. Запуск Админ-Бота
        self.admin_bot = TelegramClient('admin_bot_session', API_ID, API_HASH)
        await self.admin_bot.start(bot_token=BOT_TOKEN)
        self.admin_bot.add_event_handler(self.session_mgr.bot_start_login, events.CallbackQuery(data=b"auth_phone"))
        self.admin_bot.add_event_handler(self.session_mgr.keypad_handler, events.CallbackQuery(pattern=b"d_.*"))
        print_status("Admin-Bot Link: Established.", "success")
        
        # 3. Запуск главного цикла
        await self.task_monitor_loop()

if __name__ == '__main__':
    try:
        if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        core = DragonKernel()
        asyncio.run(core.run())
    except (KeyboardInterrupt, SystemExit):
        os._exit(0)