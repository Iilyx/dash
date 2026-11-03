# dash

Преобразование Stimulsoft JSON `.mrt` → компактный `catalog.json` для LLM.

## Запуск

```bash
python extract_mrt.py SalesOfProducts.mrt -o catalog.json
```

Без `-o` печатает JSON в stdout.

Зависимости: только стандартная библиотека Python 3.

## Что попадает в catalog

- `report`
- `databases`, `sources` (SQL), `relations`, `variables`, `business_objects`, `resources`, `functions`
- `pages` / dashboard `components`: meters, DataFilters, TopN, DashboardInteraction, drill-down, transforms, role=viz|filter, …

Вёрстка, brushes/fonts, картинки и connection strings не тащатся.
