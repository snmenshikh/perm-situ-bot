import json
import sys
sys.path.insert(0, 'vendor')
import requests
import time
import re
import uuid
from typing import Dict, List, Optional
from threading import Lock
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, unquote
import base64


class GigaChatAPI:
    """Клиент для работы с API GigaChat"""
    
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        self.token_expires_at = 0
        self.auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        self.api_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    def get_token(self) -> Optional[str]:
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        credentials = f"{self.client_id}:{self.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()

        headers = {
            'Authorization': f'Basic {encoded}',
            'RqUID': str(uuid.uuid4()),
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {'scope': 'GIGACHAT_API_PERS'}
        last_exception = None

        for attempt in range(1, 4):
            try:
                response = requests.post(
                    self.auth_url,
                    headers=headers,
                    data=data,
                    verify=False,
                    timeout=(5, 15)  # connect / read
                )
                response.raise_for_status()

                result = response.json()
                self.access_token = result.get('access_token')
                self.token_expires_at = result.get('expires_at', 0) / 1000

                return self.access_token

            except Exception as e:
                last_exception = e
                time.sleep(2 * attempt)

        print(f"❌ Ошибка получения токена GigaChat: {last_exception}")
        return None
    
    def send_message(self, messages: List[Dict], temperature: float = 0.3) -> Optional[str]:
        """Отправляет сообщение в GigaChat и получает ответ"""
        token = self.get_token()
        if not token:
            return None
        
        try:
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'model': 'GigaChat',
                'messages': messages,
                'temperature': temperature,
                'max_tokens': 1024
            }
            
            response = requests.post(self.api_url, headers=headers, json=payload, verify=False, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            return result['choices'][0]['message']['content']
        except Exception as e:
            print(f"❌ Ошибка запроса к GigaChat: {e}")
            return None

class SITUBot:
    """Чат-бот для Сетевого ИТ-Университета (СИТУ)"""
    
    def __init__(self, access_token: str, api_token: str, api_url: str, 
                 gigachat_client_id: str, gigachat_client_secret: str,
                 qa_file: str = 'qa_data.json', 
                 knowledge_base_file: str = 'knowledge_base.json',
                 system_prompt_file: str = 'system_prompt.json',
                 universities_file: str = 'universities_info.json'):
        
        self.access_token = access_token
        self.api_token = api_token
        self.api_url = api_url
        self.base_url = 'https://platform-api.max.ru'
        self.vk_admin_url = 'https://vk.com/itedunetwork'
        self.qa_data = self.load_json_file(qa_file, 'вопросов и ответов')
        self.knowledge_base = self.load_json_file(knowledge_base_file, 'базы знаний')
        with open(system_prompt_file, "r", encoding="utf-8") as f:
            self.system_prompt = json.load(f)["system_message"]
            print(f"✅ Загружен системный промпт: {system_prompt_file}")
        self.universities = self.load_json_file(universities_file, 'ВУЗов')
        self.marker = None
        self.ai_violation_counter = {}
        self.ai_blocked = set()
        
        self.gigachat = GigaChatAPI(gigachat_client_id, gigachat_client_secret)
        
        self.chat_locks = defaultdict(Lock)
        self.processed_callbacks = set()
        self.callback_cleanup_time = {}
        self.last_cleanup = time.time()
        
        self.timezone_offset = timedelta(hours=5)
        
        self.user_states = {}
        self.ai_conversations = {}

        self.situsha_photo_id: Optional[str] = None
        
    def get_current_time(self) -> str:
        utc_now = datetime.utcnow()
        local_time = utc_now + self.timezone_offset
        return local_time.strftime("%Y-%m-%d %H:%M:%S")
    
    def print_separator(self):
        print("=" * 60)
        
    def load_json_file(self, filename: str, description: str) -> Dict:
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"✅ Загружен файл {description}: {filename}")
                return data
        except FileNotFoundError:
            print(f"⚠️  Файл {filename} ({description}) не найден.")
            return {}
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка парсинга JSON в файле {filename}: {e}")
            return {}
        except Exception as e:
            print(f"❌ Неожиданная ошибка при загрузке {filename}: {e}")
            return {}
    
    def search_in_knowledge_base(self, query: str) -> Optional[str]:
        query_lower = query.lower()
        
        for item in self.knowledge_base.get('items', []):
            keywords = item.get('keywords', [])
            if any(keyword.lower() in query_lower for keyword in keywords):
                return item.get('answer')
        
        return None
    
    def is_unrelated_question(self, text: str) -> bool:
        whitelist = {
            "ситу","сетевой","ит","университет","обучение","обучаться","учеба",
            "учиться","курс","курсы","программа","программы","профиль","профили",
            "направление","вуз","вузов","пгниу","универ","пнипу","политех",
            "вшэ","вышка","пермский","пермь","тест","экзамен","контроль","входной","входного",
            "регистрация","зачисление","поступление","слушатель","слушатели",
            "диплом","документ","удостоверение","образец","занятия","старт","начало","записаться",
            "повышение","квалификация","проект","цифровые","навыки","компетенции","аналитика",
            "программирование","данные","3d-моделирование","web-программирование",
            "управление","проектами","системное","администрирование",
            "спо","во","студент","студенты","академических","часов",
            "онлайн","офлайн","формат","форматы",
            "материалы","литература","практика","задания",
            "результат","рассылка","почта","email",
            "регистрационный",
            "сложность","уровень",
            "контакт","контакты","контактные","лица",
            "план","перечень","набор",
            "искусственный","интеллект",
            "цифровые","технологии",
            "квалификация","преподаватели","it","it-компаний",
            "начало программы","начало курса","начало обучения",
            "перечень программ","перечень курсов","перечень обучения",
            "как проходит обучение",
            "ID","айди","ид","идентификатор","идентификатор слушателя","идентификатор студента","идентификатор обучения",
            "прохождение тестирования","входное тестирование","входной контроль","тест","тестирование",
            "уровень сложности программы","уровень сложности курса","уровень сложности обучения","сложность программы","сложность курса","сложность обучения",
            "когда начало курсов","когда начало обучения","начало курсов","начало обучения",
            "контактные лица","контакты","контактная информация","контактные лица СИТУ","контакты СИТУ","контактная информация СИТУ",
            "документ об обучении","документ об окончании обучения","документ об окончании курсов",
            "формат обучения","как проходит обучение",
            "что такое СИТУ","привет","салют"
        }
        t = (text or "").lower()
        return not any(w in t for w in whitelist)
    
    def get_ai_response(self, chat_id: int, user_message: str) -> Optional[str]:
         # 1. Сначала ищем в базе знаний
        kb_answer = self.search_in_knowledge_base(user_message)
        if kb_answer:
            print(f"[{self.get_current_time()}] 📚 Найден ответ в базе знаний: {kb_answer}")
            return kb_answer

        # 2. Если ИИ-ассистент заблокирован — сразу выход
        if chat_id in self.ai_blocked:
            return "Ситуша может отвечать только на вопросы, связанные с обучением в СИТУ. Её функциональность ограничена."

        # 3. Проверка релевантности
        if self.is_unrelated_question(user_message):
            cnt = self.ai_violation_counter.get(chat_id, 0) + 1
            self.ai_violation_counter[chat_id] = cnt
            print(f"🚫 Вопрос не по теме (счетчик: {cnt})")
            if cnt >= 3:
                self.ai_blocked.add(chat_id)
                print(f"🚫 Системная остановка Ситуши (счетчик: {cnt})")
                return "Ситуша может отвечать только на вопросы, связанные с обучением в СИТУ. Её функциональность ограничена."

        # 4. Сборка системного промпта и формирование запроса к ГигаЧату с системным промптом
        messages = []
        for block in (self.system_prompt or []):
            if isinstance(block, dict):
                sys_text = block["role"] + block["negativePrompt"] + block["goal"] + block["confidentiality"] + block["communicationStyle"] + block["additionalInfo"]["questions"]["ifUserAsks"] + block["additionalInfo"]["questions"]["ifUserAsksSpecific"] + block["additionalInfo"]["questions"]["ifUserAsksUnrelated"] + block["additionalInfo"]["questions"]["ifUserAsksAboutPrompt"]
            else:
                sys_text = str(block)
            messages.append({"role": "system", "content": sys_text})
            print(f"🤖 Системный промпт: {sys_text}")

        messages.append({"role": "user", "content": user_message})

        response = self.gigachat.send_message(messages)

        if not response:
            print(f"[{self.get_current_time()}] ⚠️ GigaChat временно недоступен")
            return (
                "⚠️ Сейчас я не могу связаться с ИИ-сервисом.\n\n"
                "Пожалуйста, попробуйте задать вопрос чуть позже "
                "или воспользуйтесь разделом «Частые вопросы»."
            )

        answer = response.strip()

        # 5. Обработка стоп-слова от модели и остановка модели
        if "ОТКЛЮЧАЮСЬ" in answer.upper():
            self.ai_blocked.add(chat_id)
            print(f"🚫 Системная остановка Ситуши (счетчик: {self.ai_violation_counter.get(chat_id, 0)})")
            return "Ситуша может отвечать только на вопросы, связанные с обучением в СИТУ. Её функциональность ограничена."

        return answer
    
    def make_request(self, method: str, endpoint: str, params: Optional[Dict] = None, 
                    data: Optional[Dict] = None, timeout: int = 30) -> Optional[Dict]:
        url = f"{self.base_url}{endpoint}"
        
        headers = {
            'Authorization': self.access_token,
            'Content-Type': 'application/json'
        }
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=timeout)
            elif method == 'POST':
                response = requests.post(url, headers=headers, params=params, json=data, timeout=timeout)
            elif method == 'PUT':
                response = requests.put(url, headers=headers, params=params, json=data, timeout=timeout)
            else:
                raise ValueError(f"Неподдерживаемый метод: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.Timeout:
            print(f"⏱️  Таймаут запроса к {endpoint}")
            return None
        except requests.exceptions.ConnectionError as e:
            print(f"🔌 Ошибка соединения с {endpoint}: {e}")
            return None
        except requests.exceptions.HTTPError as e:
            print(f"❌ HTTP ошибка {response.status_code}: {e}")
            return None
        except Exception as e:
            print(f"❌ Ошибка при запросе к {endpoint}: {e}")
            return None
    
    def upload_image(self, image_path: str) -> Optional[str]:
        # 1. Получаем upload URL
        upload_init = self.make_request('POST', '/uploads', params={'type': 'image'})

        if not upload_init or 'url' not in upload_init:
            print("❌ Не удалось получить upload URL")
            return None

        upload_url = upload_init['url']

        # 2. Загружаем файл
        with open(image_path, 'rb') as f:
            response = requests.post(upload_url, files={'data': f}, timeout=30)
            response.raise_for_status()

        # 3. Извлекаем photo_id ИЗ URL
        parsed = urlparse(upload_url)
        query = parse_qs(parsed.query)

        if 'photoIds' not in query:
            print("❌ photoIds не найден в upload URL")
            return None

        photo_id = unquote(query['photoIds'][0])
        return photo_id

    def safe_get_request(self, url: str, headers: dict, params: Optional[Dict] = None, attempts: int = 3, connect_timeout: int = 5, read_timeout: int = 10):
        last_exception = None

        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=(connect_timeout, read_timeout))
                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                last_exception = e
                time.sleep(2 * attempt)

        raise RuntimeError("❌ Не удалось установить соединение с БД") from last_exception
    
    # Функция прогрева API
    def warmup_external_api(self):
        url = f"{self.api_url}/situ_bot_integration/requests"
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json'
        }

        try:
            self.safe_get_request(url=url, headers=headers, params={'phone': '0000000000'}, attempts=1, connect_timeout=3, read_timeout=3)
        except Exception:
            pass  # прогрев допускает ошибку

    def search_requests_by_phone(self, phone: str) -> Optional[List[Dict]]:
        url = f"{self.api_url}/situ_bot_integration/requests"
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json'
        }
        params = {'phone': phone}

        try:
            response = self.safe_get_request(url=url, headers=headers, params=params)
            data = response.json()
            return data.get('items', [])

        except Exception as e:
            print(f"[{self.get_current_time()}] ❌ Ошибка при запросе к БД: {e}")
            return None
    
    def validate_phone(self, phone: str) -> bool:
        return bool(re.match(r'^\d{10}$', phone.strip()))
    
    def format_date(self, date_str: Optional[str]) -> str:
        if not date_str:
            return "не указана"
        try:
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt.strftime("%d.%m.%Y")
        except:
            return date_str
    
    def format_request_info(self, request: Dict) -> str:
        number = request.get('number', 'не указан')
        status = request.get('status', 'не указан')
        org_name = request.get('organizationName', 'не указана')
        service_name = request.get('serviceName', 'не указана')
        service_form = request.get('serviceForm', 'не указана')
        date_start = self.format_date(request.get('dateLearnStart'))
        
        return (f"📋 Номер заявления: {number}\n"
                f"📊 Статус заявления: {status}\n"
                f"🏢 Полное название организации: {org_name}\n"
                f"📚 Название программы: {service_name}\n"
                f"💻 Форма обучения: {service_form}\n"
                f"📅 Дата начала обучения: {date_start}")
    
    def get_bot_info(self) -> Optional[Dict]:
        return self.make_request('GET', '/me')
    
    def create_main_keyboard(self, block_ai: bool = False) -> Dict:
        """
        Главное меню. Если block_ai=True, кнопка 'Задать вопрос Ситуше' скрывается.
        Обратная совместимость: можно вызывать без аргументов.
        """
        buttons = [
            [{'type': 'callback', 'text': '📝 Частые вопросы', 'payload': 'show_faq'}],
            [{'type': 'callback', 'text': '🎓 Получить информацию об обучении', 'payload': 'get_learning_info'}],
            [{'type': 'callback', 'text': '🏛️ Информация о ВУЗах-участниках', 'payload': 'show_universities'}],
        ]

        if not block_ai:
            buttons.append([{'type': 'callback', 'text': '🐶 Задать вопрос ИИ-ассистенту Ситуше', 'payload': 'ask_ai'}])

        buttons.append([{'type': 'link', 'text': '💬 Остались вопросы?', 'url': self.vk_admin_url}])
        buttons.append([{'type': 'callback', 'text': '👋 Завершить диалог', 'payload': 'end_dialog'}])

        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}

    def create_faq_keyboard(self) -> Dict:
        buttons = []
        
        for item in self.qa_data.get('questions', []):
            buttons.append([{'type': 'callback', 'text': item['question'], 'payload': item['id']}])
        
        buttons.append([{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}])
        
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def create_faq_answer_keyboard(self) -> Dict:
        buttons = [
            [{'type': 'callback', 'text': '📝 Частые вопросы', 'payload': 'show_faq'}],
            [{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}]
        ]
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def create_universities_keyboard(self) -> Dict:
        buttons = []
        
        for uni in self.universities.get('universities', []):
            buttons.append([{
                'type': 'link',
                'text': uni['name'],
                'url': uni['geopoint']
            }])
        
        buttons.append([{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}])
        
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def create_back_to_main_keyboard(self) -> Dict:
        buttons = [[{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}]]
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def show_screen(self, callback_id: Optional[str], chat_id: int, message_id: Optional[str],
                    text: str, keyboard: Optional[Dict] = None):
        attachments = [keyboard] if keyboard else []
        if callback_id:
            try:
                payload = {
                    "message": {
                        "text": text,
                        "attachments": attachments
                    }
                }
                self.make_request('POST', '/answers', params={'callback_id': callback_id}, data=payload)
                return
            except Exception as e:
                print(f"[{self.get_current_time()}] ⚠️ Ошибка при answer_callback: {e}")
        if message_id:
            try:
                self.edit_message(message_id, text, attachments)
                return
            except Exception as e:
                print(f"[{self.get_current_time()}] ⚠️ Ошибка при edit_message: {e}")
        self.send_message(chat_id, text, attachments if attachments else None)

    def create_ai_not_enough_keyboard(self) -> Dict:
        buttons = [
            [{'type': 'link', 'text': '💬 Остались вопросы?', 'url': self.vk_admin_url}],
            [{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}]
        ]
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def create_requests_navigation_keyboard(self, current_index: int, total: int) -> Dict:
        buttons = []
        
        if total > 1:
            nav_buttons = []
            
            if current_index > 0:
                nav_buttons.append({'type': 'callback', 'text': '⬅️ Предыдущее заявление', 'payload': 'request_prev'})
            
            if current_index < total - 1:
                nav_buttons.append({'type': 'callback', 'text': 'Следующее заявление ➡️', 'payload': 'request_next'})
            
            if nav_buttons:
                buttons.append(nav_buttons)
        
        buttons.append([{'type': 'callback', 'text': '🔙 Назад в главное меню', 'payload': 'back_to_main'}])
        
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}
    
    def edit_message(self, message_id: str, text: str, attachments: Optional[List[Dict]] = None) -> Optional[Dict]:
        if not message_id:
            return None
        
        message_data = {'text': text, 'attachments': attachments or []}
        params = {'message_id': message_id}
        return self.make_request('PUT', '/messages', params=params, data=message_data)
    
    def send_message(self, chat_id: int, text: str, attachments: Optional[List[Dict]] = None) -> Optional[Dict]:
        if not chat_id:
            return None
        
        message_data = {'text': text, 'attachments': attachments or []}
        params = {'chat_id': chat_id}
        return self.make_request('POST', '/messages', params=params, data=message_data)
    
    def _cleanup_old_callbacks(self, max_age: int = 300):
        current_time = time.time()
        to_remove = [cid for cid, timestamp in self.callback_cleanup_time.items() if current_time - timestamp > max_age]
        
        for callback_id in to_remove:
            self.processed_callbacks.discard(callback_id)
            del self.callback_cleanup_time[callback_id]
        
        if to_remove:
            print(f"🧹 Очищено {len(to_remove)} старых callback_id")
    
    def handle_callback(self, callback_id: str, payload: str, chat_id: int, message_id: Optional[str] = None):
        with self.chat_locks[chat_id]:
            if callback_id in self.processed_callbacks:
                print(f"[{self.get_current_time()}] ⚠️  Callback {callback_id} уже обработан")
                self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': ''})
                return
            
            self.processed_callbacks.add(callback_id)
            self.callback_cleanup_time[callback_id] = time.time()
            
            if time.time() - self.last_cleanup > 60:
                self._cleanup_old_callbacks()
                self.last_cleanup = time.time()
            
            print(f"[{self.get_current_time()}] 🔘 Обработка callback: '{payload}' (chat_id={chat_id}, msg_id={message_id})")
            
            try:
                if payload == 'end_dialog':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Завершение диалога...'})
                    print(f"[{self.get_current_time()}] 👋 Пользователь завершил диалог (chat_id={chat_id})")
                    text = "Спасибо Вам за обращение! Рады были помочь!"
                    if message_id:
                        self.show_screen(callback_id, chat_id, message_id, text, None)
                    else:
                        self.send_message(chat_id, text)
                    return
                
                if payload == 'get_learning_info':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Запрашиваю номер телефона...'})
                    print(f"[{self.get_current_time()}] 📱 Запрос информации об обучении (chat_id={chat_id})")
                    self.user_states[chat_id] = {'state': 'waiting_phone'}
                    text = "Пожалуйста, введите Ваш номер телефона в формате: 10 цифр без +7 или 8\n\nНапример: 9991234567"
                    time.sleep(0.3)
                    if message_id:
                        self.show_screen(callback_id, chat_id, message_id, text, None)
                    else:
                        self.send_message(chat_id, text)
                    return
                
                if payload == 'show_universities':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Открываю список ВУЗов...'})
                    print(f"[{self.get_current_time()}] 🏛️ Показ списка ВУЗов (chat_id={chat_id})")
                    text = "Выберите ВУЗ, чтобы посмотреть его местоположение:"
                    keyboard = self.create_universities_keyboard()
                    time.sleep(0.3)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    return
                
                if payload == 'ask_ai':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Запускаю Ситушу...'})
                    self.user_states[chat_id] = {'state': 'chatting_with_ai'}
                    text = ("Приветствую! Я Ситуша, помощник СИТУ!\n\n"
                        "Задавайте мне любые вопросы об обучении в СИТУ, "
                        "и я с радостью на них отвечу!") 
                    self.send_message(chat_id, text)
                    return
                
                if payload == 'show_faq':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Открываю частые вопросы...'})
                    print(f"[{self.get_current_time()}] 📋 Открытие FAQ (chat_id={chat_id})")
                    text = "Выберите интересующий Вас вопрос:"
                    keyboard = self.create_faq_keyboard()
                    time.sleep(0.3)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    return
                
                if payload == 'back_to_main':
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Возврат в главное меню...'})
                    print(f"[{self.get_current_time()}] 🔙 Возврат в главное меню (chat_id={chat_id})")
                    if chat_id in self.user_states:
                        del self.user_states[chat_id]
                    text = "Выберите нужный раздел:"
                    if chat_id in self.ai_blocked:
                        keyboard = self.create_main_keyboard(block_ai=True)
                    else:
                        keyboard = self.create_main_keyboard()
                    time.sleep(0.3)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    return
                
                if payload == 'request_prev':
                    user_state = self.user_states.get(chat_id)
                    if user_state and 'current_index' in user_state:
                        user_state['current_index'] = max(0, user_state['current_index'] - 1)
                        self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Загружаю предыдущее заявление...'})
                        
                        requests_list = user_state['requests']
                        current_index = user_state['current_index']
                        total = len(requests_list)
                        current_request = requests_list[current_index]
                        
                        text = f"По указанному Вами номеру телефона найдено заявлений на обучение: {total}\n\n"
                        text += f"Заявление {current_index + 1} из {total}:\n\n"
                        text += self.format_request_info(current_request)
                        keyboard = self.create_requests_navigation_keyboard(current_index, total)
                        
                        time.sleep(0.3)
                        self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                        self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    return
                
                if payload == 'request_next':
                    user_state = self.user_states.get(chat_id)
                    if user_state and 'current_index' in user_state:
                        max_index = len(user_state.get('requests', [])) - 1
                        user_state['current_index'] = min(max_index, user_state['current_index'] + 1)
                        self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Загружаю следующее заявление...'})
                        
                        requests_list = user_state['requests']
                        current_index = user_state['current_index']
                        total = len(requests_list)
                        current_request = requests_list[current_index]
                        
                        text = f"По указанному Вами номеру телефона найдено заявлений на обучение: {total}\n\n"
                        text += f"Заявление {current_index + 1} из {total}:\n\n"
                        text += self.format_request_info(current_request)
                        keyboard = self.create_requests_navigation_keyboard(current_index, total)
                        
                        time.sleep(0.3)
                        self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                        self.show_screen(callback_id, chat_id, message_id, text, keyboard)
                    return
                
                # Поиск ответа из FAQ
                answer = None
                question_text = None
                for item in self.qa_data.get('questions', []):
                    if item['id'] == payload:
                        answer = item.get('answer')
                        question_text = item.get('question')
                        break
                
                if answer:
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Загружаю информацию...'})
                    print(f"[{self.get_current_time()}] 💬 Ответ на вопрос '{question_text}'")
                    keyboard = self.create_faq_answer_keyboard()
                    time.sleep(0.3)
                    self.show_screen(callback_id, chat_id, message_id, answer, keyboard)
                    self.show_screen(callback_id, chat_id, message_id, answer, keyboard)
                else:
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Информация не найдена.'})
                    
            except Exception as e:
                print(f"[{self.get_current_time()}] ❌ Ошибка обработки callback {callback_id}: {e}")
                try:
                    self.make_request('POST', '/answers', params={'callback_id': callback_id}, data={'notification': 'Произошла ошибка'})
                except:
                    pass
    
    def handle_text_message(self, chat_id: int, text: str):
        user_state = self.user_states.get(chat_id, {})
        
        if user_state.get('state') == 'waiting_phone':
            phone = text.strip()
            
            if not self.validate_phone(phone):
                error_text = "❌ Неверный формат номера телефона!\n\nПожалуйста, введите номер в формате: 10 цифр без +7 или 8\nНапример: 9123456789"
                self.send_message(chat_id, error_text)
                if chat_id in self.ai_blocked:
                    keyboard = self.create_main_keyboard(block_ai=True)
                else:
                    keyboard = self.create_main_keyboard()
                time.sleep(0.5)
                self.send_message(chat_id, "Выберите нужный раздел:", [keyboard])
                del self.user_states[chat_id]
                print(f"[{self.get_current_time()}] ⚠️  Некорректный номер (chat_id={chat_id})")
                return
            
            print(f"[{self.get_current_time()}] 🔍 Поиск заявлений для телефона {phone} (chat_id={chat_id})")
            time.sleep(1)
            requests_list = self.search_requests_by_phone(phone)
            
            if requests_list is None:
                error_text = "❌ Произошла ошибка при обращении к базе данных. Попробуйте позже."
                self.send_message(chat_id, error_text)
                if chat_id in self.ai_blocked:
                    keyboard = self.create_main_keyboard(block_ai=True)
                else:
                    keyboard = self.create_main_keyboard()
                time.sleep(0.5)
                self.send_message(chat_id, "Выберите нужный раздел:", [keyboard])
                del self.user_states[chat_id]
                return
            
            if not requests_list:
                not_found_text = "ℹ️  По указанному Вами номеру телефона не найдено заявлений на обучение.\n\nПроверьте правильность введенного номера или обратитесь к куратору СИТУ."
                self.send_message(chat_id, not_found_text)
                if chat_id in self.ai_blocked:
                    keyboard = self.create_main_keyboard(block_ai=True)
                else:
                    keyboard = self.create_main_keyboard()
                time.sleep(0.5)
                self.send_message(chat_id, "Выберите нужный раздел:", [keyboard])
                del self.user_states[chat_id]
                print(f"[{self.get_current_time()}] ℹ️  Заявления не найдены (chat_id={chat_id})")
                return
            
            self.user_states[chat_id] = {
                'state': 'viewing_requests',
                'requests': requests_list,
                'current_index': 0
            }
            
            print(f"[{self.get_current_time()}] ✅ Найдено {len(requests_list)} заявлений (chat_id={chat_id})")
            
            total = len(requests_list)
            current_request = requests_list[0]
            
            text_msg = f"По указанному Вами номеру телефона найдено заявлений на обучение: {total}\n\n"
            text_msg += f"Заявление 1 из {total}:\n\n"
            text_msg += self.format_request_info(current_request)
            
            keyboard = self.create_requests_navigation_keyboard(0, total)
            self.send_message(chat_id, text_msg, [keyboard])
            return
        
        if user_state.get('state') == 'chatting_with_ai':
            print(f"[{self.get_current_time()}] 🤖 Обработка вопроса для ИИ-ассистента (chat_id={chat_id})")
            
            answer = self.get_ai_response(chat_id, text)

            # Если бот заблокировал пользователя — выводим главное меню без кнопки ИИ-ассистента
            if chat_id in self.ai_blocked:
                keyboard = self.create_main_keyboard(block_ai=True)
                self.show_screen(None, chat_id, None, answer, keyboard)
                if chat_id in self.user_states:
                    del self.user_states[chat_id]
                return


            # Иначе обычный вывод ответа ИИ-ассистента + кнопка "Назад"
            keyboard = self.create_back_to_main_keyboard()
            self.show_screen(None, chat_id, None, answer, keyboard)
            return
        
        text_msg = "Бот не обрабатывает текстовые сообщения, воспользуйтесь кнопками."
        if chat_id in self.ai_blocked:
            keyboard = self.create_main_keyboard(block_ai=True)
        else:
            keyboard = self.create_main_keyboard()
        self.send_message(chat_id, text_msg, [keyboard])
    
    def handle_update(self, update: Dict):
        try:
            update_type = update.get('update_type')
            timestamp = update.get('timestamp')
            
            if not update_type:
                print(f"[{self.get_current_time()}] ⚠️  Получено обновление без типа")
                return
            
            self.print_separator()
            print(f"[{self.get_current_time()}] 📨 Получено обновление: {update_type} (timestamp: {timestamp})")
            
            if update_type == "dialog_cleared": 
                print(f"[{self.get_current_time()}] dialog_cleared → История диалога очищена")
                return

            if update_type == "bot_stopped":
                # Полный сброс ограничений и блокировок ИИ-ассистента
                self.ai_violation_counter = {}
                self.ai_blocked = set()
                print(f"[{self.get_current_time()}] bot_stopped → Счетчики блокировок сброшены")
                return

            if update_type == 'bot_started':
                chat_id = update.get('chat_id')
                if not chat_id:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует chat_id в bot_started")
                    return
                
                user = update.get('user', {})
                user_name = user.get('name', 'Гость')
                payload = update.get('payload')
                
                print(f"[{self.get_current_time()}] 👤 Пользователь {user_name} (chat_id={chat_id}) запустил бота")
                if payload:
                    print(f"[{self.get_current_time()}]    📎 С параметром: {payload}")
                
                welcome_text = "Здравствуйте! Мы - команда Сетевого ИТ-Университета (СИТУ), приветствуем Вас. Я - бот-помощник команды, отвечу на Ваши вопросы.\n\nЧто Вас интересует?"
                if chat_id in self.ai_blocked:
                    keyboard = self.create_main_keyboard(block_ai=True)
                else:
                    keyboard = self.create_main_keyboard()
                self.send_message(chat_id, welcome_text, [keyboard])
                
            elif update_type == 'message_created':
                message = update.get('message', {})
                if not message:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует message в message_created")
                    return
                
                recipient = message.get('recipient', {})
                chat_id = recipient.get('chat_id')
                
                if not chat_id:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует chat_id в message_created")
                    return
                
                sender = message.get('sender', {})
                sender_name = sender.get('name', 'Гость')
                body = message.get('body', {})
                text = body.get('text', '')
                
                print(f"[{self.get_current_time()}] 💬 Сообщение от {sender_name} (chat_id={chat_id}): {text[:50]}...")
                
                self.handle_text_message(chat_id, text)
                
            elif update_type == 'message_callback':
                callback = update.get('callback', {})
                if not callback:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует callback в message_callback")
                    return
                
                callback_id = callback.get('callback_id')
                payload = callback.get('payload')
                user = callback.get('user', {})
                user_name = user.get('name', 'Гость')
                
                message = update.get('message', {})
                recipient = message.get('recipient', {}) if message else {}
                chat_id = recipient.get('chat_id')
                message_id = message.get('message_id') if message else None
                
                if not callback_id:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует callback_id")
                    return
                
                if not payload:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует payload в callback")
                    return
                
                if not chat_id:
                    print(f"[{self.get_current_time()}] ⚠️  Отсутствует chat_id в callback")
                    return
                
                print(f"[{self.get_current_time()}] 🔘 {user_name} (chat_id={chat_id}) нажал кнопку: {payload}")
                
                self.handle_callback(callback_id, payload, chat_id, message_id)
            else:
                print(f"[{self.get_current_time()}] ℹ️  Неизвестный тип обновления: {update_type}")
                
        except Exception as e:
            print(f"[{self.get_current_time()}] ❌ Критическая ошибка обработки обновления: {e}")
            print(f"[{self.get_current_time()}]    Данные обновления: {update}")
    
    def get_updates(self, timeout: int = 30, limit: int = 100) -> List[Dict]:
        params = {'timeout': timeout, 'limit': limit}
        
        if self.marker is not None:
            params['marker'] = self.marker
        
        result = self.make_request('GET', '/updates', params=params)
        
        if result and 'marker' in result:
            self.marker = result.get('marker')
            updates = result.get('updates', [])
            if updates:
                print(f"[{self.get_current_time()}] 📬 Получено {len(updates)} обновлений")
            return updates
        elif result is None:
            print(f"[{self.get_current_time()}] ⚠️  Нет запроса на обновления")
        
        return []

    def run(self):
        print("=" * 60)
        print("🎓 Бот-помощник СИТУ запущен!")
        print("=" * 60)
        
        bot_info = self.get_bot_info()
        if bot_info:
            print(f"📋 Имя бота: {bot_info.get('name')}")
            print(f"📋 Username: @{bot_info.get('username')}")
            print(f"📋 ID: {bot_info.get('user_id')}")
        else:
            print("❌ Не удалось получить информацию о боте. Проверьте токен!")
            return
        
        self.warmup_external_api()
        print("🔥 API доступа к БД ЭПОСа прогрет")
        """print("🖼️ Загрузка изображения Ситуши...")
        self.situsha_photo_id = self.upload_image("01-situsha-graduate-student.png")
        if self.situsha_photo_id:
            print("✅ Изображение Ситуши успешно загружено и закешировано")
        else:
            print("⚠️ Не удалось загрузить изображение Ситуши — бот продолжит без картинки")"""
        print(f"\n📊 Загружено вопросов: {len(self.qa_data.get('questions', []))}")
        print(f"📚 База знаний: {len(self.knowledge_base.get('items', []))} записей")
        print(f"🏛️ ВУЗов: {len(self.universities.get('universities', []))}")
        print(f"📞 Ссылка на администратора: {self.vk_admin_url}")
        print(f"🔗 API URL: {self.api_url}")
        print("⏳ Ожидание обращений слушателей...\n")
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while True:
            try:
                updates = self.get_updates()
                
                if updates:
                    consecutive_errors = 0
                    for update in updates:
                        self.handle_update(update)
                    
            except KeyboardInterrupt:
                print("\n" + "=" * 60)
                print("⛔ Остановка бота по запросу пользователя...")
                print("=" * 60)
                break
            except Exception as e:
                consecutive_errors += 1
                print(f"❌ Неожиданная ошибка в главном цикле: {e}")
                print(f"   Попыток с ошибкой подряд: {consecutive_errors}/{max_consecutive_errors}")
                
                if consecutive_errors >= max_consecutive_errors:
                    print(f"❌ Превышено максимальное количество последовательных ошибок ({max_consecutive_errors})")
                    print("   Бот будет остановлен для предотвращения проблем")
                    break
                
                print("⏳ Повторная попытка через 5 секунд...")
                time.sleep(5)


