# Colmat X Automation

Proyecto en Python para preparar, clasificar, revisar y publicar contenido institucional en X,
con control operativo por Telegram. Usa exclusivamente las API oficiales de X, Telegram y
MiniMax; nunca automatiza la interfaz web de X.

El proyecto nace en modo seguro. En `human_review`, toda salida de IA es un borrador y una persona
distinta del autor debe aprobar el snapshot exacto. En `direct`, la ejecución usa identidades de
servicio separadas y la publicación real exige dos seguros de entorno más `--live`. El webhook de
Telegram puede consultar y registrar decisiones, pero no crea usuarios ni llama a X.

## Capacidades

- Publicaciones originales de texto, programadas en YAML.
- Plantillas Jinja reutilizables y vista previa del texto final.
- Aprobación humana auditada y ligada al hash exacto del texto y la hora revisados.
- Estimación conservadora de longitud ponderada y controles preventivos de URL, cashtags y
  duplicados exactos.
- Cola SQLite heredada para un host persistente y store SQLAlchemy para SQLite local o PostgreSQL
  en despliegues serverless.
- Autenticación OAuth 1.0a de usuario para una cuenta de Colmat.
- Modo simulación, diagnóstico, reintentos controlados y conciliación de resultados ambiguos.
- RBAC explícito para `owner`, `admin`, `editor`, `reviewer`, `publisher`, `scheduler` y `auditor`,
  con separación entre autoría, aprobación y publicación.
- Webhook de Telegram autenticado, deduplicación de `update_id` y callbacks de un solo uso ligados
  a persona, chat, revisión y snapshot.
- MiniMax para proponer borradores e imágenes en memoria, con validación local cerrada y sin
  capacidad de aprobar, programar o publicar.
- Agenda diaria persistida, ejecución idempotente por slot y modos `human_review` y `direct`.
- Workers `automation-run`, `generation-run` y `publication-run`, invocables por command jobs
  deterministas de un OpenClaw aislado en un host persistente.
- Carga de hasta cuatro imágenes en X, texto alternativo obligatorio y marca `made_with_ai` para
  media generada con IA.
- Panel FastAPI desplegable en Vercel con acceso passwordless por Telegram, gestión de equipo,
  agenda, generación y revisión; además de `/api/health`, `/api/ready` y el webhook.

No automatiza respuestas, mensajes directos, likes, follows, tendencias, scraping ni segmentación
de personas. Tampoco promete viralidad: la rúbrica de engagement compara claridad, cifra temprana,
atribución y legibilidad, pero exige verificación editorial y no autoriza una publicación.

## Instalación

Requiere Python 3.11 o posterior. Desde la raíz del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
install -m 0600 .env.example .env
colmat-x doctor
```

La base `.state/colmat.db` se crea al primer uso con permisos locales restrictivos y nunca se
incluye en Git. En sistemas sin `install`, copia el archivo y aplica permisos equivalentes para
que solo su propietario pueda leer `.env`.

## Manual canónico y política editorial

La fuente de control es el PDF de Drive con ID
`1S_870mC8iixpNRv2FYtnnLZauO0eDGww`. La política local fija tanto el ID como la huella SHA-256 del
texto extraído para contrastarla en cada nueva auditoría. Sus reglas principales están codificadas en
`config/editorial-policy.yaml`:

- `dato_semana`, `ficha_territorio`, `lamina` y `correccion_publica` son la taxonomía cerrada.
- Toda pieza incluye literalmente una cifra y una fuente que el equipo debe verificar.
- COLMAT es doctrina, la Escuela Colombiana de Filosofía es escuela y Tierra Firme es partido;
  el generador no puede confundir sus funciones.
- Las láminas exigen serie completa y eje no truncado; las correcciones no admiten atenuantes.
- La IA no reconstruye el emblema ni el Nudo del Macizo, no usa retratos de personas vivas y solo
  propone colores generativos de la paleta permitida. Los activos oficiales se incorporan después.

`colmat-x ai-draft` devuelve JSON validado con `status=draft`, una evaluación explicable y
`publication_authorized=false`:

```bash
colmat-x ai-draft "Cifra y fuente ya verificadas para el dato semanal" \
  --category dato_semana \
  --institution escuela_colombiana_de_filosofia
