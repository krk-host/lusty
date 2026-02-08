import asyncio
import os
import random
import time
from datetime import datetime, timedelta
from telethon import functions, types, errors
from config import USER_FILES

class TargetParser:
    def __init__(self, clients, bot, log_func, folder_path):
        """
        Мощный распределенный парсер.
        :param clients: Список авторизованных клиентов Telethon.
        :param log_func: Функция для отправки логов в БД (db_log).
        :param folder_path: Путь к папке пользователя (User Vault).
        """
        self.clients = clients
        self.bot = bot
        self.log = log_func
        self.folder = folder_path
        
        # Настройки фильтрации "на лету"
        self.filter_no_bots = True
        self.filter_active_only = True # Только те, кто был онлайн недавно
        self.active_days_limit = 30    # Порог "мертвости" аккаунта

    async def _join_target_safe(self, client, link):
        """
        Умный алгоритм входа в чат/канал.
        Определяет тип ссылки (публичная, приватная, хэш) и входит без ошибок.
        """
        try:
            # 1. Сначала пробуем просто получить сущность (вдруг уже вступили)
            try:
                entity = await client.get_entity(link)
                return entity
            except:
                pass # Идем дальше

            # 2. Обработка ссылок
            if "+" in link or "joinchat" in link:
                # Приватная ссылка (Invite Link)
                hash_arg = link.split("/")[-1].replace("+", "").replace("joinchat/", "").strip()
                try:
                    await client(functions.messages.ImportChatInviteRequest(hash=hash_arg))
                except errors.UserAlreadyParticipantError:
                    pass
            else:
                # Публичная ссылка
                clean_username = link.split("/")[-1].replace("@", "").strip()
                try:
                    await client(functions.channels.JoinChannelRequest(clean_username))
                except errors.UserAlreadyParticipantError:
                    pass
            
            # Даем серверу ТГ время на обработку входа
            await asyncio.sleep(2)
            return await client.get_entity(link)

        except Exception as e:
            # Логируем, но не крашимся. Вернем None, вызывающий код обработает.
            return None

    def _is_user_valid(self, user):
        """
        Фильтр качества аудитории.
        Возвращает True, если юзер подходит под критерии.
        """
        if not hasattr(user, 'username') or not user.username:
            return False # Нам нужны только юзернеймы для рассылки/инвайта

        if self.filter_no_bots and user.bot:
            return False

        if self.filter_active_only:
            # Проверка статуса (UserStatus)
            if isinstance(user.status, types.UserStatusOnline):
                return True
            if isinstance(user.status, types.UserStatusRecently):
                return True
            if isinstance(user.status, types.UserStatusLastWeek):
                return True
            if isinstance(user.status, types.UserStatusOffline):
                # Если был давно - пропускаем
                if user.status.was_online:
                    was_online = user.status.was_online.replace(tzinfo=None)
                    diff = datetime.utcnow() - was_online
                    if diff.days > self.active_days_limit:
                        return False
                else:
                    return False # Статус скрыт или очень давно
            
            # UserStatusEmpty или скрытый - считаем неактивным
            if isinstance(user.status, types.UserStatusEmpty):
                return False

        return True

    async def _worker_history_scan(self, client, entity, min_id, max_id, result_set):
        """
        Воркер для сканирования сообщений.
        Вытаскивает ID всех, кто писал в чате в заданном диапазоне.
        """
        count_local = 0
        try:
            # Итерируемся по сообщениям
            async for message in client.iter_messages(entity, min_id=min_id, max_id=max_id, limit=None):
                if message.sender and isinstance(message.sender, types.User):
                    if self._is_user_valid(message.sender):
                        result_set.add(f"@{message.sender.username}")
                        count_local += 1
        except Exception:
            pass
        return count_local

    async def _scrape_aggressive(self, client, entity, limit, result_set):
        """
        Агрессивный поиск участников (A-Z search).
        Позволяет обойти лимит в 200 участников в больших группах.
        """
        symbols = "abcdefghijklmnopqrstuvwxyz" # Можно добавить кириллицу
        total_found = 0
        
        # 1. Сначала берем "свежих" без поиска (стандартный метод)
        async for user in client.iter_participants(entity, limit=None):
            if self._is_user_valid(user):
                result_set.add(f"@{user.username}")
                total_found += 1
            if total_found >= limit: return

        # 2. Если мало - включаем поиск по буквам
        if total_found < limit:
            for char in symbols:
                if len(result_set) >= limit: break
                
                try:
                    async for user in client.iter_participants(entity, search=char, limit=None):
                        if self._is_user_valid(user):
                            result_set.add(f"@{user.username}")
                except:
                    pass # Ошибки доступа
                
                await asyncio.sleep(1) # Анти-флуд

    async def run_distributed_parsing(self, target_link, limit=1000, deep_scan=True):
        """
        ГЛАВНЫЙ МЕТОД ЗАПУСКА.
        :param target_link: Ссылка на чат
        :param limit: Сколько юзеров собрать
        :param deep_scan: Если True - сканирует историю сообщений (если участники скрыты)
        """
        if not self.clients:
            return 0

        # Используем первый клиент как "Лидера" для разведки
        leader = self.clients[0]
        # Используем ID лидера (предположительно User 1, но берем из объекта) 
        # для логов в БД. В данном контексте логика db_log принимает ID юзера.
        # Поскольку этот класс вызывается из task_parser, там ID юзера передается.
        # Но здесь мы используем self.log, который привязан к db_log(user_id, ...).
        # Чтобы не усложнять сигнатуру, мы просто передаем 0 или ID владельца, 
        # но лучше, если log_func уже обернута (partial). 
        # Предполагаем, что log_func принимает (user_id, level, msg).
        
        # Хардкод ID для лога внутри воркера - не идеально, поэтому будем просто логировать с ID 0 
        # (системный лог) или надеяться что caller смотрит TaskQueue.
        
        # Получаем доступ к чату
        entity = await self._join_target_safe(leader, target_link)
        if not entity:
            self.log(0, "ERROR", f"Parser: Access denied to {target_link}")
            return 0

        collected_users = set()
        method_used = "Unknown"

        # === ЭТАП 1: ПАРСИНГ СПИСКА УЧАСТНИКОВ (MEMBERS) ===
        try:
            # Проверяем, видны ли участники
            # Если это канал - participants вернут 0 (если мы не админ)
            # Если это чат со скрытыми участниками - вернет ошибку или 0
            self.log(0, "INFO", "Trying to scrape Member List (Aggressive mode)...")
            
            start_len = len(collected_users)
            await self._scrape_aggressive(leader, entity, limit, collected_users)
            
            if len(collected_users) > 0:
                method_used = "Participants List"
                self.log(0, "INFO", f"Scraped {len(collected_users)} from Members List.")

        except errors.ChatAdminRequiredError:
            self.log(0, "WARNING", "Members hidden by admins. Switching to History Mode.")
        except Exception as e:
            self.log(0, "WARNING", f"Members scrape error: {e}")

        # === ЭТАП 2: РАСПРЕДЕЛЕННЫЙ СКАН ИСТОРИИ (FALLBACK) ===
        # Если участников скрыли или их мало, а deep_scan включен
        if deep_scan and len(collected_users) < (limit * 0.1): # Если собрали менее 10% от желаемого
            method_used = "Deep History Scan"
            self.log(0, "INFO", f"Activating Distributed History Scan ({len(self.clients)} workers)...")
            
            try:
                # Получаем ID последнего сообщения
                last_msgs = await leader.get_messages(entity, limit=1)
                if last_msgs:
                    top_id = last_msgs[0].id
                    
                    # Глубина сканирования: 10,000 сообщений вглубь
                    # Это позволяет найти актив за последние месяцы
                    scan_depth = 10000 
                    chunk_size = scan_depth // len(self.clients)
                    
                    tasks = []
                    
                    for i, worker in enumerate(self.clients):
                        # Расчет диапазона для воркера
                        w_max = top_id - (i * chunk_size)
                        w_min = max(0, w_max - chunk_size)
                        
                        if w_max <= 0: break
                        
                        # Воркер должен вступить в чат, чтобы читать историю
                        await self._join_target_safe(worker, target_link)
                        
                        # Создаем задачу
                        tasks.append(
                            self._worker_history_scan(worker, entity, w_min, w_max, collected_users)
                        )
                    
                    # Ждем выполнения всех
                    await asyncio.gather(*tasks)
            except Exception as e:
                self.log(0, "ERROR", f"History scan failed: {e}")

        # === ЭТАП 3: СОХРАНЕНИЕ И СЛИЯНИЕ ===
        save_path = os.path.join(self.folder, USER_FILES["prs"])
        
        # Чтение существующей базы (Anti-Duplicate)
        existing_users = set()
        if os.path.exists(save_path):
            try:
                with open(save_path, "r", encoding="utf-8") as f:
                    for line in f:
                        l = line.strip()
                        if l: existing_users.add(l)
            except: pass

        # Объединение
        before_count = len(existing_users)
        existing_users.update(collected_users)
        new_added = len(existing_users) - before_count
        
        # Запись
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("\n".join(existing_users))

        self.log(0, "SUCCESS", f"Parsing Done via {method_used}. Total in DB: {len(existing_users)} (+{new_added} new).")
        return len(collected_users)