if __name__ == '__main__':
    import os
    
    ACCESS_TOKEN = os.environ.get('BOT_TOKEN')
    API_TOKEN = os.environ.get('API_TOKEN')
    API_URL = os.environ.get('API_URL')
    GIGACHAT_CLIENT_ID = os.environ.get('GIGACHAT_CLIENT_ID')
    GIGACHAT_CLIENT_SECRET = os.environ.get('GIGACHAT_CLIENT_SECRET')
    
    if not ACCESS_TOKEN:
        print("⚠️  ОШИБКА: Токен бота (BOT_TOKEN) не найден!")
        exit(1)
    
    if not API_TOKEN:
        print("⚠️  ОШИБКА: Токен API базы данных ЭПОС (API_TOKEN) не найден!")
        exit(1)
    
    if not API_URL:
        print("⚠️  ОШИБКА: URL API базы данных ЭПОС (API_URL) не найден!")
        exit(1)
    
    if not GIGACHAT_CLIENT_ID or not GIGACHAT_CLIENT_SECRET:
        print("⚠️  ОШИБКА: Учетные данные GigaChat не найдены!")
        print("   Установите GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET")
        exit(1)
    
    print("🔐 Токены успешно загружены из переменных окружения")
    
    bot = SITUBot(
        access_token=ACCESS_TOKEN, 
        api_token=API_TOKEN, 
        api_url=API_URL,
        gigachat_client_id=GIGACHAT_CLIENT_ID,
        gigachat_client_secret=GIGACHAT_CLIENT_SECRET
    )
    bot.run()