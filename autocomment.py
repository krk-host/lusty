import asyncio
import random
import os
import time
from telethon import events, errors, types
from config import USER_FILES

class AutoCommenter:
    def __init__(self, clients, log_func):
        """
        :param clients: Список доступных клиентов (сессий)
        :param log_func: Функция логирования (db_log)
        """
        self.clients = clients
        self.log = log_func
        self.active_observers = [] # Хранит ссылки на запущенные наблюдатели

    async def start_observer(self, user_id, user_folder_path):
        """
        Запускает процесс отслеживания новых постов.
        """
        if not self.clients:
            self.log(user_id, "ERROR", "AutoComment: No active sessions.")
            return

        # 1. Чтение целей (каналов)
        target_file = os.path.join(user_folder_path, USER_FILES["trg"])
        if not os.path.exists(target_file):
            self.log(user_id, "ERROR", "AutoComment: channelstarget.txt not found.")
            return

        targets = []
        with open(target_file, "r", encoding="utf-8") as f:
            # Чистим ссылки, оставляем юзернеймы или ID
            for line in f:
                l = line.strip()
                if not l: continue
                # Превращаем ссылки в юзернеймы для Telethon
                if "t.me/" in l:
                    l = l.split("t.me/")[-1].replace("+", "joinchat/")
                targets.append(l)

        # 2. Чтение базы комментариев
        comments_file = os.path.join(user_folder_path, USER_FILES["com"])
        if not os.path.exists(comments_file):
            self.log(user_id, "ERROR", "AutoComment: comments.txt not found.")
            return

        comments = []
        with open(comments_file, "r", encoding="utf-8") as f:
            comments = [l.strip() for l in f if l.strip()]

        if not targets or not comments:
            self.log(user_id, "ERROR", "AutoComment: Targets or Comments file is empty.")
            return

        # 3. Настройка Наблюдателя
        # Используем первый аккаунт как "Глаза", остальные как "Руки"
        observer_client = self.clients[0]
        
        # Предварительное разрешение (Resolve) целей, чтобы event handler работал корректно
        # Telethon требует, чтобы сущности были в кэше
        self.log(user_id, "INFO", f"AutoComment: Resolving {len(targets)} targets...")
        resolved_chats = []
        for t in targets:
            try:
                entity = await observer_client.get_entity(t)
                resolved_chats.append(entity)
            except:
                self.log(user_id, "WARNING", f"Could not find target: {t}")
        
        if not resolved_chats:
            self.log(user_id, "ERROR", "No valid targets resolved.")
            return

        self.log(user_id, "SUCCESS", f"Observer started on {len(resolved_chats)} channels.")

        # 4. Обработчик событий (New Message)
        @observer_client.on(events.NewMessage(chats=resolved_chats))
        async def comment_handler(event):
            # Проверяем, что это пост в канале, а не реплаи или личка
            if not event.is_channel or event.is_reply:
                return

            # Не комментируем свои же сообщения
            if event.sender_id == (await observer_client.get_me()).id:
                return

            try:
                # Пауза перед комментарием (имитация чтения)
                # Слишком быстро = бан. Слишком медленно = нет трафика.
                # Оптимально: 4-10 секунд.
                wait_time = random.uniform(4, 10)
                await asyncio.sleep(wait_time)

                # Выбираем Исполнителя (случайного бота из списка)
                # Желательно не использовать самого Наблюдателя для спама, чтобы он жил дольше
                worker_pool = self.clients[1:] if len(self.clients) > 1 else self.clients
                worker = random.choice(worker_pool)
                
                comment_text = random.choice(comments)

                # Отправка комментария
                # comment_to=event.id - это ключевой параметр, отправляет в обсуждение
                await worker.send_message(
                    entity=event.chat_id,
                    message=comment_text,
                    comment_to=event.id 
                )
                
                self.log(user_id, "SUCCESS", f"Commented on post in {event.chat.title}")

            except errors.ChatWriteForbiddenError:
                # Комментарии закрыты в канале
                # self.log(user_id, "WARNING", f"Comments disabled in {event.chat_id}")
                pass
            except errors.FloodWaitError as e:
                self.log(user_id, "WARNING", f"FloodWait {e.seconds}s on worker.")
            except Exception as e:
                self.log(user_id, "ERROR", f"Comment failed: {e}")

        # Держим ссылку на хендлер, чтобы сборщик мусора не удалил (хотя в Telethon это не обязательно)
        self.active_observers.append(comment_handler)
        
        # Поскольку этот метод вызывается асинхронно, он завершится, 
        # но event handler останется висеть на client.
        # Чтобы остановить, нужно будет client.remove_event_handler(...)
        # В текущей архитектуре остановка делается через stop_all (перезапуск ядра)