import asyncio
import random
import os
import time
from telethon import functions, types, errors
from config import USER_FILES, REACTIONS

# Попытка импорта сценариев, если файла нет
try:
    from scen_data import WARMUP_SCRIPTS
except ImportError:
    WARMUP_SCRIPTS = []

class AccountWarmer:
    def __init__(self, clients, log_func):
        """
        :param clients: Список активных сессий (Telethon Client objects)
        :param log_func: Функция для записи логов в БД (db_log)
        """
        self.clients = clients
        self.log = log_func

    async def _join_chat_if_needed(self, client, chat_entity):
        """Проверка и вступление в чат прогрева"""
        try:
            # Пытаемся получить диалог, чтобы убедиться, что мы там
            # Если нет - просто джойнимся (хотя обычно юзер сам добавляет ботов)
            pass 
        except:
            pass

    async def _human_typing(self, client, entity):
        """Имитация набора текста"""
        try:
            async with client.action(entity, 'typing'):
                await asyncio.sleep(random.uniform(3, 8))
        except:
            await asyncio.sleep(2)

    async def _add_reactions(self, message, entity, current_speaker_index):
        """
        Другие боты ставят реакции на сообщение.
        current_speaker_index: индекс бота, который написал сообщение (чтобы он сам себе не ставил)
        """
        # Берем 1-3 случайных бота из списка (кроме говорящего)
        potential_reactors = [c for i, c in enumerate(self.clients) if i != current_speaker_index]
        
        if not potential_reactors:
            return

        # Выбираем сколько ботов отреагирует (от 0 до 2)
        count = random.randint(0, min(2, len(potential_reactors)))
        reactors = random.sample(potential_reactors, count)

        for bot in reactors:
            try:
                # Пауза перед реакцией (человек читает)
                await asyncio.sleep(random.uniform(2, 10))
                
                # Выбор эмодзи
                emoji = random.choice(REACTIONS)
                
                # Отправка реакции
                await bot(functions.messages.SendReactionRequest(
                    peer=entity,
                    msg_id=message.id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)]
                ))
            except Exception:
                pass

    async def run_scenario(self, chat_link, user_folder_path, duration=600, task_id=None, session_maker=None):
        """
        Запуск цикла прогрева.
        :param chat_link: Ссылка на чат или ID
        :param user_folder_path: Путь к папке юзера (для чтения фраз)
        :param duration: Время работы в секундах
        :param task_id: ID задачи для проверки статуса (остановка)
        :param session_maker: Фабрика сессий БД для проверки флага stop
        """
        if not self.clients:
            return

        # 1. Загрузка фраз
        phrases = []
        warm_file = os.path.join(user_folder_path, USER_FILES["warm"])
        
        if os.path.exists(warm_file):
            with open(warm_file, "r", encoding="utf-8") as f:
                phrases = [line.strip() for line in f if line.strip()]
        
        # Если файл пуст, используем дефолтные фразы (чтобы процесс не падал)
        if not phrases:
            phrases = [
                "Всем привет!", "Как дела?", "Работаем?", "Где актив?", 
                "Крипта растет", "Кто тут?", "Доброе утро", "На связи", 
                "Скиньте инфу", "Ждем новостей", "Погнали", "🚀", "🔥"
            ]
            self.log(0, "WARNING", "Warmup file empty. Using default phrases.")

        # 2. Получение сущности чата (через первого бота)
        try:
            main_client = self.clients[0]
            if "t.me" in chat_link or "@" in chat_link:
                entity = await main_client.get_entity(chat_link)
            else:
                # Если передан int ID
                entity = await main_client.get_entity(int(chat_link))
        except Exception as e:
            self.log(0, "ERROR", f"Warmup: Can't access chat {chat_link}. {e}")
            return

        end_time = time.time() + duration
        self.log(0, "INFO", f"Warmup initialized. Duration: {duration}s. Chat: {chat_link}")

        # 3. Основной цикл
        last_msg_id = None
        
        while time.time() < end_time:
            # Проверка отмены задачи (если переданы параметры для проверки)
            if task_id and session_maker:
                db = session_maker()
                from models import TaskQueue
                t = db.query(TaskQueue).get(task_id)
                status = t.status if t else 'stopped'
                db.close()
                if status == 'stopped':
                    self.log(0, "WARNING", "Warmup stopped manually.")
                    break

            # Выбор говорящего
            speaker_idx = random.randrange(len(self.clients))
            speaker = self.clients[speaker_idx]
            
            # Выбор фразы
            text = random.choice(phrases)
            
            try:
                # Печатает...
                await self._human_typing(speaker, entity)
                
                # Вероятность ответа на предыдущее сообщение (Reply) - 30%
                reply_to = last_msg_id if (last_msg_id and random.random() < 0.3) else None
                
                # Отправка
                msg = await speaker.send_message(entity, text, reply_to=reply_to)
                last_msg_id = msg.id
                
                # Запуск фоновой задачи на реакции (fire and forget)
                asyncio.create_task(self._add_reactions(msg, entity, speaker_idx))
                
            except errors.FloodWaitError as e:
                self.log(0, "WARNING", f"FloodWait on bot #{speaker_idx}: {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                # self.log(0, "ERROR", f"Msg error: {e}") # Слишком много шума
                pass

            # Случайная задержка между сообщениями (15 - 45 сек)
            # Чтобы выглядело натурально
            delay = random.uniform(15, 45)
            await asyncio.sleep(delay)