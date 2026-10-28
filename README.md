# extract_mrt

Превращает Stimulsoft `.mrt` (JSON) в компактный каталог для LLM: источники, SQL, колонки, параметры, связи, переменные с DialogInfo, выражения виджетов, форматы чисел и фильтры шаблона.

Строки подключения, layout, картинки и скрипты в каталог не попадают. XML `.mrt` не поддерживаются.

Нужен Python 3.10+. Зависимостей у скрипта нет; окружение поднимается через `uv`.

## Окружение

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
```

## Запуск

```bash
.venv/bin/python extract_mrt.py SalesOfProducts.mrt
.venv/bin/python extract_mrt.py SalesOfProducts.mrt -o catalog.json
```

Без `-o` каталог печатается в stdout.

## Что в каталоге

| Раздел | Содержание |
| --- | --- |
| `report` | имя, alias, описание |
| `databases` | имя и тип БД без connection string |
| `sources` | SQL/запрос, колонки, параметры, фильтры источника |
| `relations` | связи parent/child и колонки join |
| `variables` | переменные, в том числе DialogInfo (keys/values, привязки) |
| `business_objects` | бизнес-объекты и их колонки |
| `resources` / `functions` | имя и тип, без содержимого |
| `pages` | страницы и элементы: title/text, expressions, formats, filters, interaction |

Пример: `SalesOfProducts.mrt` → `catalog.json`.
