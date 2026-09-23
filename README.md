# Colmat X Automation

Proyecto en Python para preparar, clasificar, revisar y publicar contenido institucional en X,
con control operativo por Telegram. Usa exclusivamente las API oficiales de X, Telegram y de los
motores de IA de una lista cerrada (MiniMax por defecto, Anthropic, OpenAI y Google Gemini); nunca
automatiza la interfaz web de X.

El proyecto nace en modo seguro. En `human_review`, toda salida de IA es un borrador y una persona
distinta del autor debe aprobar el snapshot exacto. En `direct`, la ejecución usa identidades de
servicio separadas y la publicación real exige dos seguros de entorno más `--live` y, además,
dos seguros persistidos que el bot de Telegram abre con doble paso y cierra de inmediato. El
webhook de Telegram consulta, ordena y registra decisiones, pero nunca llama a X ni a la IA.

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
- Motores de IA conectables (MiniMax por defecto; Anthropic, OpenAI o Google Gemini) para
  proponer borradores e imágenes en memoria, con validación local cerrada y sin capacidad de
  aprobar, programar o publicar. El proveedor de texto y el de imagen pueden ser distintos.
- Control operativo completo desde Telegram: agenda, seguros de publicación real, cola y
  conciliación, equipo, diagnóstico y auditoría, con confirmación de dos pasos para todo lo
  irreversible o con efecto externo.
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

`ai-draft` no usa la base de plataforma: toma el motor de `COLMAT_TEXT_PROVIDER` (MiniMax si no
se indica) y la clave de la variable propia de ese proveedor. Nunca subas una clave a Git; si
apareció en un chat, log o captura, rótala antes de usar producción.

## Motores de IA

El generador es un puerto con dos capacidades, texto e imagen, y cuatro adaptadores en
`src/colmat_x/providers/`. La lista de proveedores es cerrada y cada adaptador fija sus endpoints
en constantes del código; ninguna variable de entorno, fila de base o formulario puede inyectar
una URL. Los modelos admitidos también son una lista cerrada (`providers/catalog.py`): ampliarla
es un cambio de código revisado.

| Proveedor | Clave | Texto | Imagen | Endpoints fijos |
| --- | --- | --- | --- | --- |
| `minimax` (predeterminado) | `MINIMAX_API_KEY` | `MiniMax-M2.7` | `image-01` | `api.minimax.io/anthropic/v1/messages`, `/v1/chat/completions`, `/v1/image_generation` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` | no genera imágenes | `api.anthropic.com/v1/messages` |
| `openai` | `OPENAI_API_KEY` | `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna` | `gpt-image-2.5-sunburst` | `api.openai.com/v1/chat/completions`, `/v1/images/generations` |
| `gemini` | `GEMINI_API_KEY` | `gemini-3.8-flash`, `gemini-3.5-flash` | `gemini-3.1-flash-image` | `generativelanguage.googleapis.com/v1beta/models/<modelo>:generateContent` |

MiniMax conserva su camino actual: `MINIMAX_API_STYLE=anthropic` por defecto y `openai` solo como
compatibilidad explícita. Anthropic declara que no produce imágenes y falla con un error claro si
se le pide una; combina Claude para texto con MiniMax, OpenAI o Gemini para imagen. OpenAI solo
ofrece tres tamaños: una petición apaisada se aproxima a `1536x1024` y las dimensiones reales
quedan en la evidencia. En Gemini el único segmento variable de la URL es el nombre de modelo,
validado por patrón sin `/`, `:` ni `?`. Ningún adaptador envía `temperature` a Anthropic u
OpenAI, cuyos modelos vigentes rechazan parámetros de muestreo.

Cambiar de proveedor no altera el control editorial: cada salida pasa por `validate_ai_draft`, el
prompt visual por la política del manual y la imagen por límites locales de tamaño y firma
binaria; el resultado sigue siendo `status=draft` con `publication_authorized=false`. La evidencia
del borrador registra en `ai_engine` qué proveedor y qué modelo produjeron el texto y, si la hubo,
la imagen; el `source` de la media persistida es el nombre del proveedor.

### Selección persistida

El worker no lee la elección de archivos: `automation-run` y `generation-run` consultan
`ai_engine_settings` en la base al comenzar cada run y construyen el motor con esa fila. Cada
workspace nace con MiniMax para texto e imagen (versión 1); las bases anteriores reciben esa fila
por backfill. La escritura usa compare-and-swap con la versión leída, para que dos operadores no
se pisen, y exige el permiso `ai:engine:manage`, que solo tienen `owner` y `admin`: elegir motor
altera coste, flujo de datos y comportamiento de generación, y no es una decisión de agenda ni de
redacción. `editor`, `scheduler`, `reviewer`, `publisher` y `auditor` ven la selección en modo
lectura. Cada cambio deja auditoría con actor, momento y valores anterior y actual.

La elección es global por workspace. No hay override por slot ni por petición de `/generar`: un
override por slot añadiría otra lista cerrada al YAML de agenda, que no es fuente de verdad en
runtime, y uno por petición permitiría que un editor sin permiso de motor decidiera el proveedor.

```bash
colmat-x ai-engine-status --actor-id OWNER_ID
colmat-x ai-engine --actor-id OWNER_ID --expected-version VERSION \
  --text-provider anthropic --text-model claude-sonnet-5 --image-provider openai
