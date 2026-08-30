# Despliegues

## Topología de producción

La automatización se divide deliberadamente en componentes con autoridad distinta:

| Componente | Ubicación | Responsabilidad |
| --- | --- | --- |
| FastAPI y webhook de Telegram | Vercel | Plano de control: autenticación, comandos, callbacks y mutaciones auditadas. Nunca llama a X. |
| PostgreSQL | Proveedor persistente | Fuente de verdad de usuarios, roles, revisiones, agenda, versión y runs. |
| `automation-run` | Host persistente | Worker auditado de slots diarios: MiniMax genera propuestas; Colmat valida, persiste, notifica y solo publica texto en `direct` con todos los gates. |
| `generation-run` | Host persistente | Consumer de `/generar`: MiniMax propone texto/imagen y siempre crea revisión humana. Nunca recibe X. |
| `publication-run` | Host persistente | Consumer de `/publicar`: procesa solo snapshots aprobados con gates de X. No recibe MiniMax. |
| OpenClaw | Perfil aislado en el mismo host | Orquesta el comando fijo del worker y alerta por su resultado; no decide contenido, permisos ni aprobaciones. |
| MiniMax | API externa | Solo genera propuestas de texto e imagen; no programa, aprueba ni publica. |
| X | API oficial | Recibe snapshots aprobados desde `publication-run` o texto desde `automation-run` en `direct`, siempre bajo gates explícitos. |

Vercel Cron, GitHub Actions, el timer heredado y OpenClaw no deben invocar simultáneamente el
mismo worker. Elige un solo orquestador. La idempotencia reduce duplicados, pero no sustituye una
operación clara ni la conciliación de resultados ambiguos.

Antes de desplegar:

- Rota cualquier clave MiniMax que haya aparecido en chats, logs o capturas. Usa exclusivamente la
  clave nueva desde un gestor de secretos.
- Confirma cuota de MiniMax para texto e imagen.
- Configura OAuth 1.0a de usuario para la cuenta institucional de X y verifica su identidad.
- Añade crédito y límites de gasto en X Developer Console. Tener credenciales válidas no implica
  tener saldo para publicar.
- Mantén `COLMAT_LIVE_ENABLED=false` y `COLMAT_DIRECT_PUBLISH_ENABLED=false` durante todo el
  aprovisionamiento.

La puntuación editorial y las plantillas ayudan a evaluar una pieza, pero no garantizan viralidad,
impresiones ni crecimiento.

## Vercel y PostgreSQL

La API usa `api/index.py` y `vercel.json`. Vercel necesita una `DATABASE_URL` PostgreSQL
persistente; su filesystem efímero no admite la SQLite local ni la media del worker. El proceso
web se conecta con creación de esquema deshabilitada: Vercel no ejecuta DDL, migraciones ni
workers.

Aplica el esquema fuera de Vercel, antes del primer despliegue, y vuelve a aplicar el archivo antes
de actualizar una instalación existente. El script es repetible y conserva los registros:

```bash
psql "$DATABASE_URL_UNPOOLED" -v ON_ERROR_STOP=1 -f deploy/postgres.sql
```

Usa la DSN no agrupada equivalente de tu proveedor para este paso transaccional; no ejecutes el
DDL desde una función de Vercel.

Configura como secretos de Vercel:

```dotenv
DATABASE_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
COLMAT_LIVE_ENABLED=false
COLMAT_DIRECT_PUBLISH_ENABLED=false
```

No cargues `MINIMAX_API_KEY` ni credenciales de X en Vercel. `/generar` solo inserta una solicitud
durable; el worker persistente llama a MiniMax. El webhook no genera ni publica.

Despliega y comprueba salud y readiness:

```bash
pytest -q
vercel link
vercel --prod
curl -fsS https://TU_DOMINIO/api/health
curl -fsS https://TU_DOMINIO/api/ready
```

Solo después de obtener `200` en readiness registra
`https://TU_DOMINIO/api/telegram/webhook` con el mismo `TELEGRAM_WEBHOOK_SECRET`. Telegram exige el
header `X-Telegram-Bot-Api-Secret-Token`; el endpoint además deduplica `update_id`. No uses polling
con el mismo token mientras el webhook esté activo.

## Owner, equipo e identidades de servicio

`SirHegel` es el owner canónico. Solo en una base de plataforma nueva y vacía:

```bash
colmat-x team-bootstrap --email CORREO_REAL --display "SirHegel" --username sirhegel
```

El comando no crea contraseñas. No inventes una, no uses emails ficticios para personas y no
repitas el bootstrap sobre una base que ya tenga usuarios.

Los roles admitidos son `owner`, `admin`, `editor`, `reviewer`, `publisher`, `scheduler` y
`auditor`. Owner puede delegarlos todos; admin no puede crear ni administrar owners u otros admins.
Editor redacta, reviewer decide, publisher publica, scheduler gestiona la agenda y auditor solo
consulta. Autor y reviewer deben ser personas o identidades distintas.

Crea principals de servicio separados y conserva los IDs devueltos; no uses a `SirHegel` como
identidad automática. Los identificadores reservados `@colmat.internal` satisfacen el campo de
identidad, pero no son inbox, cuentas de login ni usuarios de Telegram y nunca reciben contraseña:

```bash
colmat-x team-add --actor-id OWNER_ID --email automation@colmat.internal \
  --display "Colmat Scheduler" --username colmat-automation --role scheduler
colmat-x team-add --actor-id OWNER_ID --email author-bot@colmat.internal \
  --display "Colmat Autor" --username colmat-ai-author --role editor
colmat-x team-add --actor-id OWNER_ID --email review-bot@colmat.internal \
  --display "Colmat Revisor" --username colmat-direct-reviewer --role reviewer
colmat-x team-add --actor-id OWNER_ID --email publisher-bot@colmat.internal \
  --display "Colmat Publicador" --username colmat-x-publisher --role publisher
colmat-x team-list --actor-id OWNER_ID
```

Estas identidades existen solo para RBAC, separación de funciones y auditoría. Para integrantes
humanos, usa siempre email real y único.

Vincula cada integrante humano que operará Telegram con su `from.id` y chat reales. El bot nunca
crea usuarios ni confía en el `username` de Telegram:

```bash
colmat-x telegram-bind --actor-id OWNER_ID --user-id USER_ID \
  --telegram-user-id FROM_ID --chat-id CHAT_ID --purpose control
```

## Sincronizar y activar la agenda

`config/automation.yaml` es solamente la entrada de validación y sincronización. El runtime no lee
ese archivo: `automation-run`, `/estado`, `/calendario` y `/modo` usan los settings persistidos en
PostgreSQL. Cambiar el YAML sin sincronizarlo no cambia la operación.

Primero valida y previsualiza el YAML sin tocar la base:

```bash
colmat-x automation-validate
colmat-x automation-calendar --days 7
```

Luego lee la versión actual, sincroniza con CAS y vuelve a leerla después de cada escritura:

```bash
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-sync --actor-id SCHEDULER_ID --expected-version VERSION_1
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-mode human_review --actor-id OWNER_ID --expected-version VERSION_2
colmat-x automation-status --actor-id OWNER_ID
```

Cada `VERSION_*` es la versión realmente devuelta en la consulta anterior; no es un valor literal.
`automation-sync` copia slots, zona horaria y límites a la base, preserva el modo persistido y no
activa ni ejecuta nada. `automation-mode` actualiza de forma atómica el modo global y el de todos
los slots persistidos. Al terminar el aprovisionamiento, conserva la agenda desactivada, ambos
gates en `false` y los tres jobs de OpenClaw deshabilitados; la activación es un cambio operativo
posterior y explícito.

Empieza siempre en `human_review`. En este modo el worker puede generar texto e imagen, persiste el
snapshot y lo envía a revisión. El equipo puede consultar y operar por Telegram con `/estado`,
`/equipo`, `/calendario`, `/modo`, `/generar`, `/publicar` y los callbacks de aprobación/rechazo.
Ninguno de esos handlers llama directamente a X.

Toda imagen generada se mantiene en `human_review`: una persona debe inspeccionar y aprobar el
snapshot exacto de texto y media antes de que `publication-run` pueda enviarlo. No habilites un
slot con `generate_image: true` en `direct`; el modo directo queda reservado a texto y exige sus
gates adicionales. `ficha_territorio` también permanece en revisión para que una persona confirme
la autoría de la célula local exigida por el manual.

La transición del run a `AWAITING_REVIEW` y su fila en `automation_review_notifications` comparten
una transacción. `automation-run` drena esa outbox antes de procesar slots, también cuando la agenda
está pausada. Una entrega Telegram ambigua queda `unknown` terminal y no se reintenta; los rechazos
explícitos quedan `failed`. La imagen se confirma primero y los botones se envían después en un
mensaje de texto ligado al snapshot. Los nonces de esos botones nunca se guardan en claro.

