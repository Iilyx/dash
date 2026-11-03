# dash

Преобразование Stimulsoft JSON `.mrt` → компактный catalog для LLM.

Полный `catalog.json` в промпт не кладётся: рядом пишется `catalog_pages/` —
лёгкий индекс всех страниц + pack на каждую (page + нужные sources/variables).

## Запуск

```bash
python extract_mrt.py SalesOfProducts.mrt
```

Пишет:

- `catalog.json` — полный catalog
- `catalog_pages/index.json` — карточки всех pages (для выбора)
- `catalog_pages/dictionary.json` — sources/variables/relations без тел pages
- `catalog_pages/0.json`, `1.json`, … — pack одной страницы

Опции: `-o path`, `--pages-dir dir`, `--no-pages`.

Зависимости: только стандартная библиотека Python 3.

## Что попадает в catalog

- `report`
- `databases`, `sources` (SQL), `relations`, `variables`, `business_objects`, `resources`, `functions`
- `pages` / dashboard `components`: meters, DataFilters, TopN, DashboardInteraction, drill-down, transforms, role=viz|filter, …

Вёрстка, brushes/fonts, картинки и connection strings не тащатся.

## Как кормить LLM

1. В системный промпт — `catalog_pages/index.json` (+ дефолты variables при необходимости).
2. По вопросу — выбрать page(s) из индекса (можно несколько / все, без top-K отсечения).
3. Подгрузить `catalog_pages/N.json` и уже по нему строить SQL / ответ.
