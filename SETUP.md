# Установка Pogoda на macOS

## 1. Установить Python 3.11+

```bash
# Через Homebrew (рекомендуется)
brew install python@3.11

# Проверить версию
python3 --version
```

## 2. Клонировать репозиторий

```bash
git clone https://github.com/grayson8geraldo/pogoda.git
cd pogoda
```

## 3. Создать виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
```

## 4. Установить зависимости

```bash
pip install -e .
```

## 5. Запуск (Paper Trading — виртуальные деньги)

Для paper trading **не нужны API ключи Polymarket**. Бот работает с реальными данными погоды и реальными рынками, но торгует на виртуальном балансе.

```bash
# Посмотреть список городов
pogoda cities

# Проверить прогноз погоды для Токио на завтра
pogoda weather tokyo

# Сканировать рынки и торговать на виртуальном балансе ($200)
pogoda scan -c tokyo

# Сканировать с другим начальным балансом
pogoda scan -c tokyo --balance 500

# Сканировать все города
pogoda scan

# Посмотреть портфель
pogoda portfolio

# Когда рынок разрешится — отметить реальную температуру
pogoda resolve "Tokyo" 15

# Сбросить виртуальный портфель
pogoda reset --balance 200
```

## 6. Рабочий цикл (ежедневный)

```bash
# 1. Утром: проверить прогнозы
pogoda weather tokyo
pogoda weather london

# 2. Запустить скан — бот найдет рынки и "купит" бины
pogoda scan -c tokyo london

# 3. Посмотреть что купил
pogoda portfolio

# 4. На следующий день: когда станет известна реальная температура,
#    разрешить рынок (например, в Токио было 16°C):
pogoda resolve "Tokyo" 16

# 5. Проверить обновленный P&L
pogoda portfolio
```

## 7. (Опционально) Настройка для реальной торговли

Для реальных ордеров на Polymarket:

```bash
cp .env.example .env
```

Заполнить `.env`:
- `POLYMARKET_API_KEY` — из настроек Polymarket
- `POLYMARKET_API_SECRET` — из настроек Polymarket
- `POLYMARKET_API_PASSPHRASE` — из настроек Polymarket
- `PRIVATE_KEY` — приватный ключ Ethereum кошелька
- `SIGNATURE_TYPE` — `0` для MetaMask, `1` для email-логина Polymarket

Запустить с реальными ордерами:

```bash
pogoda scan -c tokyo --live
```

## Решение проблем

### "No weather markets found"
Polymarket не всегда имеет активные рынки погоды. Проверьте вручную на polymarket.com.

### Ошибки API
Убедитесь что есть доступ к интернету. Open-Meteo API бесплатный и не требует ключей.

### Python не найден
```bash
# macOS может использовать python3 вместо python
alias python=python3
alias pip=pip3
```