`/generar` y `/publicar` solo encolan. `generation-run` y `publication-run`, respectivamente,
consumen esas colas con claims, leases y fences. El primero siempre deja el borrador en revisión;
su outbox elige un binding activo con permiso de revisión, aunque quien solicitó sea editor. El
segundo exige aprobación vigente, identidad de publisher y gates de X.

## Entorno privado del worker

Instala el proyecto en un host persistente bajo un usuario de sistema sin privilegios. Guarda los
secretos en archivos regulares, propiedad de ese usuario y con modo `0600`; el cargador de los
workers rechaza enlaces, propietarios distintos y permisos más amplios. No guardes ninguno dentro
del checkout. El command job de OpenClaw debe ejecutar el worker con ese mismo usuario fijo, o
mediante un mecanismo de privilegios limitado que cambie exactamente a ese usuario.

Crea tres archivos separados. `/var/lib/colmat-x/automation-worker.env` puede usar MiniMax y X
porque ejecuta el pipeline diario completo:

```dotenv
DATABASE_URL=
TELEGRAM_BOT_TOKEN=
MINIMAX_MODEL=MiniMax-M2.7
MINIMAX_IMAGE_MODEL=image-01
COLMAT_TELEGRAM_ALERT_CHAT_ID=
COLMAT_TELEGRAM_REVIEWER_USER_ID=
COLMAT_AUTOMATION_SCHEDULER_ID=
COLMAT_AUTOMATION_AUTHOR_ID=
COLMAT_AUTOMATION_REVIEWER_ID=
COLMAT_AUTOMATION_PUBLISHER_ID=
COLMAT_AUTOMATION_MEDIA_ROOT=/var/lib/colmat-x/media
EXPECTED_X_USERNAME=
EXPECTED_X_USER_ID=
X_CONSUMER_KEY=
X_CONSUMER_SECRET=
X_ACCESS_TOKEN=
X_ACCESS_TOKEN_SECRET=
COLMAT_LIVE_ENABLED=false
COLMAT_DIRECT_PUBLISH_ENABLED=false
```

`/var/lib/colmat-x/generation-worker.env` opera MiniMax y Telegram, pero nunca X:

```dotenv
DATABASE_URL=
TELEGRAM_BOT_TOKEN=
MINIMAX_MODEL=MiniMax-M2.7
MINIMAX_IMAGE_MODEL=image-01
COLMAT_AUTOMATION_SCHEDULER_ID=
COLMAT_AUTOMATION_AUTHOR_ID=
COLMAT_GENERATION_ENABLED=false
COLMAT_GENERATION_DEFAULT_IMAGE=true
COLMAT_GENERATION_MEDIA_ROOT=/var/lib/colmat-x/media
```

No añadas ninguna variable `X_*` a ese archivo: `generation-run` las rechaza.
`/var/lib/colmat-x/publication-worker.env` opera X sobre snapshots ya aprobados, pero nunca
MiniMax:

```dotenv
DATABASE_URL=
COLMAT_AUTOMATION_SCHEDULER_ID=
COLMAT_AUTOMATION_PUBLISHER_ID=
COLMAT_AUTOMATION_MEDIA_ROOT=/var/lib/colmat-x/media
EXPECTED_X_USERNAME=
EXPECTED_X_USER_ID=
X_CONSUMER_KEY=
X_CONSUMER_SECRET=
X_ACCESS_TOKEN=
X_ACCESS_TOKEN_SECRET=
COLMAT_LIVE_ENABLED=false
MINIMAX_API_KEY=
```

La última asignación está deliberadamente vacía: no es una credencial. Evita que publication
herede MiniMax si el proceso padre de OpenClaw lo recibió. Los valores de cada `--env-file`
prevalecen sobre el entorno heredado, por lo que también los gates `false` quedan cerrados.

Guarda la clave real una sola vez en otro `EnvironmentFile` de systemd, modo `0600`, y haz que
llegue al contexto que ejecuta automation y generation. No la repitas en los tres envs, en la
configuración de OpenClaw ni en el repositorio. Si la unidad padre también la expone al command job
de publication, el valor vacío de `publication-worker.env` debe enmascararla como se muestra.

