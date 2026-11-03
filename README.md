# dash

Преобразование Stimulsoft JSON `.mrt` → компактный catalog для LLM.

## Запуск

```bash
python extract_mrt.py SalesOfProducts.mrt
```

Пишет:

- `catalog.json` — полный catalog
- `catalog_pages/index.json` — карточки всех pages
- `catalog_pages/dictionary.json` — sources/variables/relations без тел pages
- `catalog_pages/0.json`, `1.json`, … — pack одной страницы

Опции: `-o path`, `--pages-dir dir`, `--no-pages`.

Зависимости: только стандартная библиотека Python 3.

## Что попадает в catalog

- `report`
- `databases`, `sources` (SQL), `relations`, `variables`, `business_objects`, `resources`, `functions`
- `pages` / dashboard components: meters, DataFilters, TopN, drill-down, transforms, role=viz|filter, …

Вёрстка, brushes/fonts, картинки и connection strings не тащатся.
