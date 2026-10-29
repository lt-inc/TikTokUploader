# Проверка TikTok-сессии: 29.10.2026

## Что установлено

`Session received` в расширении версии 0.1.0 означало только получение HTTP 200
от Terex. `Connections.receive()` сохраняет cookies в зашифрованном pending-состоянии
и вызывает проверку; при отказе возвращает `needs_reauth` с ошибкой. Расширение
скрывало этот отказ общей фразой. Аккаунт при этом не становился подключённым.

В этой рабочей копии воспроизведены и исправлены следующие дефекты:

- Chrome identity fallback превращал cookies в словарь по имени. Одноимённые
  cookies разных доменов/путей затирались и теряли ограничения области действия.
  Теперь используется scoped CookieJar; оба HTTP-клиента учитывают срок действия.
- Расширение выбирало cookie store своего контекста исполнения, без проверки
  соответствия активной вкладке. Теперь store определяется через `tabIds` и
  передаётся явно; при отсутствии соответствия экспорт останавливается.
- Проверка identity принимала отсутствующий `message` и игнорировала явный
  ненулевой `error_code`/`status_code` рядом с identity. Теперь требуются
  `message=success`, отсутствие ошибочного статуса, корректные self UID и username.
  Некорректная структура ответа не подтверждает аккаунт.
- Серверные `message`/`description` и произвольный `error_code` попадали в лог.
  Теперь remote diagnostics содержат только ограниченный числовой код,
  имя клиента и признак proxy. Текст сетевых исключений не логируется.
- Partitioned cookies отклоняются при импорте: HTTP-клиенты не могут воспроизвести
  browser partition, и удалять её молча небезопасно.

Проверка остаётся authenticated self-запросом
`GET https://www.tiktok.com/passport/web/account/info/?aid=1459`.
Публичный профиль, HTML, cookie username и наличие `sessionid` не подтверждают
владение аккаунтом. Существующие проверки bound UID, session version,
pairing generation и выбранного маршрута сохранены.

## Связь с инцидентом и ограничения

Для экспорта `funny_videos_maker5` исходное свидетельство — отказ обоих
identity-клиентов с `error_code=13` на direct и proxy. Это подтверждает отказ
endpoint, но не объясняет, почему сессия не принимается: неистёкший cookie
не доказывает действующую серверную сессию.

По сообщениям координатора в рамках этого Run, у проблемной записи нет
дубликатов auth-cookie scopes, обе проверки `aid=1459/1988` дают код 13,
а отдельный native browser с импортированной сессией перенаправляется на login
и тоже не получает self identity. Контрольная подключённая запись проходит
те же GET-проверки. Эти наблюдения выполнены координатором, а не этим worker.
Следовательно, исправление словаря cookies или смена AID не доказаны как
решение именно этого отказа. Wrong-store export остаётся возможной причиной,
но источник исходного экспорта без свежего захвата не установлен.

Оператору нужно перезагрузить установленное unpacked-расширение до версии 0.1.1,
войти в нужный аккаунт в активной вкладке и экспортировать с новым кодом;
либо пройти Browser verification на выбранном в Terex маршруте. Результат
принимается только после подтверждения UID сервером. Этот worker не обращался
к production, не получал настоящие cookies, не публиковал видео и не проверял
расширение в настоящем Chrome/Edge.

## Локальная проверка

Регрессии до исправления: 9 отказов Python и 2 отказа Node. Команды:

```text
python -m compileall -q src
pytest -q
node --test tests/extension_session.test.cjs
```

Итог в Windows/Python 3.12.13: compileall и diff whitespace check прошли,
pytest — **53 passed, 5 skipped**, Node — **2 passed**. Пять opt-in browser
fixtures пропущены; настоящий Chromium и Linux Docker build здесь не запускались.
Единственное предупреждение — deprecation alias в зависимости Starlette/AnyIO.

Node-тесты исполняют popup в VM с подставными Chrome API: выбор active-tab store,
отказ при неизвестном store и отображение `needs_reauth`. Python-регрессии
проверяют scoped cookies/expiry, отказ при ошибочном self-ответе, отсутствие
секретного текста в диагностике и отказ от превращения partitioned cookie
в обычный. Существующие critical-тесты проверяют mismatch/version/route fencing.
Эти проверки не доказывают совместимость с живым TikTok или успешную публикацию.

## Первичные источники

- [Chrome cookies API](https://developer.chrome.com/docs/extensions/reference/api/cookies):
  default execution-context store, отдельный incognito store, `tabIds`/`storeId`,
  domain/path ordering и partition semantics.
- [autotok 2.0.1 auth.py](https://github.com/makiisthenes/TiktokAutoUploader/blob/08bfaea493b35df4e1477c3999ac25f414267634/autotok/auth.py):
  ожидание login/routing cookies и сохранение UA; само по себе не проверяет bound UID.
- [wkaisertexas/tiktok-uploader auth.py](https://github.com/wkaisertexas/tiktok-uploader/blob/main/src/tiktok_uploader/auth.py):
  browser cookie import не заменяет authenticated self identity. Код не заимствован,
  более слабые критерии входа и debug-печать cookies не перенесены.