No pases secretos como argumentos ni los pegues en prompts. Crea `/var/lib/colmat-x/media` con
modo `0700` y usa esa misma raíz para ambas variables: `publication-run` debe verificar y leer las
imágenes producidas por las colas de automation y generation. Conserva y respalda el directorio;
la base registra la URL local, el MIME, el tamaño y el hash. Una imagen generada mayor de 5 MiB o
con contenido que no coincida con el MIME permitido falla antes de publicar.

Prueba con el mismo usuario del worker:

```bash
/opt/colmat-x-automation/.venv/bin/colmat-x automation-run \
  --env-file /var/lib/colmat-x/automation-worker.env
/opt/colmat-x-automation/.venv/bin/colmat-x generation-run \
  --env-file /var/lib/colmat-x/generation-worker.env --limit 5
/opt/colmat-x-automation/.venv/bin/colmat-x publication-run \
  --env-file /var/lib/colmat-x/publication-worker.env --live --limit 5
```

Con la agenda desactivada, automation debe devolver `status=paused`. Generation y publication
deben seguir bloqueados mientras sus gates estén en `false`. No abras esos gates durante el
aprovisionamiento.

## OpenClaw aislado como orquestador

Crea un perfil dedicado a Colmat, mantén su gateway ligado a `127.0.0.1` y no publiques el puerto.
No reutilices el perfil personal por defecto. OpenClaw asigna tres command jobs deterministas, con
directorio de trabajo fijo y argumentos separados. MiniMax opera la generación dentro de los
workers auditados de Colmat; OpenClaw no le delega una sesión autónoma ni acceso directo a X:

```bash
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron add \
  --name colmat-automation \
  --every 5m \
  --timeout-seconds 3600 \
  --command-argv \
  '["/opt/colmat-x-automation/.venv/bin/colmat-x","automation-run","--env-file","/var/lib/colmat-x/automation-worker.env"]' \
  --command-cwd /opt/colmat-x-automation \
  --disabled

OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron add \
  --name colmat-generation \
  --agent colmat-editorial \
  --every 2m \
  --timeout-seconds 1800 \
  --command-argv \
  '["/opt/colmat-x-automation/.venv/bin/colmat-x","generation-run","--env-file","/var/lib/colmat-x/generation-worker.env","--limit","5"]' \
  --command-cwd /opt/colmat-x-automation \
  --disabled

OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron add \
  --name colmat-publication \
  --every 2m \
  --timeout-seconds 900 \
  --command-argv \
  '["/opt/colmat-x-automation/.venv/bin/colmat-x","publication-run","--env-file","/var/lib/colmat-x/publication-worker.env","--live","--limit","5"]' \
  --command-cwd /opt/colmat-x-automation \
  --disabled
```

Adapta la ruta del estado al host, pero conserva un directorio exclusivo. `--command-argv` evita
una orden libre interpretada por shell. El job solo orquesta y no recibe `--actor-id`: la identidad
efectiva viene del `COLMAT_AUTOMATION_SCHEDULER_ID` fijo en el archivo protegido. Restringe quién
puede editar los tres envs, el `EnvironmentFile` de MiniMax y el perfil de OpenClaw; la definición
del job no administra RBAC, no aprueba borradores ni llama por sí misma a MiniMax o X. Conserva
los tres jobs deshabilitados hasta validar cada comando con su mismo usuario y entorno. Conserva
timeouts explícitos: generación y automatización pueden encadenar varias llamadas acotadas de
texto e imagen; el timeout implícito no cubre el peor caso de cada lote.

Deja el job deshabilitado hasta que el comando manual funcione. Después programa el sondeo con la
frecuencia operativa elegida y alerta ante un código distinto de cero o estados `failed`,
`unknown` o `direct_blocked`. No registres un segundo bot de Telegram en OpenClaw con el token del
webhook de Colmat.

Conserva el `JOB_ID` devuelto al crear el job. Prueba y habilita siempre sobre el perfil aislado:

```bash
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron run JOB_ID --wait
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron enable JOB_ID
```

## Activar `direct`

`direct` omite la decisión humana por pieza de texto, pero no omite los controles editoriales. La
autorización administrativa queda persistida y ligada a la versión de settings; el worker usa
cuentas de servicio distintas para autor, reviewer y publisher. La evidencia debe estar marcada
como verificada por el equipo y debe fijar `expected_figure` y `expected_source`; el borrador se
bloquea si no coincide exactamente. Un slot con media generada sigue en `human_review` y no se
habilita en `direct` sin un control visual humano específico.

