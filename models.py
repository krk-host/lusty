from sqlalchemy import create_engine, Column, Integer, String, Boolean, Float, Text, ForeignKey, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import time
import os
from config import DB_NAME

Base = declarative_base()

class User(Base):
    """
    Таблица пользователей системы SaaS.
    Хранит учетные данные, путь к файловому хранилищу, статистику
    и детальные настройки конфигурации (привязаны к UI сайта).
    """
    __tablename__ = 'users'
    
    # === ИДЕНТИФИКАЦИЯ ===
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    is_admin = Column(Boolean, default=False) # Root права
    is_banned = Column(Boolean, default=False) # Блокировка доступа к SaaS
    
    # Путь к изолированной папке пользователя (SaaS структура)
    # Пример: .../users_vault/user_174543_admin/
    folder_path = Column(String(255), nullable=False)
    
    # === СТАТИСТИКА (GLOBAL COUNTERS) ===
    sent_count = Column(Integer, default=0)        # Всего сообщений отправлено
    failed_count = Column(Integer, default=0)      # Ошибки отправки
    parsed_count = Column(Integer, default=0)      # Спарсено пользователей
    warmup_minutes = Column(Integer, default=0)    # Минут прогрева аккаунтов
    
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    
    # === РАСШИРЕННЫЕ НАСТРОЙКИ (SETTINGS SLIDERS) ===
    # Эти поля управляются через API /api/update_settings
    
    # -- Настройки СПАМА --
    config_min_delay = Column(Integer, default=25)      # Мин. задержка (сек)
    config_max_delay = Column(Integer, default=45)      # Макс. задержка (сек)
    config_humanize = Column(Boolean, default=True)     # Эмуляция "печатает..."
    config_stop_at_night = Column(Boolean, default=False) # Пауза ночью
    config_loop_wait = Column(Float, default=0.5)       # Скорость цикла ядра
    
    # -- Настройки ПАРСЕРА --
    config_parse_limit = Column(Integer, default=1000)  # Лимит парсинга за 1 заход
    config_parse_depth = Column(Boolean, default=True)  # Глубокий анализ истории чата
    config_parse_members = Column(Boolean, default=True) # Парсить список участников
    
    # -- Настройки СИСТЕМЫ --
    config_threads = Column(Integer, default=3)         # Кол-во одновременных потоков (воркеров)
    config_auto_answer = Column(Boolean, default=False) # Автоответчик (beta)
    
    # === СВЯЗИ ===
    # Cascade delete-orphan означает, что при удалении юзера удаляется всё его имущество
    sessions_config = relationship("SessionConfig", back_populates="owner", cascade="all, delete-orphan")
    tasks = relationship("TaskQueue", back_populates="owner", cascade="all, delete-orphan")
    logs = relationship("SystemLog", back_populates="owner", cascade="all, delete-orphan")

class SessionConfig(Base):
    """
    Управление правами конкретной сессии (файла .session).
    Позволяет администратору включать/отключать функции для конкретного аккаунта.
    """
    __tablename__ = 'session_configs'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    filename = Column(String(100), nullable=False) # Имя файла (напр. 79991234567.session)
    
    # Права доступа (Capabilities Switches)
    can_spam = Column(Boolean, default=True)   # Разрешено участие в рассылке
    can_parse = Column(Boolean, default=True)  # Разрешено использование для парсинга
    can_warm = Column(Boolean, default=True)   # Разрешено участие в прогреве
    can_invite = Column(Boolean, default=True) # Разрешен инвайтинг
    
    # Статус
    is_active = Column(Boolean, default=True)  # Глобальный переключатель (Вкл/Выкл)
    last_used = Column(Float, default=0.0)     # Timestamp последнего использования
    
    owner = relationship("User", back_populates="sessions_config")

class TaskQueue(Base):
    """
    Очередь задач (Command & Control).
    Web Server пишет сюда задачу -> Main Loop ядра читает и исполняет.
    Обеспечивает асинхронную связь между сайтом и Телеграм-движком.
    """
    __tablename__ = 'task_queue'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    
    # Тип задачи: 
    # 'spam_chat', 'spam_dm', 'parse', 'warmup', 'stop_all'
    command = Column(String(50), nullable=False)
    
    # Аргументы задачи (например, ссылка на чат или настройки)
    target_value = Column(String(500), nullable=True) 
    amount = Column(Integer, default=0) # Количественный параметр (сколько сообщений/минут)
    
    # Статус выполнения: 'pending', 'processing', 'completed', 'failed', 'stopped'
    status = Column(String(20), default='pending')
    
    created_at = Column(Float, default=time.time)
    updated_at = Column(Float, default=time.time)
    
    owner = relationship("User", back_populates="tasks")

class SystemLog(Base):
    """
    Таблица логов.
    Используется для вывода в консоль на сайте в реальном времени.
    """
    __tablename__ = 'system_logs'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    
    level = Column(String(10), default="INFO") # INFO, ERROR, WARNING, SUCCESS
    message = Column(Text, nullable=False)
    timestamp = Column(Float, default=time.time)
    
    owner = relationship("User", back_populates="logs")

# === ИНИЦИАЛИЗАЦИЯ ДВИЖКА БД ===
# check_same_thread=False обязателен для SQLite при работе с FastAPI + Telethon (разные потоки)
engine = create_engine(f'sqlite:///{DB_NAME}', pool_recycle=3600, connect_args={'check_same_thread': False})

# Создание таблиц
Base.metadata.create_all(engine)

# Фабрика сессий
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Глобальный объект сессии (для простых скриптов)
db_session = SessionLocal()