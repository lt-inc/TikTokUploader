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

Координатор проверил развёрнутую ревизию `231241e7` и зашифрованные сессии
на сервере, не выводя их значения. Разрозненные строки `via_proxy=True/False`
не относятся автоматически к одному аккаунту: раньше лог не содержал его ID.

Для `funny_videos_maker5` (account 24) одна и та же сохранённая сессия:

- На выбранном direct-маршруте получает `13`; native browser открывает login.
- Через настроенный server-default proxy подтверждает authenticated self UID
  и username `funny_videos_maker5`. Изменение `aid=1459` на `1988` direct-отказ
  не исправляет.
- После сохранения `proxy_mode=default` штатным API и `check` через main-be
  Terex подтвердил `connected`, session version 3. Повторный экспорт не понадобился.
- HTTP executor создал тестовый проект, передал синтетический MP4, получил
  media ID и завершил CommitUploadInner. Подписи финального запроса подготовлены;
  final POST не отправлялся. Media ID не доказывает опубликованный пост.

Для `funny-clips / coto28968b7` (account 23) картина другая: исходная сессия
подтверждает identity на direct, но получает `13` на server-default proxy.
На direct и HTTP, и native Studio возвращают `30411` до выдачи UploadNodes.
Повторный native тест с согласованным Chromium 145 UA и headful runtime
также вернул `30411`: смена TLS fingerprint/UA или переход на Studio не доказаны
как исправление. Direct egress определён как RU, server-default — FI; этого
недостаточно, чтобы объявить географию единственной причиной кода `30411`.
По явному согласию пользователя account 23 переведён на server-default,
session version 4; подготовлен Browser verification для повторного входа.
Его восстановление и дальнейшая загрузка требуют подтверждения после входа.

Оператору нужно перезагрузить установленное unpacked-расширение до версии 0.1.1.
Перед экспортом/Browser verification сохраняйте нужный маршрут. Ошибка `13`
сама по себе не различает истёкшую сессию и отказ на другом маршруте.
Автоматического обхода на другой proxy/direct нет: смена маршрута остаётся
явной операцией с session-version fencing. Полный выпуск с post ID не проверен.

## Диагностика загрузки

Логи проверки соединения теперь включают account ID, session version,
route mode и признак pending. Логи upload attempt/failure включают также
publication/attempt ID, upload mode, HTTP status, числовой provider code,
тип ответа и ограниченный SHA-256 tag RequestId. Тела ответов, исходный
RequestId, cookies, bearer tokens, proxy credentials и media URL не выводятся.
Существующая пауза и запрет повторной отправки после неизвестного final outcome
сохранены. `30411` описывается как отказ upload gateway, а не доказанный rate limit.

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