Antes de activarlo:

1. Rota y prueba la clave MiniMax; confirma cuota de imagen si corresponde.
2. Confirma permisos, identidad esperada y crédito de X con `doctor --credentials`, `x-whoami` y la
   consola de desarrolladores.
3. Sincroniza slots con evidencia real y auditable mientras el sistema sigue en `human_review`.
4. Verifica que scheduler, autor, reviewer y publisher sean cuentas activas con los roles correctos
   y que autor y reviewer sean distintos.
5. Detén el job, pausa la agenda con CAS y cambia a `direct` con owner o admin. La operación exige
   que `COLMAT_DIRECT_PUBLISH_ENABLED=true` esté presente en ese entorno.
6. Activa de nuevo la agenda con owner o admin: un scheduler puede activar `human_review`, pero no
   puede autorizar ni reautorizar `direct`.
7. Habilita ambos gates en el archivo privado del worker y añade `--live` al job fijo.
8. Ejecuta manualmente un solo slot controlado antes de habilitar el job recurrente.

Usa la versión que devuelva cada consulta:

```bash
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-disable --actor-id SCHEDULER_ID --expected-version VERSION_1
colmat-x automation-status --actor-id OWNER_ID
COLMAT_DIRECT_PUBLISH_ENABLED=true colmat-x automation-mode direct \
  --actor-id OWNER_ID --expected-version VERSION_2
colmat-x automation-status --actor-id OWNER_ID
COLMAT_DIRECT_PUBLISH_ENABLED=true colmat-x automation-enable \
  --actor-id OWNER_ID --expected-version VERSION_3
```

Con el job todavía deshabilitado, fija la tercera confirmación en sus argumentos:

```bash
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron edit JOB_ID \
  --disable \
  --timeout-seconds 3600 \
  --command-argv \
  '["/opt/colmat-x-automation/.venv/bin/colmat-x","automation-run","--env-file","/var/lib/colmat-x/automation-worker.env","--live"]'
```

La invocación directa es:

```bash
/opt/colmat-x-automation/.venv/bin/colmat-x automation-run \
  --env-file /var/lib/colmat-x/automation-worker.env --live
```

Son tres confirmaciones independientes en ejecución: `COLMAT_LIVE_ENABLED=true`,
`COLMAT_DIRECT_PUBLISH_ENABLED=true` y `--live`. Además, la base debe seguir habilitada, en modo
`direct`, con la misma versión y el mismo hash de slot autorizados. El publisher revalida esos
datos y ambos gates antes de crear el post.

## Parada de emergencia y observabilidad

Ante una anomalía:

1. Deshabilita o detén el job de OpenClaw.
2. Consulta la versión y ejecuta `automation-disable` con CAS.
3. Devuelve el modo a `human_review` con owner/admin si procede.
4. Retira `--live` de la definición del job mientras continúa deshabilitado.
5. Cambia ambos gates a `false` en el archivo privado antes de reiniciar el worker.
6. Revisa `automation-status`, la auditoría, Telegram y la cuenta de X.

```bash
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron disable JOB_ID
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-disable --actor-id SCHEDULER_ID --expected-version VERSION_ACTUAL
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-mode human_review --actor-id OWNER_ID --expected-version VERSION_SIGUIENTE
OPENCLAW_STATE_DIR=/var/lib/openclaw-colmat openclaw cron edit JOB_ID \
  --disable \
  --timeout-seconds 3600 \
  --command-argv \
  '["/opt/colmat-x-automation/.venv/bin/colmat-x","automation-run","--env-file","/var/lib/colmat-x/automation-worker.env"]'
```

Un resultado `unknown` significa que X pudo haber recibido la solicitud. No reintentes a ciegas:
comprueba la cuenta, conserva los logs y concilia el intento antes de reanudar. Respalda PostgreSQL
y el directorio de media con el worker detenido o mediante mecanismos consistentes del proveedor.

## Unidades systemd heredadas

`deploy/systemd/colmat-x.service` y `colmat-x.timer` ejecutan la cola YAML heredada mediante
`run-due --live`; no ejecutan `automation-run` ni sustituyen OpenClaw. No habilites esas unidades
si OpenClaw opera la automatización nueva sobre la misma cuenta de X.
