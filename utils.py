import os
import time
import sys

# Попытка импорта colorama, если нет - заглушка
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    
    # Заглушки классов, чтобы код не падал
    class Fore:
        BLUE = ""
        GREEN = ""
        RED = ""
        YELLOW = ""
        CYAN = ""
        MAGENTA = ""
        WHITE = ""
        LIGHTBLACK_EX = ""
        
    class Style:
        RESET_ALL = ""

# --- ЦВЕТОВАЯ ПАЛИТРА ---
C_TITLE = Fore.MAGENTA
C_BORDER = Fore.LIGHTBLACK_EX
C_TEXT = Fore.CYAN
C_ACCENT = Fore.RED
C_MENU_BORDER = Fore.YELLOW
C_MENU_TEXT = Fore.GREEN
C_RESET = Style.RESET_ALL

def clear_screen(): 
    """Очистка экрана (поддержка Unix/Windows)"""
    os.system('cls' if os.name == 'nt' else 'clear')

def draw_border(title="", width=70):
    """Метод отрисовки разделителя"""
    if not title:
        return f"{C_BORDER}╠{'═'*width}╣{C_RESET}"
    else:
        text_len = len(title) + 2
        dashes = (width - text_len) // 2
        # Корректировка ширины для четности
        return f"{C_BORDER}╠{'═'*dashes} {C_ACCENT}{title}{C_BORDER} {'═'*(width - dashes - text_len)}╣{C_RESET}"

def print_header(stats=None):
    """Вывод логотипа и показателей системы"""
    if stats is None: stats = {}
    clear_screen()
    
    logo = f"""{C_TITLE}
 ██████╗ ██████╗  █████╗  ██████╗  ██████╗ ███╗   ██╗
 ██╔══██╗██╔══██╗██╔══██╗██╔════╝ ██╔═══██╗████╗  ██║
 ██║  ██║██████╔╝███████║██║  ███╗██║   ██║██╔██╗ ██║
 ██║  ██║██╔══██╗██╔══██║██║   ██║██║   ██║██║╚██╗██║
 ██████╔╝██║  ██║██║  ██║╚██████╔╝╚██████╔╝██║ ╚████║
 ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝
    {C_TEXT}DRAGON CORE ENTERPRISE v7.0 SaaS EDITION
    """
    
    print(f"{C_BORDER}╔{'═'*68}╗")
    print(logo)
    print(f"{C_BORDER}╠{'═'*68}╣")
    
    if stats:
        active_s = stats.get('sessions', 0)
        uptime = stats.get('uptime', '00:00:00')
        tasks = stats.get('tasks', 0)
        
        print(f" {C_TEXT}SESSIONS: {Fore.YELLOW}{active_s} {C_TEXT}| TASKS: {Fore.GREEN}{tasks} {C_TEXT}| UPTIME: {Fore.BLUE}{uptime}")
    
    print(f"{C_BORDER}╚{'═'*68}╝{C_RESET}")

def print_status(message, status="info"):
    """
    Атомарная функция вывода статуса процессов в консоль.
    :param message: Текст сообщения
    :param status: Тип сообщения (info, success, error, warning, system)
    """
    colors = {
        "info": Fore.BLUE, 
        "success": Fore.GREEN, 
        "error": Fore.RED, 
        "warning": Fore.YELLOW,
        "system": Fore.MAGENTA
    }
    
    icons = {
        "info": "ℹ️", 
        "success": "✅", 
        "error": "❌", 
        "warning": "⚠️",
        "system": "⚙️"
    }
    
    t = time.strftime("%H:%M:%S")
    ic = icons.get(status, "🔹")
    c = colors.get(status, Fore.WHITE)
    
    # Форматированный вывод
    print(f"{C_BORDER}[{t}] {c}{ic} {message}{C_RESET}")