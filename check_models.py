import requests
import os
from dotenv import load_dotenv

# Завантажуємо ваш ключ з файлу .env
load_dotenv()
api_key = os.getenv('GEMINI_API_KEY')

if not api_key:
    print("Ключ не знайдено! Перевірте файл .env")
    exit()

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
response = requests.get(url)

if response.status_code == 200:
    print("✅ Доступні моделі для вашого ключа (які підтримують генерацію тексту):")
    models = response.json().get('models', [])
    for m in models:
        # Відфільтровуємо лише ті моделі, які вміють генерувати текст
        if 'generateContent' in m.get('supportedGenerationMethods', []):
            # Прибираємо префікс 'models/' для зручності
            print(f"- {m['name'].replace('models/', '')}")
else:
    print("❌ Помилка доступу до API:")
    print(response.text)