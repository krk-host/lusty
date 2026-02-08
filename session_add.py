import asyncio
import os
import shutil
from telethon import TelegramClient, events, Button, errors
from sqlalchemy.orm import Session
from config import API_ID, API_HASH, ADMIN_IDS
from models import SessionLocal, User, SessionConfig

class SessionManager:
    def __init__(self, kernel_core):
        self.core = kernel_core
        # Кэш авторизации: {tg_user_id: {client, phone, code_buffer, user_db_id}}
        self.auth_cache = {} 

    def get_ios_keypad(self):
        """
        Генерирует инлайн-клавиатуру в стиле iOS для ввода цифр.
        Это позволяет вводить код подтверждения нажатиями, не отправляя его текстом.
        """
        return [
            [Button.inline("1", b"d_1"), Button.inline("2", b"d_2"), Button.inline("3", b"d_3")],
            [Button.inline("4", b"d_4"), Button.inline("5", b"d_5"), Button.inline("6", b"d_6")],
            [Button.inline("7", b"d_7"), Button.inline("8", b"d_8"), Button.inline("9", b"d_9")],
            [Button.inline("⬅️ Del", b"d_clr"), Button.inline("0", b"d_0"), Button.inline("OK ✅", b"d_done")]
        ]

    async def bot_start_login(self, event):
        """
        Начало процесса добавления аккаунта.
        Срабатывает на команду или кнопку в боте.
        """
        sender_id = event.sender_id
        
        # Простая проверка: если пишет Админ, сохраняем в его папку (ID 1).
        # В полной SaaS версии здесь должна быть привязка Telegram ID -> User ID.
        # Пока сохраняем для первого пользователя (Admin) или ищем по TG ID.
        
        target_user_id = 1 # По умолчанию Root Admin
        
        async with self.core.admin_bot.conversation(sender_id) as conv:
            await conv.send_message(
                "📞 **Режим регистрации сессии.**\n"
                "Введите номер телефона добавляемого аккаунта\n"
                "(в формате +7999...):"
            )
            phone_res = await conv.get_response()
            phone = phone_res.text.strip().replace(" ", "")

            # Создаем временную папку для инициализации
            temp_path = os.path.join("temp_sessions")
            if not os.path.exists(temp_path): os.makedirs(temp_path)
            
            temp_session_file = os.path.join(temp_path, f"temp_{sender_id}_{phone}")
            
            # Инициализируем клиент
            client = TelegramClient(temp_session_file, API_ID, API_HASH)
            await client.connect()
            
            # Сохраняем состояние
            self.auth_cache[sender_id] = {
                "cl": client,
                "p": phone,
                "c": "", # Буфер для кода
                "uid": target_user_id
            }
            
            try:
                # Запрос кода от Telegram
                await client.send_code_request(phone)
                
                await conv.send_message(
                    f"🔒 Код отправлен на номер `{phone}`.\n"
                    "**Введите код, используя кнопки ниже:**",
                    buttons=self.get_ios_keypad()
                )
            except errors.PhoneNumberInvalidError:
                await conv.send_message("❌ Неверный формат номера. Попробуйте снова.")
                await client.disconnect()
            except errors.FloodWaitError as e:
                await conv.send_message(f"❌ Флуд-контроль. Подождите {e.seconds} секунд.")
                await client.disconnect()
            except Exception as e:
                await conv.send_message(f"❌ Ошибка инициализации: {e}")
                await client.disconnect()

    async def keypad_handler(self, event):
        """Обработка нажатий на виртуальную клавиатуру"""
        uid = event.sender_id
        data = event.data.decode()
        
        if uid not in self.auth_cache: 
            await event.answer("Сессия истекла. Начните заново.", alert=True)
            return

        state = self.auth_cache[uid]
        
        if data == "d_done":
            # Подтверждение ввода
            await event.delete()
            await self._execute_signin(uid, event)
            
        elif data == "d_clr":
            # Очистка
            state["c"] = ""
            await event.edit(
                f"🔒 Код очищен. Введите заново:", 
                buttons=self.get_ios_keypad()
            )
            
        elif data.startswith("d_"):
            # Ввод цифры
            digit = data.split("_")[1]
            state["c"] += digit
            
            # Визуальное отображение (звездочки)
            mask = "• " * len(state["c"])
            await event.edit(
                f"🔒 Ввод кода: {mask}\n(Нажмите OK, когда введете весь код)", 
                buttons=self.get_ios_keypad()
            )

    async def _execute_signin(self, uid, event):
        """Попытка входа с введенным кодом"""
        state = self.auth_cache[uid]
        client = state["cl"]
        phone = state["p"]
        code = state["c"]
        
        try:
            await client.sign_in(phone, code)
            await self._finalize_success(uid)
            
        except errors.SessionPasswordNeededError:
            # Требуется 2FA пароль
            async with self.core.admin_bot.conversation(uid) as conv:
                await conv.send_message("🔐 Аккаунт защищен **2FA Паролем**.\nНапишите его в чат:")
                pwd_res = await conv.get_response()
                pwd = pwd_res.text.strip()
                
                try:
                    await client.sign_in(password=pwd)
                    await self._finalize_success(uid)
                except Exception as e:
                    await conv.send_message(f"❌ Ошибка 2FA: {e}")
                    await client.disconnect()
                    
        except errors.PhoneCodeInvalidError:
            await self.core.admin_bot.send_message(uid, "❌ Неверный код. Попробуйте снова.")
            # Можно сбросить state['c'] и показать клавиатуру снова, но проще перезапустить
            await client.disconnect()
            
        except Exception as e:
            await self.core.admin_bot.send_message(uid, f"❌ Ошибка входа: {e}")
            await client.disconnect()

    async def _finalize_success(self, uid):
        """Финализация: сохранение файла и запись в БД"""
        state = self.auth_cache[uid]
        client = state["cl"]
        phone = state["p"]
        user_db_id = state["uid"]
        
        # Получаем данные пользователя из БД, чтобы узнать путь к его папке
        db = SessionLocal()
        user = db.query(User).filter(User.id == user_db_id).first()
        
        if not user:
            await self.core.admin_bot.send_message(uid, "❌ Ошибка: Пользователь не найден в БД.")
            await client.disconnect()
            db.close()
            return
            
        # Формируем целевой путь
        clean_name = f"{phone.replace('+','')}.session"
        sessions_dir = os.path.join(user.folder_path, "sessions")
        if not os.path.exists(sessions_dir): os.makedirs(sessions_dir)
        
        target_path = os.path.join(sessions_dir, clean_name)
        
        # Сохраняем сессию (Telethon сохраняет при действиях, но форсируем сохранение)
        # Так как мы используем SQLite session storage (по умолчанию файл), 
        # нам нужно просто переместить файл сессии.
        
        # Получаем текущий путь файла
        # client.session.filename хранит путь
        current_session_path = client.session.filename
        
        # Отключаемся, чтобы освободить файл
        await client.disconnect()
        
        # Перемещение
        if os.path.exists(target_path):
            os.remove(target_path) # Удаляем старую, если есть
            
        shutil.move(current_session_path, target_path)
        
        # Удаляем временные файлы (-journal и т.д.)
        temp_dir = os.path.dirname(current_session_path)
        for f in os.listdir(temp_dir):
            if f.startswith(os.path.basename(current_session_path)):
                try: os.remove(os.path.join(temp_dir, f))
                except: pass
                
        # Регистрация в таблице SessionConfig
        existing_conf = db.query(SessionConfig).filter_by(user_id=user.id, filename=clean_name).first()
        if not existing_conf:
            new_conf = SessionConfig(user_id=user.id, filename=clean_name)
            db.add(new_conf)
            db.commit()
            msg = "✅ Сессия добавлена в базу и активирована!"
        else:
            existing_conf.is_active = True
            db.commit()
            msg = "✅ Сессия обновлена!"
            
        db.close()
        
        # Очистка кэша
        del self.auth_cache[uid]
        
        await self.core.admin_bot.send_message(
            uid, 
            f"{msg}\n📂 Файл: `{clean_name}`\n👤 Владелец ID: {user_db_id}"
        )