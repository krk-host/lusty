import os
import time
import logging
import json
from datetime import datetime
from typing import Optional
from hashlib import sha256

from fastapi import FastAPI, Request, Form, Depends, HTTPException, BackgroundTasks, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

# Импорт внутренних модулей системы
from config import (
    API_ID, API_HASH, VAULT_DIR, STATIC_DIR, TEMPLATES_DIR, 
    USER_FILES, START_TIME, WEB_PORT, WEB_HOST
)
from models import SessionLocal, User, SessionConfig, TaskQueue, SystemLog

# Настройка логирования для вывода в консоль Termux/Linux
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn")

app = FastAPI(title="Dragon Core Enterprise", version="7.0.0")

# SECURITY: Инициализация сессий через Cookies
# secret_key необходим для шифрования данных сессии на стороне клиента
app.add_middleware(
    SessionMiddleware, 
    secret_key="DRAGON_CORE_ULTRA_SECRET_KEY_V7_2024", 
    https_only=False
)

# Проверка и создание необходимых директорий перед запуском
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR, exist_ok=True)
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Монтирование статических файлов (CSS, JS, Images)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Инициализация движка шаблонов Jinja2
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# --- DEPENDENCIES (Зависимости для маршрутов) ---

def get_db():
    """Создание и закрытие сессии базы данных для каждого запроса"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Извлечение объекта текущего пользователя на основе сессии"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()

# --- AUTHENTICATION ROUTES (Авторизация и Регистрация) ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Отображение страницы входа"""
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_submit(
    request: Request, 
    username: str = Form(...), 
    password: str = Form(...), 
    db: Session = Depends(get_db)
):
    """Обработка формы входа с фиксом ошибки DateTime"""
    # Хеширование введенного пароля по алгоритму SHA-256
    pass_hash = sha256(password.encode()).hexdigest()
    
    # Поиск пользователя в базе данных
    user = db.query(User).filter(User.username == username, User.password_hash == pass_hash).first()
    
    if user:
        # Сохранение ID пользователя в зашифрованную сессию
        request.session["user_id"] = user.id
        
        # [FIX] Исправление ошибки Server Error:
        # Поле last_active в моделях имеет тип DateTime. SQLAlchemy требует объект datetime.
        # Запись float (time.time()) приводила к исключению OperationalError/TypeError.
        user.last_active = datetime.utcnow() 
        
        try:
            db.commit()
            logger.info(f"Identity Verified: User '{username}' logged in.")
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        except Exception as e:
            db.rollback()
            logger.error(f"Database commit error during login: {e}")
            return RedirectResponse(url="/login?error=db_error", status_code=status.HTTP_303_SEE_OTHER)
    else:
        logger.warning(f"Unauthorized access attempt: Username '{username}'.")
        return RedirectResponse(url="/login?error=invalid_credentials", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/register")
async def register_submit(
    request: Request, 
    username: str = Form(...), 
    password: str = Form(...), 
    db: Session = Depends(get_db)
):
    """Регистрация нового пользователя с созданием изолированного хранилища"""
    # Проверка на наличие дубликата в БД
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse(url="/login?error=user_exists", status_code=status.HTTP_303_SEE_OTHER)
    
    # Генерация пути к папке пользователя (SaaS Vault)
    # Имя папки: u_<timestamp>_<clean_username>
    safe_name = "".join(x for x in username if x.isalnum())
    folder_name = f"u_{int(time.time())}_{safe_name}"
    user_vault_path = os.path.join(VAULT_DIR, folder_name)
    
    try:
        # Создание физической структуры директорий
        os.makedirs(user_vault_path, exist_ok=True)
        os.makedirs(os.path.join(user_vault_path, "sessions"), exist_ok=True)
        
        # Создание предустановленных текстовых баз (согласно USER_FILES в config.py)
        for key in USER_FILES:
            file_name = USER_FILES[key]
            full_file_path = os.path.join(user_vault_path, file_name)
            with open(full_file_path, "w", encoding="utf-8") as f:
                f.write("") # Инициализация пустого файла
        
        # Создание записи пользователя в БД
        new_user = User(
            username=username,
            password_hash=sha256(password.encode()).hexdigest(),
            folder_path=user_vault_path,
            is_admin=(username.lower() == "admin"),
            created_at=datetime.utcnow(),
            last_active=datetime.utcnow()
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        # Автоматический вход в систему после успешной регистрации
        request.session["user_id"] = new_user.id
        logger.info(f"New User Registered: '{username}'. Vault created at {user_vault_path}")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    except Exception as e:
        db.rollback()
        logger.error(f"Registration critical failure: {e}")
        return HTMLResponse(content=f"Critical Error during registration: {e}", status_code=500)

@app.get("/logout")
async def logout(request: Request):
    """Завершение сессии пользователя"""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

# --- CORE DASHBOARD (Главная панель управления) ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Главная страница системы со статистикой и логами"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    # Сбор актуальных данных для отображения на дашборде
    # 1. Список сессий (аккаунтов) пользователя
    sessions = db.query(SessionConfig).filter(SessionConfig.user_id == user.id).all()
    active_sessions_count = len([s for s in sessions if s.is_active])
    
    # 2. Количество задач в очереди (ожидающие или в процессе)
    pending_tasks = db.query(TaskQueue).filter(
        TaskQueue.user_id == user.id, 
        TaskQueue.status.in_(['pending', 'processing'])
    ).count()
    
    # 3. Извлечение последних 20 записей системного лога
    logs = db.query(SystemLog).filter(SystemLog.user_id == user.id).order_by(SystemLog.timestamp.desc()).limit(20).all()
    
    # Расчет показателей аптайма и пинга (имитация для UI)
    current_uptime = time.strftime("%H:%M:%S", time.gmtime(time.time() - START_TIME))
    
    stats_package = {
        "active_sessions": active_sessions_count,
        "total_sessions": len(sessions),
        "uptime": current_uptime,
        "active_tasks": pending_tasks,
        "ping": 12 # ms
    }
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "stats": stats_package,
        "sessions_config": sessions,
        "logs": logs,
        "admin": user.is_admin
    })