```

Al omitir el modelo tras cambiar de proveedor se toma el predeterminado del catálogo. Un modelo
que no pertenezca al proveedor elegido, o `anthropic` como proveedor de imagen, se rechaza antes de
escribir.

### Credenciales: archivo del worker o almacén sellado

Hay dos vías, y la precedencia es fija: **si existe una credencial sellada en la base para el
proveedor, el worker la usa; si no existe, usa la variable de su archivo `0600`**. Si existe pero el
worker no puede abrirla, el run termina en `failed` con ese detalle; nunca cae en silencio a la
variable del entorno. El worker opera sin ninguna credencial en la base.

La vía de archivos es la de siempre, extendida: cada clave vive en un archivo `0600`, regular y
del mismo usuario, con una sola variable, declarado en el env primario como
`COLMAT_MINIMAX_ENV_FILE`, `COLMAT_ANTHROPIC_ENV_FILE`, `COLMAT_OPENAI_ENV_FILE` o
`COLMAT_GEMINI_ENV_FILE`.

La vía del panel guarda la clave en `ai_provider_credentials` dentro de un sobre sellado: X25519
más HKDF-SHA256 y AES-256-GCM, con el workspace y el proveedor como dato autenticado, de modo que un
sobre copiado a otra fila no se abre. El par de claves se genera una vez:

```bash
colmat-x ai-credential-keypair
```

La clave pública (`COLMAT_CREDENTIAL_PUBLIC_KEY`) sella y puede vivir en el panel, en el webhook
y en el worker. La clave privada (`COLMAT_CREDENTIAL_PRIVATE_KEY`) abre y vive **únicamente** en el
worker, en su propio archivo `0600` declarado como `COLMAT_CREDENTIAL_PRIVATE_KEY_ENV_FILE`; la
readiness web en Vercel falla si la encuentra en su entorno. Sin clave pública, el sistema arranca
en modo degradado, lo declara en el panel y en `ai-engine-status`, y rechaza guardar claves: nunca
las persiste en claro.

La fila y la auditoría nunca contienen el valor: solo el sobre, una huella SHA-256, los últimos
cuatro caracteres, quién la puso y cuándo. Ninguna respuesta HTTP, JSON de CLI, log ni mensaje de
error devuelve la clave; la suite lo comprueba. Solo `owner` tiene `ai:credentials:manage` para
crear, rotar o eliminar claves, porque son secretos con consecuencias económicas y el owner es el
responsable último del workspace; `admin` puede elegir motor y solicitar verificaciones, no tocar
claves. Alta, rotación, borrado, verificación y cambio de proveedor quedan auditados.

La verificación consulta el endpoint de modelos del proveedor (Anthropic, OpenAI y Gemini; MiniMax
no lo documenta y usa una llamada de un token). Al guardar desde el panel se comprueba en el acto
con la clave recién recibida. Comprobar una clave ya sellada exige abrir el sobre: el panel solo
registra la solicitud y el worker la ejecuta en su siguiente run, publicando válida, inválida o no
concluyente. Desde el host del worker es inmediato:

```bash
colmat-x ai-credential-set openai --actor-id OWNER_ID --env-file /ruta/segura/generation-worker.env
colmat-x ai-credential-verify openai --actor-id OWNER_ID --env-file /ruta/segura/generation-worker.env
colmat-x ai-credential-delete openai --actor-id OWNER_ID
```

`ai-credential-set` lee la clave por prompt oculto o por stdin, nunca por argumentos. El worker
publica en `ai_provider_status`, en cada run, qué proveedores tienen archivo de clave y si puede
abrir sobres; nunca los valores. El panel muestra ese estado junto a las claves selladas.

Consecuencias de seguridad de esta vía, para decidir con conocimiento: una filtración de la base
expone sobres inútiles sin la clave privada; una filtración de la clave privada sola no expone
nada sin la base; con ambas, todas las claves guardadas quedan expuestas y deben rotarse. Un
proceso web comprometido no puede leer claves ya guardadas, pero sí capturar las que se peguen
mientras dure el compromiso. La invariante de que el webhook no contiene credenciales de IA se
mantiene para las claves en reposo y se relaja para el instante de alta.

`src/colmat_x/ai_engine.py` expone `AIEngineService`, el puerto que usan el panel y la CLI y que
puede reutilizar el bot de Telegram: `overview`, `select_engine`, `store_credential`,
`delete_credential`, `verify_credential`, `resolve_api_key`, `build_engine`,
`report_worker_status` y `run_pending_verifications`.

## Estilo de Gustavo Bueno y norma RAE

El generador no redacta con un registro genérico: el prompt incorpora una **guía de estilo
derivada de un corpus real** de textos de Gustavo Bueno, publicados en abierto por la Fundación
Gustavo Bueno y su escuela. La cadena es reproducible y trazable:

1. `config/gustavo-bueno-corpus-sources.yaml` declara los tres índices autorizados (El Catoblepas
   en `nodulo.org`, *Filosofía en español* en `filosofia.org` y los artículos de prensa firmados en
   `fgbueno.es`), las condiciones de uso verificadas de cada sitio y el retardo mínimo por host.
2. `colmat-x bueno-corpus-build` descarga solo desde la lista blanca de `research_fetch`, lee
   `robots.txt` de cada host, respeta sus rutas prohibidas y su `Crawl-delay` (60 s en
   `filosofia.org`), se identifica con un `User-Agent` propio y guarda los textos **fuera de Git**
   (`.state/corpus/gustavo-bueno`, ignorado). Lo único versionado es
   `config/gustavo-bueno-corpus-manifest.yaml`: URL, título, año, fecha de descarga y SHA-256 del
   cuerpo recibido y del texto extraído de cada pieza.
3. `colmat-x bueno-corpus-verify` comprueba que cada texto local coincide con su huella.
4. `colmat-x bueno-style-derive` recalcula `config/estilo-gustavo-bueno-evidencia.yaml` (longitud
   de frase y párrafo, conectores, léxico técnico del materialismo filosófico, puntuación, aperturas,
   cierres, figuras argumentales, n-gramas) y sella la evidencia con el SHA-256 del manifiesto;
   `--check` falla si la evidencia versionada se desvía del corpus.
5. `config/estilo-gustavo-bueno.yaml` es la guía: cada término del léxico y cada directiva citan
   claves de la evidencia con un mínimo exigido, y el cargador rechaza cualquier afirmación sin
   respaldo, un manifiesto distinto, una autoridad ortográfica que no sea la RAE o un bloque de
   estilo demasiado largo.

`build_editorial_messages` inserta ese bloque después de la identidad visual y antes de la orden
de responder por herramienta. El estilo **modula el registro y no relaja ninguna regla**: cifra y
fuente literales, 280 caracteres, taxonomía cerrada, separación institucional, identidad visual y
`publication_authorized=false` siguen mandando; el propio prompt lo reitera. La norma ortográfica y
de estilo es exclusivamente la de la Real Academia Española (comillas angulares, coma decimal,
`25,2 %`, sin coma de Oxford, sin *Title Case* ni convenciones de manuales anglosajones). Los textos
de Bueno están protegidos por derechos de autor: no se copian en el repositorio ni en la guía, y
nunca se le atribuyen citas ni opiniones.

```bash
colmat-x bueno-corpus-build            # descarga cortés; tarda ~1 h por el Crawl-delay de filosofia.org
colmat-x bueno-corpus-verify           # huellas locales frente al manifiesto
colmat-x bueno-style-derive            # regenera la evidencia y valida la guía
colmat-x bueno-style-derive --check    # solo comprueba; falla ante cualquier desvío
```

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
| `/usuarios` | Lista `user_id`, rol y vínculos numéricos; solo owner y chat privado. |
| `/invitar <telegram_id> <rol> <correo> <nombre>` | Crea atómicamente un miembro y su vínculo privado; solo owner. |
| `/vincular <telegram_id> <user_id>` | Vincula un miembro existente por IDs reales; solo owner y chat privado. |
| `/linea` | Consulta la línea editorial del mes; owner o editor. |
| `/linea <AAAA-MM> <texto>` | Crea o versiona la línea mensual; solo owner y chat privado. |
| `/patrones` | Explica el patrón cerrado y los límites de la investigación asistida. |
| `/investigar <tema>` | Encola una síntesis sin imagen según la línea mensual; consulta solo URLs oficiales autorizadas y nunca publica. |
| `/calendario [días]` | Consulta entre 1 y 31 días de agenda persistida. |
| `/modo [human_review\|direct]` | Consulta o fija el modo; `direct` exige owner/admin, chat privado, el seguro persistido `direct` abierto y confirmación con botón. |
| `/generar <brief>` | Encola según la línea mensual; el webhook no llama al motor de IA. |
| `/publicar <id>` | Encola durablemente un borrador ya aprobado; el webhook no lo envía a X. |
| `/agenda` | Estado de la agenda persistida, seguros y runs recientes; cualquier rol. |
| `/agenda runs [1-20]` | Runs recientes con estado, intento y error; cualquier rol. |
| `/agenda validar` | Valida el `config/automation.yaml` desplegado sin tocar la base; cualquier rol. |
| `/agenda sincronizar` | Copia el YAML a la base con CAS y conserva el modo; owner, admin o scheduler, chat privado. |
| `/agenda activar` | Activa la agenda con CAS; en `direct` exige confirmación con botón; owner, admin o scheduler, chat privado. |
| `/agenda pausar` | Pausa la agenda con CAS; owner, admin o scheduler, desde cualquier chat autorizado. |
| `/agenda reintentar <run_id>` | Solicita un reintento de un run `failed` sin borrador; owner, admin o scheduler, chat privado. |
| `/seguros` | Estado de los seguros persistidos `live` y `direct`, con quién y cuándo los abrió; cualquier rol. |
| `/seguro <live\|direct> off` | Cierra un seguro de inmediato (parada de emergencia); owner o admin, desde cualquier chat autorizado. |
| `/seguro <live\|direct> on` | Abre un seguro con confirmación de botón; owner o admin, chat privado. |
| `/cola [1-20]` | Solicitudes de publicación y generación con su estado; cualquier rol. |
| `/cola conciliar <request_id> publicado <x_post_id>` | Cierra un `unknown` verificado como publicado, con confirmación; publisher, owner o admin, chat privado. |
| `/cola conciliar <request_id> no_publicado` | Cierra un `unknown` verificado como no publicado, con confirmación; nunca reencola el snapshot. |
| `/revisar <id>` | Reemite botones de aprobación y rechazo ligados al snapshot actual; reviewer distinto del autor. |
| `/retirar <id>` | Devuelve a `draft` una aprobación que aún no entró en la cola; reviewer distinto del autor. |
| `/baja <user_id>` | Desactiva a un integrante de rol delegable, con confirmación; solo owner y chat privado. |
| `/alta <user_id>` | Reactiva a un integrante de rol delegable; solo owner y chat privado. |
| `/rol <user_id> <rol>` | Cambia entre roles delegables; solo owner y chat privado. |
| `/doctor` | Diagnóstico del plano de control: base, agenda, seguros, línea, YAML, colas y runs; cualquier rol. |
| `/auditoria [1-50]` | Últimos eventos de auditoría; owner, admin o auditor. |
| `/ayuda` | Muestra la guía integrada. |

Los callbacks de aprobación o rechazo usan nonces expirables y de un solo uso. Editar texto,
fecha, evidencia o imagen invalida la aprobación anterior. Ni los comandos ni los callbacks del
webhook llaman directamente a X: solo consultan o mutan el estado transaccional. Cualquier acceso
a X queda fuera del proceso web y bajo los seguros del worker persistente.

Las órdenes irreversibles o con efecto externo —abrir un seguro, pasar a `direct`, activar la
agenda en `direct`, dar de baja a alguien y conciliar un `unknown`— nunca se ejecutan con una
sola línea de texto. El bot responde con un teclado `Confirmar`/`Cancelar` cuyo nonce vive en
`control_confirmations`, ligado a `from.id`, al chat privado, a la acción y a la versión CAS
leída; vale diez minutos, se consume una sola vez y el callback vuelve a exigir el mismo permiso
que la orden. Cerrar un seguro o pausar la agenda es la dirección segura y se aplica de inmediato
desde cualquier chat autorizado. Cada orden, confirmación y cancelación deja auditoría con actor,
momento, acción y resultado.

Desde Telegram nunca se crean owners ni admins, ni se modifican sus membresías: esas altas y
bajas siguen exigiendo `team-add` en el host. Tampoco se reencola un snapshot cuyo envío terminó
`failed` o `unknown`: la conciliación solo documenta el resultado verificado y, para publicar de
nuevo, hay que revisar, aprobar y encolar una revisión nueva.

Las altas administrativas no confían en `@username`: el owner entrega el `from.id` decimal y el
bot crea o vincula únicamente un chat privado cuyo `chat_id` coincide con ese ID. Los roles que se
pueden dar de alta por Telegram son `editor`, `reviewer`, `publisher`, `scheduler` y `auditor`; cada
uno conserva sus permisos separados. Repetir exactamente un alta es idempotente y toda creación,
membresía, vínculo y cambio de línea deja auditoría.

`/investigar` aplica siempre este patrón: pregunta o tesis; hechos y cifras solo de fuentes
aportadas o del registro canónico; separación entre hecho e inferencia; contraste o posible
refutación; síntesis futura para X; y prohibición de fabricar citas. Antes de delegar la síntesis,
el worker recupera como máximo tres URLs HTTPS de una lista institucional cerrada, rechaza
redirecciones a otro host y limita tanto el cuerpo como el texto entregado al modelo. MiniMax solo
produce una **síntesis exploratoria**: no hace navegación web abierta. El registro canónico contiene 44 títulos
o series documentados y una atribución editorial disputada; el selector envía como máximo tres
coincidencias fuertes por título, cuya pertinencia debe comprobarse. La atribución de 1962 solo se
incluye ante una consulta explícita y se presenta junto con la fuente donde Bueno negó su
participación. Si no hay coincidencia, se incluyen únicamente las URLs maestras y el brief exige
marcar las inferencias `POR VERIFICAR`. La evidencia durable conserva las URLs finales y el
SHA-256 de cada respuesta, nunca el cuerpo descargado; la verificación editorial humana sigue
siendo obligatoria.

El worker deriva del prefijo interno —no de la salida de MiniMax— la evidencia
`research_only=true`. Esa síntesis se entrega sin botones de aprobación o publicación y queda
bloqueada al encolar, reclamar o crear directamente un intento de publicación, incluso para filas
antiguas. Tampoco puede quitarse la marca mediante una revisión: tras verificar los hallazgos, un
owner o editor debe copiarlos deliberadamente a `/generar`, que crea un draft separado, exige y
antepone la línea vigente con su versión y vuelve a pasar por revisión humana. Si línea más encargo
exceden 1000 caracteres, la solicitud se rechaza en vez de omitir la directriz. `/publicar` no
reescribe nada: solo encola un snapshot publicable que ya fue aprobado por un rol publisher.

`generation-run` consume la cola de `/generar`: OpenClaw asigna el command job, el motor de IA
persistido propone texto y, opcionalmente, imagen, y Colmat crea siempre un draft `in_review`. Una outbox durable
entrega la revisión y sus botones al binding activo de un usuario con permiso de revisión; quien
solicita puede ser editor sin recibir permiso para aprobar. Los nonces se emiten atómicamente al
preparar la entrega y solo existen en memoria y en el mensaje de Telegram; la base conserva sus
hashes.
`publication-run` consume por separado la cola aprobada de `/publicar` con lease, fence,
idempotencia y los gates de X. El webhook no contiene credenciales de IA en reposo ni de X: solo
recibe una clave de proveedor en el instante en que un owner la pega para sellarla.

El panel web usa el mismo binding `control` para enviar un código de ocho dígitos por Telegram.
Solo conserva HMAC de códigos, sesiones y CSRF; los códigos duran cinco minutos y las sesiones se
revalidan contra usuario activo, membresía, rol y binding en cada solicitud. `SirHegel` puede crear
cuentas desde `/app`; cada nueva identidad debe vincularse a su propio Telegram antes de entrar.

La sección **Motor de IA** de `/app` muestra el proveedor y modelo vigentes, el estado de cada
proveedor (clave sellada en la base, archivo presente en el worker, última verificación) y, para
`owner` y `admin`, el formulario de selección con versión CAS. Solo `owner` ve los campos para
pegar, rotar o eliminar claves; el campo es de tipo contraseña y la clave sale del formulario
sellada, sin pasar por logs ni volver en ninguna respuesta. El botón «Probar» sobre una clave ya
guardada registra la solicitud y el worker la ejecuta.

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
configuración que ya fue persistida. El mismo flujo existe en Telegram: `/agenda validar` valida
el YAML desplegado con el plano de control, `/agenda sincronizar` lo copia con CAS, `/agenda
activar` y `/agenda pausar` cambian `enabled`, y `/agenda reintentar` deja la solicitud de
reintento. El bot lee la versión vigente antes de escribir; si otra escritura se cruza, la orden
falla y hay que repetirla.

Si un run termina en `failed` antes de persistir un borrador, el scheduler seguirá devolviendo un
resultado no saludable para ese slot en vez de ocultarlo como un duplicado exitoso. Un owner,
admin o scheduler puede solicitar exactamente un nuevo intento sobre el mismo run:

```bash
colmat-x automation-retry RUN_ID --actor-id SCHEDULER_ID
```

La orden solo deja la solicitud durable y auditada. El siguiente `automation-run` escanea retries
pendientes de cualquier fecha local, reclama exactamente el `run_id` original, incrementa
`attempt_count` y conserva la misma clave de idempotencia y el mismo cupo diario. Nunca crea un run
retroactivo nuevo. Si la agenda dejó de corresponder, retira y audita el flag en esa ejecución para
que no quede varado al cruzar otra medianoche. No admite runs `unknown` ni runs que ya tengan un
draft: esos casos requieren conciliación para no duplicar snapshots o efectos externos.

Antes de llamar al motor de IA, cada slot autónomo resuelve la línea editorial del mes de su fecha
programada y la antepone al brief con su versión. Mes, versión y SHA-256 quedan en la evidencia del
draft. Si la línea falta o es inválida, el run termina en `failed` antes de generar o publicar.

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

En `human_review`, el worker genera el texto con el motor persistido, puede generar la imagen
cuando el slot lo indica, guarda ambos como borrador y los somete a aprobación. Si el worker no
tiene la clave del proveedor elegido, o no puede abrir el sobre de la base, el run termina en
`failed` con ese detalle y nunca cambia de proveedor por su cuenta. Un slot con imagen generada permanece
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
Los tres comandos worker crean el esquema únicamente cuando usan SQLite local. Con PostgreSQL
operan sin DDL; aplica `deploy/postgres.sql` con una identidad de migración antes de arrancarlos.

Usa tres archivos **distintos, modo `0600` y fuera del checkout**: `automation-worker.env` puede
acceder a los motores de IA y a X; `generation-worker.env` accede a los motores pero rechaza
cualquier variable `X_*`; `publication-worker.env` accede a X y rechaza toda clave de IA y la clave
privada de sobres. Guarda cada clave una sola vez en un archivo `0600` con una única variable
(`MINIMAX_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` o
`COLMAT_CREDENTIAL_PRIVATE_KEY`); los dos primeros envs declaran su ruta como
`COLMAT_MINIMAX_ENV_FILE`, `COLMAT_ANTHROPIC_ENV_FILE`, `COLMAT_OPENAI_ENV_FILE`,
`COLMAT_GEMINI_ENV_FILE` o `COLMAT_CREDENTIAL_PRIVATE_KEY_ENV_FILE` y el cargador las incorpora sin
interpolar ni copiarlas a la definición del job. Una ruta heredada que no figure en el env
primario se ignora.
`COLMAT_AUTOMATION_MEDIA_ROOT` y
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
colmat-x ai-engine-status --actor-id OWNER_ID
colmat-x ai-engine --actor-id OWNER_ID --expected-version VERSION --text-provider anthropic
colmat-x ai-credential-keypair
colmat-x ai-credential-set openai --actor-id OWNER_ID --env-file /ruta/segura/generation-worker.env
colmat-x ai-credential-verify openai --actor-id OWNER_ID --env-file /ruta/segura/generation-worker.env
colmat-x ai-credential-delete openai --actor-id OWNER_ID
colmat-x safeguards-status --actor-id USER_ID
colmat-x safeguards-set live on --actor-id OWNER_ID --expected-version VERSION
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
nueva clave en el gestor de secretos o séllala desde el panel. Verifica también que la cuenta del
proveedor elegido tenga cuota para texto y, si hay slots con `generate_image: true`, para imagen.
El motor de IA es exclusivamente el generador; no recibe permisos de owner, reviewer, scheduler o
publisher.

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

### Seguros de publicación real: dos capas

Las variables de entorno son la **capacidad**: viven en los archivos `0600` del host del worker
y solo cambian con acceso al host. Encima hay una capa de **intención operativa** persistida en
la tabla `publication_safeguards`: dos seguros, `live` y `direct`, con versión CAS, evidencia de
quién y cuándo los abrió y auditoría de cada cambio. Nacen cerrados; una base sin fila equivale a
ambos en `false`. Los workers exigen las dos capas a la vez:

| Efecto | Exige en el host (variables del worker) | Exige en la plataforma (bot o CLI) |
| --- | --- | --- |
| `publication-run` envía a X un snapshot aprobado | `COLMAT_LIVE_ENABLED=true` y `--live` | seguro `live` abierto |
| `automation-run` publica sin revisión en `direct` | ambas variables en `true` y `--live` | seguros `live` y `direct` abiertos, modo `direct` y agenda activa |
| Registrar el modo `direct` en la configuración | `COLMAT_DIRECT_PUBLISH_ENABLED=true` en el proceso que escribe **o** | seguro `direct` abierto |

Lo que el bot controla: abrir cada seguro con doble paso (owner o admin, chat privado, botón de
un solo uso), cerrarlo de inmediato desde cualquier chat autorizado, pasar a `direct` y volver a
`human_review`, activar y pausar la agenda, y observar todo con `/seguros`, `/agenda` y
`/doctor`. El worker relee los seguros persistidos antes de cada envío, de modo que un cierre
desde Telegram detiene la siguiente publicación sin redeploy y sin tocar el host. El mismo estado
se opera desde el host con `safeguards-status` y `safeguards-set`.

Lo que sigue exigiendo acceso al host, y por qué: las variables `COLMAT_LIVE_ENABLED` y
`COLMAT_DIRECT_PUBLISH_ENABLED` de los archivos del worker, la opción `--live` del command job,
las credenciales de X y MiniMax, `x-whoami` (la única verificación que contacta a X, y el webhook
no tiene credenciales), la alta o baja de owners y admins, el flujo YAML heredado (`approve`,
`withdraw`, `retry`, `reconcile-published` sobre la SQLite local) y la aplicación del esquema. Con
esa división, una cuenta de Telegram comprometida puede como máximo detener la operación o dejar
todo armado en la plataforma, pero nunca convierte por sí sola una instalación cerrada en una que
publica: la capacidad sigue en el host. Al desplegar esta versión sobre una instalación existente,
los seguros persistidos aparecen cerrados y hay que abrirlos deliberadamente.

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
El motor de IA opera la generación exclusivamente dentro de `automation-run` y `generation-run`,
donde quedan aplicadas la política editorial, la validación de media, la auditoría y las colas
durables; nunca programa, aprueba ni publica en X.

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

`automation-run` puede recibir claves de IA y X porque cubre el pipeline diario completo.
`generation-run` recibe claves de IA para `/generar` y rechaza cualquier credencial `X_*`;
`publication-run` es el consumer de `/publicar`, recibe X y rechaza toda clave de IA no vacía y la
clave privada de sobres. Mantén los tres archivos `0600` fuera del checkout y los tres jobs
inicialmente deshabilitados. Cada clave de proveedor y la clave privada de sobres viven en un
dotenv exclusivo `0600`, regular y del mismo usuario, con una sola variable y un máximo de 4 KiB.
Declara sus rutas (`COLMAT_MINIMAX_ENV_FILE`, `COLMAT_ANTHROPIC_ENV_FILE`,
`COLMAT_OPENAI_ENV_FILE`, `COLMAT_GEMINI_ENV_FILE`, `COLMAT_CREDENTIAL_PRIVATE_KEY_ENV_FILE`)
dentro de los envs primarios de automation y generation. No copies ninguna al repositorio, a
OpenClaw ni al env de publication.

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
`MINIMAX_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`COLMAT_CREDENTIAL_PRIVATE_KEY` ni credenciales `X_*` en Vercel: `/generar` solo encola y los
workers persistentes atienden ambas integraciones; la readiness falla si encuentra alguna. Sí
puedes definir `COLMAT_CREDENTIAL_PUBLIC_KEY` para que el panel selle claves. Vercel no crea ni
migra el esquema: aplícalo de forma explícita antes del despliegue (incluidas las tablas
`ai_engine_settings`, `ai_provider_credentials` y `ai_provider_status` de `deploy/postgres.sql`) y
usa readiness para verificar que la versión requerida ya existe. Despliega después de ejecutar la
suite:

```bash
pytest -q
vercel link
vercel --prod
curl -fsS https://TU_DOMINIO/api/health
curl -fsS https://TU_DOMINIO/api/ready
```

La raíz redirige a `/login`; después del código de Telegram, `/app` muestra el centro de
operaciones. Todas las mutaciones exigen cookie segura, CSRF, origen del mismo host y permisos
RBAC; FastAPI solo encola trabajo y nunca llama al motor de IA o a X durante la solicitud, salvo la
comprobación mínima de una clave recién pegada contra el endpoint de modelos de su proveedor.

Solo cuando `ready` responda `200`, registra en BotFather/API de Telegram la URL HTTPS
`https://TU_DOMINIO/api/telegram/webhook` con el mismo secreto. Vercel aloja el plano de control y
el webhook; no ejecuta workers persistentes. OpenClaw, en el host aislado, invoca los tres
consumers contra la misma PostgreSQL. El webhook nunca genera ni publica y no recibe credenciales
de IA en reposo ni de X.

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

En la cola de plataforma (`/publicar`), un `unknown` se concilia desde Telegram con
`/cola conciliar <request_id> publicado <x_post_id>` o `... no_publicado`, siempre con confirmación
y tras comprobar la cuenta. El snapshot nunca se reencola: publicar de nuevo exige revisar y
aprobar una revisión nueva.

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