```

La clave se recibe solo por `MINIMAX_API_KEY`. Usa `MiniMax-M2.7` para texto e `image-01` para
imagen salvo que la documentación oficial y las pruebas justifiquen otro modelo. Nunca subas una
clave a Git; si apareció en un chat, log o captura, rótala antes de usar producción.

## Equipo, jerarquía y Telegram

`SirHegel` es el owner canónico del workspace. En una base **nueva y vacía**, se crea una sola vez
con su correo real; el comando no genera ni solicita contraseñas:

```bash
colmat-x team-bootstrap --email CORREO_REAL --display "SirHegel" --username sirhegel
```

No ejecutes `team-bootstrap` sobre una base existente ni inventes correos, contraseñas o cuentas
compartidas. Cada integrante debe tener identidad propia y, si usa el bot, su `from.id` real:

```bash
colmat-x team-add --actor-id OWNER_ID --email CORREO_REAL_DEL_MIEMBRO \
  --display "Nombre real" --username USUARIO --role editor
colmat-x team-list --actor-id OWNER_ID
colmat-x telegram-bind --actor-id OWNER_ID --user-id USER_ID \
  --telegram-user-id FROM_ID --chat-id CHAT_ID --purpose control
```

Los roles no son contraseñas ni una escala numérica; son funciones explícitas:

| Rol | Alcance principal |
| --- | --- |
| `owner` | Autoridad total y único rol que puede delegar `owner` o `admin`. |
| `admin` | Administra equipo, integraciones y operación; no delega `owner` ni `admin`. |
| `editor` | Crea, edita y envía borradores; no los aprueba. |
| `reviewer` | Aprueba o rechaza; debe ser distinto del autor. |
| `publisher` | Publica el snapshot aprobado, sin editarlo ni aprobarlo. |
| `scheduler` | Sincroniza y opera la agenda; no cambia a `direct` ni publica por su rol. |
| `auditor` | Consulta workspace, borradores, automatización, Telegram y auditoría. |

El worker usa IDs estables de cuentas de servicio de mínimo privilegio: scheduler, autor,
revisor y publicador. No se deben rellenar esos IDs con el owner `SirHegel` ni con nombres
mutables. La identidad autora y la revisora siempre deben ser distintas. Los identificadores con
dominio reservado `@colmat.internal` son principals técnicos: no tienen inbox, contraseña, sesión
interactiva ni acceso por Telegram. Los emails reales y únicos siguen siendo obligatorios para
integrantes humanos.

Telegram autentica por `from.id`, nunca por `username`, y exige además el chat autorizado. Expone:

| Comando | Efecto |
| --- | --- |
| `/start` | Confirma la conexión. |
| `/estado` | Consulta el estado editorial y operativo. |
| `/equipo` | Muestra integrantes y roles visibles. |
| `/calendario [días]` | Consulta entre 1 y 31 días de agenda persistida. |
| `/modo [human_review\|direct]` | Consulta o solicita un modo, según RBAC y seguros. |
| `/generar <brief>` | Encola una generación durable; el webhook no llama a MiniMax. |
| `/publicar <id>` | Encola durablemente un borrador ya aprobado; el webhook no lo envía a X. |
| `/ayuda` | Muestra la guía integrada. |

Los callbacks de aprobación o rechazo usan nonces expirables y de un solo uso. Editar texto,
fecha, evidencia o imagen invalida la aprobación anterior. Ni los comandos ni los callbacks del
webhook llaman directamente a X: solo consultan o mutan el estado transaccional. Cualquier acceso
a X queda fuera del proceso web y bajo los seguros del worker persistente.

`generation-run` consume la cola de `/generar`: OpenClaw asigna el command job, MiniMax propone
texto y, opcionalmente, imagen, y Colmat crea siempre un draft `in_review`. Una outbox durable
entrega la revisión y sus botones al binding activo de un usuario con permiso de revisión; quien
solicita puede ser editor sin recibir permiso para aprobar. Los nonces se emiten atómicamente al
preparar la entrega y solo existen en memoria y en el mensaje de Telegram; la base conserva sus
hashes.
`publication-run` consume por separado la cola aprobada de `/publicar` con lease, fence,
idempotencia y los gates de X. El webhook no contiene credenciales de MiniMax ni de X.

El panel web usa el mismo binding `control` para enviar un código de ocho dígitos por Telegram.
Solo conserva HMAC de códigos, sesiones y CSRF; los códigos duran cinco minutos y las sesiones se
revalidan contra usuario activo, membresía, rol y binding en cada solicitud. `SirHegel` puede crear
cuentas desde `/app`; cada nueva identidad debe vincularse a su propio Telegram antes de entrar.

## Agenda y automatización programada

`config/automation.yaml` es una entrada revisable para validar y **sincronizar** la agenda; no es
la fuente de verdad durante una ejecución. `automation-run` lee exclusivamente `automation_settings`
en la base de datos. Por tanto, editar o desplegar el YAML no cambia el runtime hasta ejecutar
`automation-sync` con la versión CAS vigente.

Cuando un slot queda en revisión, `automation-run` confirma `AWAITING_REVIEW` y una fila de
`automation_review_notifications` en la misma transacción. El worker drena esa outbox antes de
evaluar nuevos slots, incluso si la agenda está pausada, por lo que un reinicio después del commit
no regenera el borrador ni pierde silenciosamente el aviso. Los callbacks se crean al reclamar la
entrega y sus nonces solo viven en memoria. Como Telegram no ofrece clave de idempotencia para
`sendMessage`/`sendPhoto`, una respuesta de transporte o protocolo ambigua queda `unknown` y nunca
se reenvía automáticamente; un rechazo explícito queda `failed` para intervención humana.

Flujo de configuración seguro:

```bash
colmat-x automation-validate
colmat-x automation-calendar --days 7
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-sync --actor-id SCHEDULER_ID --expected-version VERSION_1
colmat-x automation-status --actor-id OWNER_ID
colmat-x automation-mode human_review --actor-id OWNER_ID --expected-version VERSION_2
colmat-x automation-status --actor-id OWNER_ID
```

`automation-calendar` previsualiza el YAML local; `/calendario` y `automation-status` consultan la
configuración que ya fue persistida.

Cada slot puede declarar `weekdays` con una lista no vacía de valores canónicos: `lunes`,
`martes`, `miércoles`, `jueves`, `viernes`, `sábado` y `domingo`. Omitirla conserva la ejecución
diaria. La agenda canónica limita `dato-manana` a los lunes, como exige `dato_semana`; tanto el
calendario como el claim idempotente usan la fecha local de Bogotá y rechazan los demás días.

Cada escritura usa compare-and-swap: vuelve a consultar `automation-status` y sustituye cada
`VERSION_*` por la versión realmente devuelta. `automation-sync` preserva el modo persistido,
alinea a ese modo todos los slots y no activa ni ejecuta nada. `automation-mode` actualiza
atómicamente el modo global y el de los slots persistidos. Tras el aprovisionamiento, la agenda y
los tres jobs de OpenClaw permanecen deshabilitados; `automation-enable` es un paso operativo
posterior y deliberado.

En `human_review`, el worker genera el texto con MiniMax, puede generar la imagen cuando el slot lo
indica, guarda ambos como borrador y los somete a aprobación. Un slot con imagen generada permanece
en `human_review`: requiere inspección visual y aprobación humana del snapshot exacto antes de
entrar en `publication-run`. El modo `direct` queda reservado a piezas sin media generada y solo
continúa si la evidencia editorial está verificada y ligada a la cifra y fuente esperadas, la
rúbrica supera el umbral configurado, se respetan los roles separados y todos los seguros están
activos. `ficha_territorio` también conserva revisión humana para comprobar la autoría de la célula
local exigida por el manual. La puntuación ayuda a priorizar calidad; no demuestra ni garantiza
alcance viral.

El worker se invoca siempre con una identidad scheduler fija, nunca con un actor elegido por cada
ejecución:

```bash
# human_review: genera, persiste y notifica; no necesita --live
colmat-x automation-run --env-file /ruta/segura/automation-worker.env