# --- CONTROL API (Обработка AJAX запросов) ---

@app.post("/api/toggle_session")
async def api_toggle_session(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Включение/выключение прав доступа (RBAC) для конкретной сессии"""
    user = get_current_user(request, db)
    if not user: 
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    
    sess_id = payload.get("id")
    field_to_toggle = payload.get("field") # can_spam, can_parse, etc.
    
    # Поиск конфигурации сессии, принадлежащей именно этому пользователю
    session_record = db.query(SessionConfig).filter(
        SessionConfig.id == sess_id, 
        SessionConfig.user_id == user.id
    ).first()
    
    if session_record and hasattr(session_record, field_to_toggle):
        current_state = getattr(session_record, field_to_toggle)
        setattr(session_record, field_to_toggle, not current_state)
        db.commit()
        return JSONResponse({"status": "success", "new_state": not current_state})
    
    return JSONResponse({"error": "Configuration record not found"}, status_code=404)

@app.post("/api/update_settings")
async def api_update_settings(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Обновление глобальных параметров пользователя (ползунки в UI)"""
    user = get_current_user(request, db)
    if not user: 
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    
    try:
        # Массовое обновление полей на основе полученного JSON
        user.config_min_delay = int(payload.get("min_delay", 25))
        user.config_max_delay = int(payload.get("max_delay", 45))
        user.config_parse_limit = int(payload.get("parse_limit", 1000))
        user.config_threads = int(payload.get("threads", 3))
        user.config_humanize = bool(payload.get("humanize", True))
        
        db.commit()
        return JSONResponse({"status": "success"})
    except Exception as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/create_task")
async def api_create_task(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Создание новой задачи в очереди (TaskQueue) для ядра бота"""
    user = get_current_user(request, db)
    if not user: 
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    
    command_type = payload.get("command")
    target_link = payload.get("target", "")
    
    if not command_type:
        return JSONResponse({"error": "Command type is missing"}, status_code=400)
    
    # Добавление задачи в БД. Ядро (main.py) подхватит её автоматически в своем цикле.
    new_task = TaskQueue(
        user_id=user.id,
        command=command_type,
        target_value=target_link,
        status='pending',
        created_at=time.time()
    )
    db.add(new_task)
    
    # Системное уведомление в лог
    log_entry = SystemLog(
        user_id=user.id,
        level="INFO",
        message=f"Web-UI: Task '{command_type}' queued.",
        timestamp=time.time()
    )
    db.add(log_entry)
    
    db.commit()
    return JSONResponse({"status": "queued"})

@app.get("/api/get_logs")
async def api_get_logs(request: Request, db: Session = Depends(get_db)):
    """Получение свежих логов для обновления терминала без перезагрузки страницы"""
    user = get_current_user(request, db)
    if not user: 
        return JSONResponse([], status_code=401)
    
    logs_data = db.query(SystemLog).filter(SystemLog.user_id == user.id).order_by(SystemLog.timestamp.desc()).limit(20).all()
    
    # Форматирование для JS (старые -> новые для терминала)
    response_logs = []
    for log in reversed(logs_data):
        response_logs.append({
            "time": time.strftime("%H:%M:%S", time.gmtime(log.timestamp)),
            "message": log.message,
            "level": log.level
        })
    
    return JSONResponse(response_logs)

# --- FILE MANAGER & EDITOR (Управление базами данных) ---

@app.get("/editor", response_class=HTMLResponse)
async def editor_ui(request: Request, filename: Optional[str] = None, db: Session = Depends(get_db)):
    """Интерфейс текстового редактора для TXT баз пользователя"""
    user = get_current_user(request, db)
    if not user: 
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    # Защита: разрешаем открывать только файлы, прописанные в USER_FILES
    allowed_files = list(USER_FILES.values())
    if not filename or filename not in allowed_files:
        filename = allowed_files[0] # По умолчанию открываем raskid.txt
        
    file_full_path = os.path.join(user.folder_path, filename)
    file_content = ""
    
    if os.path.exists(file_full_path):
        try:
            with open(file_full_path, "r", encoding="utf-8") as f:
                file_content = f.read()
        except Exception as e:
            file_content = f"Error reading file: {e}"
    else:
        file_content = "File does not exist in user vault."
            
    return templates.TemplateResponse("editor.html", {
        "request": request,
        "current_filename": filename,
        "content": file_content,
        "user": user,
        "file_map": USER_FILES
    })

@app.post("/save_file")
async def editor_save(
    request: Request, 
    filename: str = Form(...), 
    content: str = Form(...), 
    db: Session = Depends(get_db)
):
    """Сохранение отредактированного текстового файла"""
    user = get_current_user(request, db)
    if not user: 
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    
    # Валидация имени файла
    if filename in USER_FILES.values():
        target_path = os.path.join(user.folder_path, filename)
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"User '{user.username}' updated file: {filename}")
        except Exception as e:
            logger.error(f"Failed to save file {filename}: {e}")
            
    return RedirectResponse(url=f"/editor?filename={filename}", status_code=status.HTTP_303_SEE_OTHER)

# --- SYSTEM INITIALIZATION ---
# Веб-сервер запускается как часть Dragon Kernel в отдельном потоке.
# Конфигурация хоста и порта берется из файла config.py.