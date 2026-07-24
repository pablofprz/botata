# Plantilla de instancia (T28c)

Una **instancia** = un agente desplegado: su identidad (SOUL, settings, skills,
moods) + sus datos (DB, media, credenciales). El **motor** (este repo) corre
cualquier instancia; hay un motor y N instancias.

Crear una instancia nueva:

```
python src/init_instance.py <carpeta-destino>
```

Eso copia esta plantilla COMPLETA: settings neutro + SOUL neutra + .env.example
+ los defaults genéricos de `prompts/`, `moods/`, `skills/` y `config/` (toda la
identidad default vive acá — el motor no tiene identidad propia).
Después: completá `.env`, editá `context/SOUL.md` y `config/settings.json`, y
arrancá con:

```
python -m botata --instance <carpeta-destino>
```

(o `BOTATA_INSTANCE=<carpeta>` en el entorno). Sin `--instance` ni env var, el
bot corta con instrucciones (salvo layout histórico repo-como-instancia, que
sigue detectándose por back-compat).

Regla: **canal nuevo de la misma comunidad = misma instancia; comunidad nueva =
instancia nueva** (con su propia DB — las memorias no se comparten entre
comunidades).