# direct: además exige ambos gates de entorno y --live
colmat-x automation-run --env-file /ruta/segura/automation-worker.env --live
```

El archivo indicado debe ser regular, pertenecer al usuario del worker y tener modo `0600`.
Contiene los IDs fijos `COLMAT_AUTOMATION_SCHEDULER_ID`, `COLMAT_AUTOMATION_AUTHOR_ID`,
`COLMAT_AUTOMATION_REVIEWER_ID` y `COLMAT_AUTOMATION_PUBLISHER_ID`, además de las integraciones
necesarias. Los valores presentes en ese archivo prevalecen sobre un entorno heredado. No pases
secretos en argumentos ni los guardes en OpenClaw.

Usa tres archivos **distintos, modo `0600` y fuera del checkout**: `automation-worker.env` puede
acceder a MiniMax y X; `generation-worker.env` accede a MiniMax pero rechaza cualquier variable
`X_*`; `publication-worker.env` accede a X y nunca recibe MiniMax. La clave MiniMax puede
inyectarse a los dos primeros mediante un `EnvironmentFile` de systemd con el mismo modo, sin
duplicarla en los archivos del job. `COLMAT_AUTOMATION_MEDIA_ROOT` y
`COLMAT_GENERATION_MEDIA_ROOT` deben apuntar ambos a `/var/lib/colmat-x/media`, para que el
publicador lea media de las dos colas. Una imagen mayor de 5 MiB se descarta antes de crear el
draft.

La raíz no es una ruta libre: el worker acepta únicamente los perfiles exactos `project`, `user`
o `system` (respectivamente `.state/media`, `~/.local/share/colmat/media` y
`/var/lib/colmat-x/media`), además de esas rutas canónicas exactas. Los alias locales heredados
`.state/media/automation` y `.state/media/generation` convergen en `.state/media`; valores con
`..`, subdirectorios o prefijos parecidos se rechazan antes de tocar el sistema de archivos.

## Flujo editorial YAML heredado

Este flujo manual usa `content/posts/*.yaml`, `sync` y `run-due`; es distinto de la agenda de
plataforma anterior. No ejecutes ambos schedulers sobre la misma cuenta sin una decisión operativa
explícita.

1. Duplica `content/posts/001-bienvenida.yaml` y asigna un `id` único.
2. Escoge una plantilla de `content/templates/` y completa `data`.
3. Ejecuta `colmat-x validate` y `colmat-x preview --id TU_ID`.
4. Revisa texto, hora y el hash `snapshot de aprobación` que muestra `preview`.
5. Sincroniza con `colmat-x sync`.
6. Aprueba exactamente ese snapshot:

   ```bash
   colmat-x approve TU_ID --by "Equipo editorial" --snapshot HASH_COMPLETO
   ```

7. Comprueba lo vencido con `colmat-x run-due`; esto no llama a X.
8. Solo cuando todo esté listo, habilita y ejecuta la publicación real.

La aprobación vive en SQLite, no en el YAML. Cualquier cambio de texto o de hora invalida el
snapshot y devuelve la pieza a `draft`; hay que previsualizarla y aprobarla de nuevo. `--by` es
metadato de auditoría, no autentica por sí solo a una persona: restringe también el acceso al host
y a la cuenta de servicio.

Si eliminas de `content/posts/` un YAML que estaba en borrador o programado, la siguiente
sincronización lo cancela en SQLite para que no pueda publicarse desde estado antiguo. Si repones
un archivo con el mismo ID, ejecuta `colmat-x restore TU_ID` y vuelve a aprobarlo. Puede contener
texto u hora nuevos: `restore` carga ese snapshot como `draft` y nunca conserva la aprobación
anterior. Eliminar todos los YAML es válido y cancela toda pieza activa.

Ejemplo de contenido:

```yaml
id: colmat-pregunta-001
template: idea
publish_at: "2026-09-02T09:00:00-05:00"
data:
  titulo: "¿Qué pregunta necesitamos formular mejor?"
  desarrollo: "Las ideas ganan claridad cuando se contrastan con otras perspectivas."
  cierre: "Colmat: pensamiento que se convierte en conversación."
```

`publish_at` siempre debe incluir el desplazamiento horario. El proyecto guarda UTC y muestra
las horas en `America/Bogota`, configurada en `config/colmat.yaml`.

## Comandos

```bash
colmat-x validate
colmat-x preview
colmat-x preview --id colmat-pregunta-001
colmat-x sync
colmat-x approve colmat-pregunta-001 --by "Equipo editorial" --snapshot HASH
colmat-x withdraw colmat-pregunta-001
colmat-x restore colmat-pregunta-001
colmat-x status
colmat-x status --state scheduled
colmat-x run-due                 # simulación, nunca llama a X
colmat-x doctor
colmat-x doctor --credentials    # comprueba secretos sin habilitar publicación
colmat-x x-whoami --expected-username CUENTA_INSTITUCIONAL
colmat-x team-list --actor-id USER_ID
colmat-x automation-validate
colmat-x automation-calendar --days 7
colmat-x automation-status --actor-id USER_ID
colmat-x automation-sync --actor-id SCHEDULER_ID --expected-version VERSION
colmat-x automation-mode human_review --actor-id OWNER_ID --expected-version VERSION
colmat-x automation-enable --actor-id SCHEDULER_ID --expected-version VERSION
colmat-x automation-disable --actor-id SCHEDULER_ID --expected-version VERSION
colmat-x automation-run --env-file /ruta/segura/automation-worker.env
colmat-x generation-run --env-file /var/lib/colmat-x/generation-worker.env --limit 5
colmat-x publication-run --env-file /var/lib/colmat-x/publication-worker.env --live --limit 5
```

`run-due` en simulación no llama a X, pero sí sincroniza YAML y puede crear, actualizar o cancelar
filas de SQLite. Un YAML inválido detiene toda la sincronización y, por seguridad, no se publica
ninguna otra pieza en esa ejecución. Aprobar una fecha ya vencida la deja lista para el siguiente
sondeo.

Campos de `data` requeridos por las plantillas incluidas:

| Plantilla | Campos |
| --- | --- |
| `idea` | `titulo`, `desarrollo`, `cierre` |
| `anuncio` | `encabezado`, `detalle`, `llamado` |
| `reflexion` | `reflexion`, `etiquetas` |

Si un cierre, llamado o etiqueta no aplica, conserva la clave con texto vacío.

Los topes están en `config/colmat.yaml`. Por defecto se procesa una publicación por ejecución y
como máximo dos por día. Las URL también están desactivadas allí porque actualmente tienen un
costo de API mucho mayor que una publicación sin URL. El límite diario es atómico para procesos
que comparten esta SQLite, pero no conoce publicaciones manuales ni otra instalación con una base
distinta.

## Credenciales y publicación real

En [X Developer Console](https://console.x.com/) crea o configura una app con permisos de
lectura y escritura y genera las credenciales OAuth 1.0a de usuario para la cuenta de Colmat.
Si cambias los permisos de una app existente, vuelve a generar sus access tokens. La cuenta de
desarrollador necesita crédito disponible: una integración correcta no evita que X rechace una
publicación por saldo insuficiente.

La clave MiniMax compartida previamente debe considerarse expuesta: **rótala** y configura solo la
nueva clave en el gestor de secretos. Verifica también que la cuenta MiniMax tenga cuota para texto
y, si hay slots con `generate_image: true`, para imagen. MiniMax es exclusivamente el generador;
no recibe permisos de owner, reviewer, scheduler o publisher.

Completa el entorno sin compartirlo ni subirlo a Git y conserva inicialmente ambos bloqueos:

```dotenv
X_CONSUMER_KEY=...
X_CONSUMER_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
COLMAT_LIVE_ENABLED=false
COLMAT_DIRECT_PUBLISH_ENABLED=false
```

Después ejecuta `colmat-x doctor --credentials` y `colmat-x x-whoami` con el usuario o ID esperado.
El primero valida presencia local; el segundo contacta X y bloquea una cuenta distinta. Ninguno
confirma saldo, precios o capacidad de escritura, que deben revisarse en la consola. Realiza la
primera prueba con una cuenta controlada y una sola pieza. El flujo YAML heredado usa solo
`COLMAT_LIVE_ENABLED`; el worker nuevo en modo `direct` exige simultáneamente los dos gates y
`--live` en esa invocación:

```bash
# En el entorno seguro del worker:
# COLMAT_LIVE_ENABLED=true
# COLMAT_DIRECT_PUBLISH_ENABLED=true
colmat-x automation-run --env-file /ruta/segura/automation-worker.env --live

# /generar: OpenClaw reclama, MiniMax redacta y Telegram recibe revisión; nunca X
colmat-x generation-run --env-file /var/lib/colmat-x/generation-worker.env --limit 5

# /publicar: solo snapshots ya aprobados y con los gates de X abiertos
colmat-x publication-run --env-file /var/lib/colmat-x/publication-worker.env --live --limit 5
```

El cliente envía `POST https://api.x.com/2/tweets` con contexto de usuario. Un Bearer Token
app-only no sirve para crear publicaciones. El proyecto usa OAuth 1.0a porque esta versión opera
una sola cuenta desatendida; X también permite OAuth 2.0 Authorization Code con PKCE.

Un `.env` con modo `0600` es la protección local mínima. Para producción, usa el gestor de
secretos del entorno y cifrado en reposo; rota de inmediato cualquier credencial expuesta.
Cuando se selecciona otro proyecto mediante `--config` o `COLMAT_CONFIG`, solo se carga el `.env`
de ese proyecto; un `.env` del directorio de trabajo no puede sustituir sus credenciales. La ruta
de configuración debe permanecer bajo el directorio de trabajo o el perfil del usuario. Las rutas
de contenido y plantillas permanecen dentro del proyecto; la base admite además el destino de
servicio dedicado `/var/lib/colmat-x/`. La normalización previa rechaza escapes con `..`, prefijos
hermanos y enlaces simbólicos.

Solo `.env.example`, con nombres y valores secretos vacíos, pertenece al repositorio. `.env`,
`.state/`, bases SQLite, logs, entornos virtuales, cobertura y artefactos de construcción están
excluidos por `.gitignore`. No fuerces su inclusión con `git add -f`.

## Ejecución programada con OpenClaw

OpenClaw se usa como **orquestador aislado** en un host persistente: asigna periódicamente tres
command jobs fijos, observa sus códigos de salida y alerta. No genera contenido por sí mismo, no
decide roles o aprobaciones y no sustituye la base, el RBAC ni la máquina de estados de Colmat.
MiniMax opera la generación exclusivamente dentro de `automation-run` y `generation-run`, donde
quedan aplicadas la política editorial, la validación de media, la auditoría y las colas durables;
nunca programa, aprueba ni publica en X.

Mantén el gateway de OpenClaw en loopback, con un perfil dedicado a Colmat y sin exponerlo a
Internet. Cada job debe ejecutar rutas absolutas y una identidad de sistema fija:

```text
/opt/colmat-x-automation/.venv/bin/colmat-x automation-run \
  --env-file /var/lib/colmat-x/automation-worker.env
/opt/colmat-x-automation/.venv/bin/colmat-x generation-run \
  --env-file /var/lib/colmat-x/generation-worker.env --limit 5
/opt/colmat-x-automation/.venv/bin/colmat-x publication-run \
  --env-file /var/lib/colmat-x/publication-worker.env --live --limit 5
```

`automation-run` puede recibir MiniMax y X porque cubre el pipeline diario completo.
`generation-run` recibe MiniMax para `/generar` y rechaza cualquier credencial `X_*`;
`publication-run` es el consumer de `/publicar`, recibe X y rechaza una clave MiniMax no vacía.
Mantén los tres archivos
`0600` fuera del checkout y los tres jobs inicialmente deshabilitados. Si systemd inyecta
`MINIMAX_API_KEY` mediante otro `EnvironmentFile` `0600`, limita esa inyección a los command jobs de
automation y generation; no copies la clave al repositorio ni al env de publication.

Los tres procesos comparten `/var/lib/colmat-x/media`: configura con ese mismo valor
`COLMAT_AUTOMATION_MEDIA_ROOT` en automation/publication y `COLMAT_GENERATION_MEDIA_ROOT` en
generation. Separar esas rutas impediría que `publication-run` verificara y leyera una imagen
creada por la otra cola.

Configura el directorio de trabajo en la raíz del repositorio. No añadas `--live` al job de
`automation-run` mientras la agenda esté en `human_review`; para `direct`, solo añádelo después de
la autorización humana, la rotación de MiniMax, la verificación de identidad/crédito de X y la
activación deliberada de ambos gates. `publication-run` exige su propio `--live`, pero sigue sin
contactar X mientras `COLMAT_LIVE_ENABLED=false`.

Deja el job deshabilitado durante el aprovisionamiento. Antes de activarlo, ejecuta manualmente el
mismo comando con el mismo usuario del sistema y confirma el JSON de salida, la auditoría y las
notificaciones. No configures además cron, systemd timer o Vercel Cron para el mismo worker: un
único orquestador facilita la operación y la investigación de resultados ambiguos.

Las unidades `deploy/systemd/` pertenecen al flujo YAML heredado `run-due`; no invocan
`automation-run`. Consulta `deploy/README.md` para la topología nueva y la parada de emergencia.

## FastAPI, PostgreSQL y Vercel

SQLite no es almacenamiento persistente válido en Vercel. Conecta un PostgreSQL serverless (por
ejemplo Neon) y define `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` y un
`WEB_AUTH_PEPPER` aleatorio de al menos 32 caracteres como secretos del proyecto. No definas
`MINIMAX_API_KEY` ni credenciales `X_*` en Vercel: `/generar`
solo encola y los workers persistentes atienden ambas integraciones. Vercel no crea ni migra el
esquema: aplícalo de forma explícita antes del despliegue y usa readiness para verificar que la
versión requerida ya existe. Despliega después de ejecutar la suite:

```bash
pytest -q
vercel link
vercel --prod
curl -fsS https://TU_DOMINIO/api/health
curl -fsS https://TU_DOMINIO/api/ready
```

La raíz redirige a `/login`; después del código de Telegram, `/app` muestra el centro de
operaciones. Todas las mutaciones exigen cookie segura, CSRF, origen del mismo host y permisos
RBAC; FastAPI solo encola trabajo y nunca llama a MiniMax o X durante la solicitud.

Solo cuando `ready` responda `200`, registra en BotFather/API de Telegram la URL HTTPS
`https://TU_DOMINIO/api/telegram/webhook` con el mismo secreto. Vercel aloja el plano de control y
el webhook; no ejecuta workers persistentes. OpenClaw, en el host aislado, invoca los tres
consumers contra la misma PostgreSQL. El webhook nunca genera ni publica y no recibe credenciales
de MiniMax o X.

Para desarrollo local:

```bash
uvicorn colmat_x.web:create_app --factory --host 127.0.0.1 --port 8000
```

El endpoint de salud no inicializa integraciones ni devuelve secretos. Readiness solo informa
`ok`, `missing` o `error`. El webhook limita el cuerpo a 1 MiB, exige JSON y el header
`X-Telegram-Bot-Api-Secret-Token`, y responde sin exponer errores internos.

El workflow de GitHub incluido ejecuta calidad y pruebas, pero no publica. Una tarea programada
de GitHub Actions no conserva SQLite de forma fiable entre ejecuciones y podría romper la
protección local contra duplicados.

## Estados y recuperación

La automatización de plataforma registra cada claim y su snapshot de agenda. En `human_review`,
un run termina en `awaiting_review`; en `direct` puede avanzar por `publishing` hasta `succeeded`.
Los cierres seguros son `failed` y `unknown`; este último nunca se reintenta automáticamente. El
resultado operativo también distingue `direct_blocked`, duplicados y límite diario. Consulta estos
registros con `automation-status`.

La cola YAML heredada usa estos estados:

```text
draft --approve--> scheduled --claim--> publishing --éxito--> published
  ^                   |                    |----error claro--> failed
  |                   |                    +----ambiguo------> unknown
  +------withdraw-----+

draft/scheduled --archivo ausente--> cancelled --restore--> draft
```

`failed` significa que X rechazó claramente la solicitud; puede revisarse y reencolarse:

```bash
colmat-x retry TU_ID
```

`unknown` significa que la conexión terminó sin confirmar si X publicó. Nunca se reintenta de
forma automática y bloquea toda publicación real hasta su conciliación. Primero revisa
manualmente la cuenta:

```bash
# Si sí aparece en X:
colmat-x reconcile-published TU_ID ID_DEL_POST_EN_X

# Si comprobaste que no aparece:
colmat-x retry TU_ID --confirm-not-published
```

Si un lease vence, `run-due` convierte primero ese intento a `unknown` y termina sin sincronizar
ni publicar nada más. Antes de confirmar que no se publicó, verifica también que el proceso o
servicio anterior ya terminó: el control local impide que un callback tardío cambie un intento
nuevo, pero no puede cancelar una solicitud externa que todavía esté en vuelo.

Esta precaución existe porque el endpoint de creación no documenta una clave de idempotencia:
repetir a ciegas una solicitud ambigua puede duplicar el contenido.

## Reglas, límites y costo de X

Información verificada el 15 de agosto de 2026; revisa siempre la consola antes de activar el
servicio:

Configura créditos prepago, límite de gasto y alertas en la consola. El control de duplicados del
proyecto compara texto normalizado dentro de su propia SQLite; la revisión editorial todavía debe
detectar contenido sustancialmente similar o ya publicado fuera de esta instalación.

- [Crear una publicación](https://docs.x.com/x-api/posts/create-post): endpoint, cuerpo y
  respuesta oficial.
- [Autenticación de API v2](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping):
  crear contenido requiere contexto de usuario.
- [Conteo de caracteres](https://docs.x.com/fundamentals/counting-characters): 280 caracteres
  ponderados para una publicación estándar; las URL cuentan como 23.
- [Límites de uso](https://docs.x.com/x-api/fundamentals/rate-limits): consulta también los
  encabezados `x-rate-limit-*` de cada respuesta.
- [Precios](https://docs.x.com/x-api/getting-started/pricing): en la fecha indicada, crear una
  publicación costaba USD 0,015 y hacerlo con URL USD 0,200. Los precios pueden cambiar.
- [Reglas de automatización](https://help.x.com/en/rules-and-policies/x-automation): prohíben,
  entre otros comportamientos, spam, duplicados y automatizar la interfaz web.
- [Etiqueta de cuenta automatizada](https://help.x.com/es/using-x/automated-account-labels):
  Colmat debe evaluar si la naturaleza de su operación requiere identificar y vincular la cuenta
  automatizada.

La validación Python normaliza Unicode y aplica una estimación segura para texto latino y los
casos habituales, pero no es la implementación oficial completa de `twitter-text`: algunas
secuencias complejas de emoji y rutas de URL pueden contarse de más. El detector cubre esquemas,
`www`, dominios comunes e internacionales, pero X conserva la validación definitiva. Si el equipo
necesita coincidencia exacta antes del envío, debe integrar el paquete oficial `twitter-text` de
JavaScript y su suite de conformidad.

## Desarrollo

```bash
ruff format --check .
ruff check .
pytest --cov --cov-report=term-missing
```

La integración real nunca corre en CI. Haz la primera prueba con una cuenta controlada, un solo
snapshot aprobado y el límite diario en `1`.
