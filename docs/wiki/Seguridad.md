# Seguridad

## El modelo de amenaza

Botata es un bot **público**: cualquier desconocido puede hablarle, y un LLM obedece texto.
El diseño asume que la prompt injection **va a pasar** — la pregunta no es cómo evitarla
sino qué puede conseguir alguien que la logre. La respuesta buscada: nada importante.

## Las defensas, por capa

### Los permisos no dependen del modelo

- Cada tool tiene un **scope** (`reply` / `feed_reflection` / `admin`) verificado en
  código: una tool que no está en el scope del contexto ni siquiera se le ofrece al modelo.
- Los comandos de admin exigen que el autor **sea** el admin: comparación de strings contra
  `ADMIN_HANDLE` de settings. El LLM clasifica, pero jamás autoriza.
- Las tools sensibles (y **todas** las tools MCP) nacen scope `admin`. Promoverlas a
  `reply` es opt-in explícito del operador.

### Por mención solo se reduce exposición

El admin puede administrar el bot hablándole, pero hay un lock de doble capa (handler +
guarda en la persistencia) sobre lo crítico. **Jamás** se puede cambiar por mención:

- la identidad (`BOT_HANDLE` / `ADMIN_HANDLE`);
- `MODELS` y endpoints — **anti-exfiltración**: redirigir el endpoint del LLM mandaría
  prompts y memoria a un servidor ajeno;
- la estructura MCP;
- las tools de config a sí mismas (anti auto-lockout);
- la tarea `mentions` (es el canal por el que viaja el `/wake`);
- **ampliar** scopes de cualquier tool.

Apagar cosas sí se puede: reducir exposición por mención está bien; ampliarla, solo desde
el panel local en 127.0.0.1.

### Contenido de terceros = datos, no instrucciones

- `read_url` trae páginas **rotuladas como dato**, y tiene guarda de **SSRF**: resuelve el
  host y rechaza IPs no globales (loopback, LAN, metadata de cloud), revalidando cada
  redirect — nadie le hace leer servicios internos.
- No existe una tool genérica de HTTP request en scope público, a propósito.
- Solo el admin escribe en `skills/` y `moods/`: esos cuerpos entran al system prompt.

### Cuando el prompt no alcanza, la guarda va en código

Lección pagada del proyecto: si una conducta falla por prompt tres veces, se acota en
código. Topes duros por día/hilo en generación de imágenes, verificación de links contra
los que las tools realmente trajeron (anti link inventado), validación de todo plan que un
LLM quiera persistir en la base (todo-o-nada, con undo), tope de reintentos por mención.

### Operación

- Credenciales en el `.env` de la instancia (gitignored, write-only en el panel); jamás en
  el repo.
- El panel de configuración solo escucha en `127.0.0.1`.
- **Presupuesto diario de tokens**: si se quema, el bot se duerme — un ataque de volumen
  no te funde la cuenta.
- La DB es local; los embeddings corren en tu CPU: los datos de tu comunidad no salen a
  ningún servicio externo.
